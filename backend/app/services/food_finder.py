"""餐饮推荐：附近餐馆与城市特色小吃。

附近餐馆用高德周边检索（按距离排序，分页并发）；
特色小吃用多关键词并发检索并按评分筛选。
纯标准库实现，结果裁剪为适合小程序渲染的 JSON 结构。
"""

import math
import re
import threading
import time

from services import amap_client, keyed_lock

FOOD_TYPES = "050100|050200|050300|050400"  # 中餐/外国餐厅/快餐厅/休闲餐饮
PAGE_SIZE = 25
MAX_LIMIT = 5
NEARBY_RADIUS = 1500
PREFERENCE_RADIUS = 3000
MIN_RATING = 3.5
CACHE_TTL = 1800
SNACK_CACHE_TTL = 3600
MAX_CACHE = 300

SNACK_NAME_TOKENS = (
    "小吃",
    "小笼",
    "汤包",
    "生煎",
    "馄饨",
    "饺子",
    "包子",
    "馒头",
    "烧麦",
    "锅贴",
    "面",
    "粉",
    "粥",
    "饼",
    "糕",
    "点心",
    "凉皮",
    "卤味",
    "串",
)

SOLO_WORDS = (
    "快餐",
    "小吃",
    "面馆",
    "面",
    "米粉",
    "饺子",
    "包子",
    "馄饨",
    "粥",
    "咖啡",
    "茶艺",
    "甜品",
    "糕饼",
    "简餐",
    "汉堡",
    "麻辣烫",
    "凉皮",
    "生煎",
    "汤包",
    "小笼",
)
GROUP_WORDS = (
    "火锅",
    "烧烤",
    "自助",
    "酒楼",
    "宴会",
    "私房菜",
    "烤鱼",
    "中餐厅",
    "西餐厅",
    "川菜",
    "粤菜",
    "湘菜",
    "东北菜",
    "本帮菜",
    "海鲜",
    "清真",
    "日料",
    "韩餐",
)

# 口语化偏好 → 可用于匹配店名/类型的词
PREFERENCE_HINTS = {
    "清淡": ("粥", "汤", "蒸", "素", "白灼"),
    "面食": ("面", "粉", "饺", "包"),
    "甜": ("甜", "糕", "饼", "点心"),
    "夜宵": ("烧烤", "串", "夜宵", "宵夜", "小龙虾"),
    "辣": ("川菜", "湘菜", "火锅", "麻辣", "辣"),
    "素食": ("素", "斋"),
    "海鲜": ("海鲜", "鱼", "蟹", "虾"),
}

_cache = {}
_cache_lock = threading.Lock()


class FoodError(Exception):
    """缺少城市/位置或检索失败。"""


def recommend(city=None, location=None, mode="nearby", preference=None, exclude=None, limit=3):
    """返回附近餐馆或城市特色小吃，preference 用于按偏好筛选。"""
    city = (city or "").strip()
    mode = _normalize_mode(mode)
    limit = max(1, min(int(limit or 3), MAX_LIMIT))

    if mode == "snack":
        if not city:
            raise FoodError("请提供城市名称")
        candidates = _snack_candidates(city, preference)
        location_label = ""
    else:
        anchor, location_label = _resolve_anchor(city, location)
        candidates = _nearby_candidates(anchor, location_label, preference)
        candidates = [item for item in candidates if item["rating"] >= MIN_RATING] or candidates

    picked = _pick(candidates, exclude, limit)
    if not picked:
        raise FoodError("没有找到更多符合条件的餐馆，可以换个位置或放宽偏好")

    return {
        "city": city,
        "location_label": location_label,
        "mode": mode,
        "preference": (preference or "").strip(),
        "preference_matched": any(item.get("_preference_hit") for item in candidates),
        "restaurants": picked,
    }


def render_summary(result):
    """把推荐结果转成供大模型阅读的文本。"""
    items = result["restaurants"]
    if result["mode"] == "snack":
        header = f"{result['city']}特色小吃推荐（{len(items)} 个）："
    else:
        place = result["location_label"] or "当前位置"
        header = f"{result['city']}{place}附近餐馆推荐（{len(items)} 个）："

    lines = [header]
    for index, item in enumerate(items, 1):
        parts = [item["name"]]
        parts.append(f"评分 {item['rating']}" if item["rating"] else "评分暂无")
        if item["distance_text"]:
            parts.append(item["distance_text"])
        if item["suitable_for"]:
            parts.append(item["suitable_for"])
        if item["address"]:
            parts.append(item["address"])
        lines.append(f"{index}. " + "｜".join(parts))

    if result["mode"] == "snack":
        if result["preference"] and not result["preference_matched"]:
            lines.append(
                f"没有找到特别符合「{result['preference']}」的小吃，以上是本地热门；"
                "可以换个偏好（如面食、甜点、夜宵、素食）。"
            )
        else:
            lines.append("不合口味的话，告诉我偏好（如面食、甜点、夜宵、清真），我再换一批。")
    else:
        lines.append("需要按口味、人数或预算再筛吗？告诉我偏好，我再推荐。")
    return "\n".join(lines)


