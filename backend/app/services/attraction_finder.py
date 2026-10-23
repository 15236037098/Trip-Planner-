"""城市热门景点推荐。

四叉树递归拆分城市包围盒，并发检索高德 POI（仅标准库），
并结合高德「景点」检索顺序衡量热度；可按用户偏好做二次推荐。
"""

import re
import threading
import time
from itertools import chain

from services import amap_client, keyed_lock

PAGE_SIZE = 25
MAX_DEPTH = 1
CACHE_TTL = 3600
MAX_CACHE = 100
MAX_LIMIT = 5
UNKNOWN_POPULARITY = 10**6

_cache = {}
_cache_lock = threading.Lock()


class AttractionError(Exception):
    """城市或偏好无结果时抛出。"""


def recommend(city, preference=None, exclude=None, limit=3):
    """返回城市热门景点，preference 用于按偏好筛选。"""
    city = (city or "").strip()
    if not city:
        raise AttractionError("请提供城市名称")
    limit = max(1, min(int(limit or 3), MAX_LIMIT))

    candidates = _city_candidates(city)
    if preference:
        candidates = _apply_preference(city, candidates, preference)
    candidates = _rank(candidates)

    excluded = {name.strip() for name in (exclude or []) if name and name.strip()}
    picked = []
    for item in candidates:
        if item["name"] in excluded:
            continue
        if any(item["name"] in chosen["name"] or chosen["name"] in item["name"] for chosen in picked):
            continue
        picked.append(item)
        if len(picked) >= limit:
            break

    if not picked:
        raise AttractionError(f"没有找到更多符合条件的{city}景点，可以换个城市或放宽偏好")

    return {
        "city": city,
        "preference": (preference or "").strip(),
        "attractions": picked,
    }


def render_summary(result):
    """把推荐结果转成供大模型阅读的文本。"""
    items = result["attractions"]
    lines = [f"{result['city']}热门景点推荐（{len(items)} 个）："]
    for index, item in enumerate(items, 1):
        rating = f"评分 {item['rating']}" if item["rating"] else "评分暂无"
        category = f"｜{item['category']}" if item["category"] else ""
        address = f"｜{item['address']}" if item["address"] else ""
        lines.append(f"{index}. {item['name']}｜{rating}{category}{address}")

    if result["preference"]:
        lines.append(f"以上按偏好「{result['preference']}」推荐，如仍不满意可以补充偏好，我再换一批。")
    else:
        lines.append("如果这些不合心意，请告诉我你的偏好（如亲子、历史人文、自然风光、夜景、美食）。")
    return "\n".join(lines)


def to_payload(result):
    """转成前端渲染用的精简结构（不含内部排序字段）。"""
    keys = ("name", "rating", "address", "category", "lng", "lat", "photo")
    return {
        "city": result["city"],
        "preference": result["preference"],
        "attractions": [{key: item[key] for key in keys} for item in result["attractions"]],
    }


def _city_candidates(city):
    """四叉树覆盖 + 关键词热度排序，按城市缓存。"""
    cached = _cache_get(city)
    if cached is not None:
        return cached

    with keyed_lock.lock_for(("attraction", city)):
        cached = _cache_get(city)
        if cached is not None:
            return cached

        bbox, prefix = _city_geo(city)
        # 两批子请求的实际耗时由全局 QPS 决定，顺序执行即可，避免嵌套线程池
        crawled = _crawl(bbox, prefix, "景点")
        popular_pois, popular_ranks = _popular_candidates(city, prefix)

        merged = {}
        for poi in chain(crawled.values(), popular_pois):
            name = _text(poi.get("name"))
            if not name:
                continue
            item = _normalize(poi, popular_ranks.get(name, UNKNOWN_POPULARITY))
            current = merged.get(name)
            if current is None or item["_popularity"] < current["_popularity"]:
                merged[name] = item

        candidates = list(merged.values())
        if not candidates:
            raise AttractionError(f"没有找到{city}的景点")

        _cache_set(city, candidates)
        return candidates


def _cache_get(key):
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if entry and now - entry[0] < CACHE_TTL:
            return entry[1]
        if entry:
            _cache.pop(key, None)
    return None


def _cache_set(key, value):
    now = time.time()
    with _cache_lock:
        _cache[key] = (now, value)
        while len(_cache) > MAX_CACHE:
            oldest = min(_cache, key=lambda item: _cache[item][0])
            _cache.pop(oldest, None)


def _city_geo(city):
    """行政区边界转包围盒，并给出用于过滤外地 POI 的 adcode 前缀。"""
    district = amap_client.district(city)
    bbox = _bbox(district.get("polyline", ""))
    if bbox is None:
        raise AttractionError(f"无法获取{city}的范围")
    return bbox, _adcode_prefix(district.get("adcode", ""))


def _crawl(bbox, adcode_prefix, keywords):
    """逐层拆分四叉树：满页的格子继续拆，同层格子并发检索。"""
    collected = {}
    frontier = [bbox]
    depth = 0

    while frontier and depth <= MAX_DEPTH:
        next_frontier = []
        pois_list = amap_client.map_calls(
            lambda cell: _query_cell(cell, keywords), frontier
        )
        for cell, pois in zip(frontier, pois_list):
            for poi in pois:
                if not _in_city(poi, adcode_prefix):
                    continue
                key = poi.get("id") or poi.get("name")
                if key and key not in collected:
                    collected[key] = poi
            if depth < MAX_DEPTH and len(pois) >= PAGE_SIZE:
                next_frontier.extend(_split(cell))
        frontier = next_frontier
        depth += 1

    return collected


