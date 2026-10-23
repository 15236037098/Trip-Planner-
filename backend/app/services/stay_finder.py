"""住宿推荐：附近酒店、青年旅舍、民宿。

按坐标并发检索高德住宿类 POI，按类型均衡推荐；
高德不提供实时房价与房态，价格/房态统一提示电话确认。
仅用标准库，返回适合小程序渲染的 JSON。
"""

import math
import re
import threading
import time

from services import amap_client, keyed_lock

STAY_TYPES = "100000"  # 住宿服务
PAGE_SIZE = 25
MAX_LIMIT = 5
RADIUS = 3000
EXTRA_RADIUS = 5000
CACHE_TTL = 1800
MAX_CACHE = 300
CATEGORY_ORDER = ("青年旅舍", "酒店", "民宿")
SUB_POI_WORDS = ("大堂", "礼宾部", "停车场", "前台", "餐厅", "宴会厅", "会议室", "售票处")

_cache = {}
_cache_lock = threading.Lock()


class StayError(Exception):
    """缺少城市/位置或检索失败。"""


def recommend(city=None, location=None, preference=None, exclude=None, limit=3):
    """返回附近住宿，尽量覆盖青年旅舍、酒店、民宿。"""
    city = (city or "").strip()
    limit = max(1, min(int(limit or 3), MAX_LIMIT))
    anchor, location_label = _resolve_anchor(city, location)

    candidates = _nearby_candidates(anchor, location_label, preference)
    picked = _pick(candidates, exclude, limit)
    if not picked:
        raise StayError("没有找到更多符合条件的住宿，可以换个位置或放宽偏好")

    return {
        "city": city,
        "location_label": location_label,
        "preference": (preference or "").strip(),
        "stays": picked,
    }


def render_summary(result):
    """把推荐结果转成供大模型阅读的文本。"""
    items = result["stays"]
    place = result["location_label"] or "当前位置"
    lines = [f"{result['city']}{place}附近住宿推荐（{len(items)} 个）："]

    for index, item in enumerate(items, 1):
        parts = [item["name"], item["category"]]
        if item["rating"]:
            parts.append(f"评分 {item['rating']}")
        if item["distance_text"]:
            parts.append(item["distance_text"])
        parts.append(item["price_text"])
        lines.append(f"{index}. " + "｜".join(parts))
        details = [f"房型：{item['room_types']}", item["room_status"]]
        if item["tel"]:
            details.append(f"电话 {item['tel']}")
        if item["address"]:
            details.append(item["address"])
        lines.append("   " + "｜".join(details))

    lines.append("高德未提供实时房价与房态，价格和是否有房请以电话确认为准。")
    return "\n".join(lines)


def to_payload(result):
    """转成前端/小程序渲染用的精简结构。"""
    keys = (
        "name",
        "category",
        "rating",
        "star_text",
        "price_text",
        "room_types",
        "room_status",
        "tel",
        "address",
        "lng",
        "lat",
        "photo",
        "distance_text",
    )
    return {
        "city": result["city"],
        "location_label": result["location_label"],
        "preference": result["preference"],
        "stays": [{key: item[key] for key in keys} for item in result["stays"]],
    }


def _nearby_candidates(anchor, location_label, preference):
    key = ("stay", round(anchor[0], 3), round(anchor[1], 3), preference or "")
    cached = _cache_get(key)
    if cached is not None:
        return cached

    with keyed_lock.lock_for(("stay", key)):
        cached = _cache_get(key)
        if cached is not None:
            return cached

        tasks = [
            {"radius": RADIUS, "page": 1, "keywords": None},
            {"radius": RADIUS, "page": 2, "keywords": None},
            {"radius": RADIUS, "page": 3, "keywords": None},
            {"radius": EXTRA_RADIUS, "page": 1, "keywords": "青年旅舍"},
            {"radius": EXTRA_RADIUS, "page": 1, "keywords": "民宿"},
        ]

        results = amap_client.map_calls(
            lambda task: _fetch_around(anchor, task), tasks
        )

        merged = {}
        for pois in results:
            for poi in pois:
                item = _normalize(poi, anchor, location_label)
                if not item:
                    continue
                current = merged.get(item["name"])
                if current is None or item["_distance_m"] < current["_distance_m"]:
                    merged[item["name"]] = item

        items = list(merged.values())
        wanted = _preference_categories(preference)
        if wanted:
            filtered = [item for item in items if item["category"] in wanted]
            items = filtered or items

        items = sorted(items, key=_quality_key)
        _cache_set(key, items, CACHE_TTL)
        return items