def to_payload(result):
    """转成前端/小程序渲染用的精简结构。"""
    keys = (
        "name",
        "rating",
        "address",
        "category",
        "lng",
        "lat",
        "photo",
        "distance_text",
        "suitable_for",
    )
    return {
        "city": result["city"],
        "location_label": result["location_label"],
        "mode": result["mode"],
        "preference": result["preference"],
        "restaurants": [{key: item[key] for key in keys} for item in result["restaurants"]],
    }


def _nearby_candidates(anchor, location_label, preference):
    key = ("nearby", round(anchor[0], 3), round(anchor[1], 3), preference or "")
    cached = _cache_get(key)
    if cached is not None:
        return cached

    with keyed_lock.lock_for(("food", key)):
        cached = _cache_get(key)
        if cached is not None:
            return cached

        tasks = [
            {"radius": NEARBY_RADIUS, "page": 1, "keywords": None, "hit": False},
            {"radius": NEARBY_RADIUS, "page": 2, "keywords": None, "hit": False},
        ]
        if preference:
            tasks.append(
                {"radius": PREFERENCE_RADIUS, "page": 1, "keywords": preference, "hit": True}
            )

        results = amap_client.map_calls(
            lambda task: _fetch_around(anchor, task), tasks
        )

        base = []
        hits = []
        for task, pois in zip(tasks, results):
            bucket = hits if task["hit"] else base
            bucket.extend(_normalize(poi, anchor, location_label) for poi in pois)

        merged = _merge(base)
        if hits:
            preferred = _merge(hits)
            for item in preferred:
                item["_preference_hit"] = True
            preferred_names = {item["name"] for item in preferred}
            merged = preferred + [
                item for item in merged if item["name"] not in preferred_names
            ]

        tokens = _preference_tokens(preference)
        for item in merged:
            if item.get("_preference_hit") and tokens:
                item["_preference_exact"] = any(
                    token in f"{item['name']}{item['category']}" for token in tokens
                )

        merged = sorted(
            merged,
            key=lambda item: (
                0
                if item.get("_preference_exact")
                else 1
                if item.get("_preference_hit")
                else 2,
                item["_distance_m"],
            ),
        )
        _cache_set(key, merged, CACHE_TTL)
        return merged


def _snack_candidates(city, preference):
    key = ("snack", city, preference or "")
    cached = _cache_get(key)
    if cached is not None:
        return cached

    with keyed_lock.lock_for(("food", key)):
        cached = _cache_get(key)
        if cached is not None:
            return cached

        short_city = city[:-1] if city.endswith("市") else city
        keywords = [f"{short_city}特色小吃", "小吃"]
        if preference:
            keywords.append(preference)

        pages = amap_client.map_calls(
            lambda keyword: _fetch_text(city, keyword), keywords
        )

        items = []
        for keyword_index, pois in enumerate(pages):
            for position, poi in enumerate(pois):
                items.append(
                    _normalize(poi, None, "", keyword_index * PAGE_SIZE + position)
                )

        merged = _merge(items)
        snack_like = [
            item
            for item in merged
            if any(token in item["name"] for token in SNACK_NAME_TOKENS)
        ]
        pool_items = snack_like if len(snack_like) >= 3 else merged

        tokens = _preference_tokens(preference)
        if tokens:
            for item in pool_items:
                if any(
                    token in f"{item['name']}{item['category']}{item['address']}"
                    for token in tokens
                ):
                    item["_preference_hit"] = True

        ranked = sorted(
            pool_items,
            key=lambda item: (0 if item.get("_preference_hit") else 1, item["_order"]),
        )
        ranked = [item for item in ranked if item["rating"] >= MIN_RATING] or ranked
        _cache_set(key, ranked, SNACK_CACHE_TTL)
        return ranked


def _fetch_around(anchor, task):
    location = f"{anchor[0]},{anchor[1]}"
    try:
        data = amap_client.search_around(
            location,
            radius=task["radius"],
            types=FOOD_TYPES,
            keywords=task["keywords"],
            offset=PAGE_SIZE,
            page=task["page"],
        )
        return data.get("pois") or []
    except amap_client.AmapError:
        return []