def _query_cell(cell, keywords):
    """检索单个矩形区域，失败时退避重试一次。"""
    polygon = f"{cell[0]},{cell[1]}|{cell[2]},{cell[3]}"
    for attempt in range(2):
        try:
            data = amap_client.search_polygon(polygon, keywords=keywords, offset=PAGE_SIZE)
            return data.get("pois") or []
        except amap_client.AmapError:
            if attempt == 0:
                time.sleep(0.3)
    return []


def _popular_candidates(city, adcode_prefix):
    """按高德「景点」检索顺序取热度榜，返回 (POI 列表, 名称→名次)。"""
    def fetch(page):
        try:
            data = amap_client.search_text("景点", city=city, offset=PAGE_SIZE, page=page)
            return data.get("pois") or []
        except amap_client.AmapError:
            return []

    pages = amap_client.map_calls(fetch, (1, 2))

    pois = []
    ranks = {}
    for poi in chain(*pages):
        name = _text(poi.get("name"))
        if not name or not _in_city(poi, adcode_prefix):
            continue
        ranks.setdefault(name, len(ranks))
        pois.append(poi)
    return pois, ranks


def _apply_preference(city, candidates, preference):
    """用偏好关键词做四叉树检索，并保留名称/类型匹配的候选。"""
    tokens = [token for token in re.split(r"[\s,，、/和与及]+", preference) if len(token) >= 2]
    matched = [
        {**item, "_preference_hit": True}
        for item in candidates
        if any(token in f"{item['name']}{item['category']}{item['address']}" for token in tokens)
    ]

    extras = _crawl_preference(city, preference)
    pool = _merge_by_name(extras + matched)
    if not pool:
        return candidates
    return pool + [item for item in candidates if item["name"] not in {p["name"] for p in pool}]


def _crawl_preference(city, preference):
    bbox, prefix = _city_geo(city)
    pois = _crawl(bbox, prefix, preference)
    items = [_normalize(poi, 0) for poi in pois.values() if poi.get("name")]
    items = sorted(items, key=lambda item: item["rating"], reverse=True)
    for index, item in enumerate(items):
        item["_popularity"] = index
        item["_preference_hit"] = True
    return items


def _merge_by_name(items):
    merged = {}
    for item in items:
        current = merged.get(item["name"])
        if current is None or (item["_popularity"], -item["rating"]) < (
            current["_popularity"],
            -current["rating"],
        ):
            merged[item["name"]] = item
    return list(merged.values())


def _rank(candidates):
    return sorted(
        candidates,
        key=lambda item: (
            0 if item.get("_preference_hit") else 1,
            item["_popularity"],
            -item["rating"],
        ),
    )


def _split(cell):
    min_lng, min_lat, max_lng, max_lat = cell
    mid_lng = (min_lng + max_lng) / 2
    mid_lat = (min_lat + max_lat) / 2
    return [
        (min_lng, min_lat, mid_lng, mid_lat),
        (mid_lng, min_lat, max_lng, mid_lat),
        (min_lng, mid_lat, mid_lng, max_lat),
        (mid_lng, mid_lat, max_lng, max_lat),
    ]


def _bbox(polyline):
    """行政区边界坐标转矩形包围盒。"""
    coords = []
    for point in re.split(r"[;|]", polyline or ""):
        parts = point.split(",")
        if len(parts) == 2:
            coords.append((float(parts[0]), float(parts[1])))
    if not coords:
        return None

    lngs = [coord[0] for coord in coords]
    lats = [coord[1] for coord in coords]
    return min(lngs), min(lats), max(lngs), max(lats)


def _adcode_prefix(adcode):
    """直辖市取前两位，其余城市取前四位，用于过滤外地 POI。"""
    adcode = str(adcode or "")
    if not adcode:
        return ""
    return adcode[:2] if adcode.endswith("0000") else adcode[:4]


def _in_city(poi, prefix):
    return not prefix or str(poi.get("adcode") or "").startswith(prefix)


def _normalize(poi, popularity):
    location = str(poi.get("location") or "")
    parts = location.split(",")
    lng, lat = (float(parts[0]), float(parts[1])) if len(parts) == 2 else (0.0, 0.0)

    biz = poi.get("biz_ext") or {}
    category = _text(poi.get("type")).split(";")[-1]

    return {
        "name": _text(poi.get("name")),
        "rating": _to_float(biz.get("rating")),
        "address": _text(poi.get("address")),
        "category": category,
        "lng": lng,
        "lat": lat,
        "photo": _first_photo(poi),
        "_popularity": popularity,
    }


def _first_photo(poi):
    """取 POI 的第一张可用图片。"""
    for photo in poi.get("photos") or []:
        url = _text((photo or {}).get("url"))
        if url.startswith(("http://", "https://")):
            return url
    return ""


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _text(value):
    return "" if isinstance(value, list) else str(value or "").strip()