def _fetch_around(anchor, task):
    try:
        data = amap_client.search_around(
            f"{anchor[0]},{anchor[1]}",
            radius=task["radius"],
            types=STAY_TYPES,
            keywords=task["keywords"],
            offset=PAGE_SIZE,
            page=task["page"],
        )
        return data.get("pois") or []
    except amap_client.AmapError:
        return []


def _pick(candidates, exclude, limit):
    excluded = {name.strip() for name in (exclude or []) if name and name.strip()}
    picked = []

    def add(item):
        if item["name"] in excluded:
            return False
        if any(item["name"] in chosen["name"] or chosen["name"] in item["name"] for chosen in picked):
            return False
        picked.append(item)
        return True

    for category in CATEGORY_ORDER:
        for item in candidates:
            if item["category"] == category and add(item):
                break
        if len(picked) >= limit:
            return picked

    for item in candidates:
        if len(picked) >= limit:
            break
        add(item)
    return picked


def _quality_key(item):
    """优先保证能打电话、有图片、评分可用的房源。"""
    return (
        0 if item["tel"] else 1,
        0 if item["photo"] else 1,
        0 if item["rating"] >= 3.5 else 1,
        item["_distance_m"],
    )


def _normalize(poi, anchor, location_label):
    name = _text(poi.get("name"))
    if not name or any(word in name for word in SUB_POI_WORDS):
        return None

    category = _category(poi)
    if category not in CATEGORY_ORDER:
        return None

    lng, lat = _location(poi)
    distance_m = poi.get("distance")
    if isinstance(distance_m, str):
        distance_m = float(distance_m) if distance_m.replace(".", "", 1).isdigit() else None
    elif not isinstance(distance_m, (int, float)):
        distance_m = None
    if distance_m is None:
        distance_m = _haversine(anchor, (lng, lat))

    biz = poi.get("biz_ext") or {}
    star = _text(biz.get("star"))

    return {
        "name": name,
        "category": category,
        "rating": _to_float(biz.get("rating")),
        "star_text": f"{star}星级" if star.isdigit() else "",
        "price_text": _price_text(biz),
        "room_types": _room_types(category),
        "room_status": "房态请电话确认",
        "tel": _text(poi.get("tel")).split(";")[0].strip(),
        "address": _text(poi.get("address")),
        "lng": lng,
        "lat": lat,
        "photo": _first_photo(poi),
        "distance_text": _distance_text(distance_m, location_label),
        "_distance_m": distance_m,
    }


def _category(poi):
    name = _text(poi.get("name"))
    type_text = _text(poi.get("type"))
    text = f"{name}{type_text}"

    if "青年旅舍" in text or "青旅" in name:
        return "青年旅舍"
    if "民宿" in text or "客栈" in text:
        return "民宿"
    if (
        "宾馆酒店" in type_text
        or "旅馆招待所" in type_text
        or any(word in name for word in ("酒店", "宾馆", "饭店", "旅馆"))
    ):
        return "酒店"
    return "其他住宿"


def _price_text(biz):
    for key in ("lowest_price", "cost"):
        value = biz.get(key)
        numbers = re.findall(r"\d+", _text(value) if not isinstance(value, list) else "")
        if numbers and int(numbers[0]) > 0:
            return f"¥{numbers[0]} 起"
    return "价格需电话咨询"


def _room_types(category):
    if category == "青年旅舍":
        return "床位/单人间/双人间（可询）"
    return "单人间/双人间（可询）"


def _preference_categories(preference):
    text = preference or ""
    categories = set()
    if any(word in text for word in ("青年旅舍", "青旅", "床位", "便宜", "经济", "穷游")):
        categories.add("青年旅舍")
    if "民宿" in text or "客栈" in text:
        categories.add("民宿")
    if "酒店" in text or "宾馆" in text or "饭店" in text:
        categories.add("酒店")
    return categories


def _resolve_anchor(city, location):
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
            raise StayError(f"无法定位「{raw}」：{exc}") from exc
        return (poi["lng"], poi["lat"]), poi["name"] or raw

    if not city:
        raise StayError("请提供城市或当前位置")
    try:
        district = amap_client.district(city)
    except amap_client.AmapError as exc:
        raise StayError(f"无法定位城市「{city}」：{exc}") from exc
    lng, lat = district["center"].split(",")
    return (float(lng), float(lat)), "市中心"


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