def _fetch_text(city, keyword):
    try:
        data = amap_client.search_text(keyword, city=city, offset=PAGE_SIZE, page=1)
        return data.get("pois") or []
    except amap_client.AmapError:
        return []


def _resolve_anchor(city, location):
    """支持 "lng,lat" 或地点名；缺省用城市中心。"""
    if location:
        raw = str(location).replace("，", ",").strip()
        parts = raw.split(",")
        if len(parts) == 2:
            try:
                lng, lat = float(parts[0]), float(parts[1])
                if -180 <= lng <= 180 and -90 <= lat <= 90:
                    return (lng, lat), "当前位置"
            except ValueError:
                pass
        try:
            poi = amap_client.geocode(raw, city or None)
        except amap_client.AmapError as exc:
            raise FoodError(f"无法定位「{raw}」：{exc}") from exc
        return (poi["lng"], poi["lat"]), poi["name"] or raw

    if not city:
        raise FoodError("请提供城市或当前位置")
    try:
        district = amap_client.district(city)
    except amap_client.AmapError as exc:
        raise FoodError(f"无法定位城市「{city}」：{exc}") from exc
    lng, lat = district["center"].split(",")
    return (float(lng), float(lat)), "市中心"


def _normalize(poi, anchor, location_label, order=0):
    lng, lat = _location(poi)
    distance_m = poi.get("distance")
    if isinstance(distance_m, str):
        distance_m = float(distance_m) if distance_m.replace(".", "", 1).isdigit() else None
    elif not isinstance(distance_m, (int, float)):
        distance_m = None
    if distance_m is None:
        distance_m = _haversine(anchor, (lng, lat)) if anchor else None

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
        "distance_text": _distance_text(distance_m, location_label),
        "suitable_for": _suitable_for(poi, category),
        "_distance_m": distance_m if distance_m is not None else float("inf"),
        "_order": order,
    }


def _merge(items):
    merged = {}
    for item in items:
        if not item["name"]:
            continue
        current = merged.get(item["name"])
        if current is None or item["_distance_m"] < current["_distance_m"]:
            merged[item["name"]] = item
    return list(merged.values())


def _pick(candidates, exclude, limit):
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
    return picked


def _suitable_for(poi, category):
    text = f"{_text(poi.get('name'))}{category}"
    if any(word in text for word in SOLO_WORDS):
        return "适合1人"
    if any(word in text for word in GROUP_WORDS):
        return "适合多人"
    return "1人/多人均可"


def _distance_text(meters, label):
    if meters is None or math.isinf(meters):
        return ""
    prefix = "距你" if label == "当前位置" else f"距{label}"
    if meters < 1000:
        return f"{prefix} {int(round(meters))} 米"
    return f"{prefix} {meters / 1000:.1f} 公里"


def _haversine(start, end):
    lng1, lat1 = start
    lng2, lat2 = end
    radius = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _location(poi):
    parts = _text(poi.get("location")).split(",")
    if len(parts) == 2:
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            pass
    return 0.0, 0.0


def _first_photo(poi):
    for photo in poi.get("photos") or []:
        url = _text((photo or {}).get("url"))
        if url.startswith("http://"):
            return "https://" + url[len("http://"):]
        if url.startswith("https://"):
            return url
    return ""


def _preference_tokens(preference):
    text = (preference or "").strip()
    if not text:
        return []
    tokens = [token for token in re.split(r"[\s,，、/和与及]+", text) if len(token) >= 2]
    for key, hints in PREFERENCE_HINTS.items():
        if key in text:
            tokens.extend(hints)
    return tokens


def _normalize_mode(mode):
    return "snack" if str(mode or "").lower() in {"snack", "xiaochi", "specialty"} else "nearby"


def _cache_get(key):
    now = time.time()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < cached[1]:
            return cached[2]
        if cached:
            _cache.pop(key, None)
    return None


def _cache_set(key, value, ttl):
    now = time.time()
    with _cache_lock:
        _cache[key] = (now, ttl, value)
        if len(_cache) > MAX_CACHE:
            for expired in [k for k, (ts, t, _) in _cache.items() if now - ts >= t]:
                _cache.pop(expired, None)
            while len(_cache) > MAX_CACHE:
                oldest = min(_cache, key=lambda k: _cache[k][0])
                _cache.pop(oldest, None)


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _text(value):
    return "" if isinstance(value, list) else str(value or "").strip()
