"""高德地图 Web 服务 API 封装。

只负责 HTTP 请求与参数拼装，不包含路线规划业务逻辑，
供 services.route_planner 等上层模块复用。
"""

import logging
import math
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import requests

BASE_URL = "https://restapi.amap.com/v3"
TIMEOUT = 10
STATIC_MAP_SIZE = (750, 500)
MAX_PATH_POINTS = 50

logger = logging.getLogger("amap")
_SECRET_PATTERN = re.compile(r"(key=)[^&\s]+", re.IGNORECASE)


def _env_number(name, default, cast=float):
    try:
        return cast(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


# 高德 Web 服务有 QPS 与并发上限（个人 Key 通常 3 QPS），
# 所有请求统一经过下面两个闸门，避免并发爬取触发 CUQPS_HAS_EXCEEDED_THE_LIMIT。
GAODE_QPS = max(1.0, _env_number("GAODE_QPS", 3.0))
# 同时在飞数默认与 QPS 对齐：即使某个请求变慢，也不会叠出超过上限的并发
GAODE_MAX_INFLIGHT = max(1, _env_number("GAODE_MAX_INFLIGHT", 3, int))
# 进程级线程池：批量地理编码、距离矩阵共用，避免每次规划都新建线程池
AMAP_POOL_SIZE = max(1, _env_number("AMAP_POOL_SIZE", 8, int))


class _RateLimiter:
    """滑动窗口限速：任意 1 秒内放行的请求数不超过 qps。

    比固定间隔更严格——固定间隔在窗口边界仍可能放行 qps+1 个请求，正是
    CUQPS_HAS_EXCEEDED_THE_LIMIT 的常见触发场景。
    """

    def __init__(self, qps):
        self._qps = max(1, int(qps))
        self._lock = threading.Lock()
        self._starts = deque()

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                while self._starts and now - self._starts[0] >= 1.0:
                    self._starts.popleft()
                if len(self._starts) < self._qps:
                    self._starts.append(now)
                    return
                delay = 1.0 - (now - self._starts[0])
            time.sleep(max(delay, 0.01))


_limiter = _RateLimiter(GAODE_QPS)
_inflight = threading.BoundedSemaphore(GAODE_MAX_INFLIGHT)
_pool = ThreadPoolExecutor(max_workers=AMAP_POOL_SIZE, thread_name_prefix="amap")


class AmapError(Exception):
    """高德接口不可用或返回失败。"""


def _redact(text):
    return _SECRET_PATTERN.sub(r"\1***", str(text or ""))


def _perform(url, params):
    """在并发闸门与 QPS 限速下发起一次请求。"""
    with _inflight:
        _limiter.acquire()
        return requests.get(url, params=params, timeout=TIMEOUT)


def _request(path, payload):
    """请求 JSON 接口，返回 (数据, 异常)。"""
    try:
        response = _perform(f"{BASE_URL}/{path}", payload)
        response.raise_for_status()
        return response.json(), None
    except (requests.RequestException, ValueError) as exc:
        return None, exc


def _is_qps_error(data):
    return (
        isinstance(data, dict)
        and str(data.get("status")) != "1"
        and "QPS" in str(data.get("info") or "").upper()
    )


def _get(path, params):
    payload = {"key": os.getenv("GAODE_API_KEY"), **params}
    data, error = _request(path, payload)

    if _is_qps_error(data):
        # 命中限速时退避一个间隔再试一次，避免整批子任务同时失败
        logger.warning("高德接口触发 QPS 限制，退避后重试：%s", path)
        time.sleep(1.0 / GAODE_QPS)
        data, error = _request(path, payload)

    if error is not None:
        logger.warning("高德接口请求失败：%s", _redact(error))
        raise AmapError("高德接口暂时不可用，请稍后重试") from None

    if not isinstance(data, dict) or data.get("status") != "1":
        message = data.get("info", "未知错误") if isinstance(data, dict) else "返回格式异常"
        logger.warning("高德接口返回异常：%s", _redact(message))
        raise AmapError("高德接口暂时不可用，请稍后重试")
    return data


def static_image(params):
    """获取静态地图图片，复用同一套 QPS 限速。返回 (图片内容, content-type)。"""
    try:
        response = _perform(f"{BASE_URL}/staticmap", params)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("静态地图请求失败：%s", _redact(exc))
        raise AmapError("地图加载失败") from None
    return response.content, response.headers.get("content-type") or ""


def geocode(address, city=None):
    """把地点名称转换成经纬度坐标。

    优先按 POI 关键字搜索（景点名匹配更准），无结果时再按地址解析。
    """
    poi = _search_poi(address, city)
    if poi is not None:
        lng, lat = poi["location"].split(",")
        return {
            "name": address,
            "lng": float(lng),
            "lat": float(lat),
            "address": _as_text(poi.get("address")),
            "city": _as_text(poi.get("cityname")),
        }

    params = {"address": address}
    if city:
        params["city"] = city
    data = _get("geocode/geo", params)
    geocodes = data.get("geocodes") or []
    if not geocodes:
        raise AmapError(f"未找到地点：{address}")

    lng, lat = geocodes[0]["location"].split(",")
    return {
        "name": address,
        "lng": float(lng),
        "lat": float(lat),
        "address": geocodes[0].get("formatted_address", ""),
        "city": _as_text(geocodes[0].get("city")) or _as_text(geocodes[0].get("province")),
    }


def map_calls(func, items):
    """在共享线程池里并发执行高德批量调用。

    func 内只能发起高德请求，不能再向本池提交任务，否则可能因线程耗尽而互相等待。
    """
    items = list(items)
    if not items:
        return []
    return list(_pool.map(func, items))


def geocode_many(names, city=None):
    """并发解析多个地点，复用进程级线程池，避免每次路线规划都新建线程池。"""
    names = list(names or [])
    if not names:
        return []
    return map_calls(lambda name: geocode(name, city), names)


def _search_poi(keyword, city=None):
    params = {"keywords": keyword, "offset": 1, "page": 1}
    if city:
        params.update({"city": city, "citylimit": "true"})
    try:
        data = _get("place/text", params)
    except AmapError:
        return None
    pois = data.get("pois") or []
    return pois[0] if pois and pois[0].get("location") else None


def _as_text(value):
    if isinstance(value, list):
        return "".join(str(item) for item in value)
    return str(value or "")


def district(name):
    """查询城市行政区信息（含边界坐标与 adcode）。"""
    data = _get(
        "config/district",
        {"keywords": name, "subdistrict": 0, "extensions": "all"},
    )
    districts = data.get("districts") or []
    if not districts:
        raise AmapError(f"未找到城市：{name}")
    return districts[0]


def search_polygon(polygon, types=None, keywords=None, offset=25, page=1):
    """在矩形/多边形区域内检索 POI。polygon 形如 "lng1,lat1|lng2,lat2"。"""
    params = {"polygon": polygon, "offset": offset, "page": page, "extensions": "all"}
    if types:
        params["types"] = types
    if keywords:
        params["keywords"] = keywords
    return _get("place/polygon", params)


def search_text(keywords, city=None, offset=25, page=1):
    """按关键字检索 POI，可按城市限定范围。"""
    params = {"keywords": keywords, "offset": offset, "page": page, "extensions": "all"}
    if city:
        params.update({"city": city, "citylimit": "true"})
    return _get("place/text", params)


def search_around(
    location,
    radius=1000,
    types=None,
    keywords=None,
    offset=25,
    page=1,
    sortrule="distance",
):
    """按坐标检索周边 POI，默认按距离由近到远。"""
    params = {
        "location": location,
        "radius": radius,
        "offset": offset,
        "page": page,
        "extensions": "all",
        "sortrule": sortrule,
    }
    if types:
        params["types"] = types
    if keywords:
        params["keywords"] = keywords
    return _get("place/around", params)


def driving_matrix(points):
    """计算任意两点间的驾车距离（米）和耗时（秒）。

    返回 (distances, durations) 两个 n*n 矩阵，distances[i][j] 表示 i 到 j。
    """
    count = len(points)
    if count == 0:
        return [], []
    origins = "|".join(f"{p['lng']},{p['lat']}" for p in points)

    def fetch(dest_index, point):
        data = _get(
            "distance",
            {
                "origins": origins,
                "destination": f"{point['lng']},{point['lat']}",
                "type": "1",
            },
        )
        return dest_index, data.get("results", [])

    results = map_calls(lambda pair: fetch(pair[0], pair[1]), enumerate(points))

    distances = [[0] * count for _ in range(count)]
    durations = [[0] * count for _ in range(count)]
    for dest_index, items in results:
        for item in items:
            origin_index = int(item["origin_id"]) - 1
            distances[origin_index][dest_index] = int(item["distance"])
            durations[origin_index][dest_index] = int(item["duration"])

    return distances, durations


def driving_geometry(origin, destination, waypoints=None):
    """获取驾车路线的坐标序列，元素形如 "lng,lat"。

    传入途经点时一次请求即可覆盖整条路线，避免逐段调用拉长长路线的耗时。
    """
    params = {
        "origin": f"{origin['lng']},{origin['lat']}",
        "destination": f"{destination['lng']},{destination['lat']}",
        "extensions": "all",
        "strategy": "0",
    }
    points = [point for point in (waypoints or []) if point][:16]
    if points:
        params["waypoints"] = ";".join(f"{point['lng']},{point['lat']}" for point in points)

    data = _get("direction/driving", params)
    paths = (data.get("route") or {}).get("paths") or []
    if not paths:
        raise AmapError(f"未找到 {origin['name']} 到 {destination['name']} 的驾车路线")

    polyline = []
    for step in paths[0].get("steps", []):
        polyline.extend(point for point in step.get("polyline", "").split(";") if point)
    return polyline


def driving_route(origin, destination, waypoints=None):
    """获取驾车方案的距离、耗时与过路费，支持途经点。"""
    params = {
        "origin": f"{origin['lng']},{origin['lat']}",
        "destination": f"{destination['lng']},{destination['lat']}",
        "extensions": "all",
        "strategy": "0",
    }
    points = [point for point in (waypoints or []) if point][:16]
    if points:
        params["waypoints"] = ";".join(f"{point['lng']},{point['lat']}" for point in points)

    data = _get("direction/driving", params)
    paths = (data.get("route") or {}).get("paths") or []
    if not paths:
        raise AmapError(f"未找到 {origin['name']} 到 {destination['name']} 的驾车路线")

    path = paths[0]
    return {
        "distance_m": int(float(path.get("distance") or 0)),
        "duration_s": int(float(path.get("duration") or 0)),
        "tolls": float(path.get("tolls") or 0),
    }


def static_map_url(stops, polyline):
    """生成静态地图代理地址（不包含高德 Key，Key 由后端代理注入）。"""
    if not stops:
        raise AmapError("缺少地图标注点")

    (lng, lat), zoom = _center_and_zoom(stops)
    markers = "|".join(
        f"mid,0x764ba2,{_marker_label(index)}:{stop['lng']:.6f},{stop['lat']:.6f}"
        for index, stop in enumerate(stops)
    )

    params = {
        "location": f"{lng:.6f},{lat:.6f}",
        "zoom": str(zoom),
        "size": f"{STATIC_MAP_SIZE[0]}*{STATIC_MAP_SIZE[1]}",
        "scale": "2",
        "markers": markers,
    }
    if polyline:
        params["paths"] = "5,0x667eea,0.9,,:" + ";".join(_sample(polyline, MAX_PATH_POINTS))

    return f"/api/map?{urlencode(params)}"


def _center_and_zoom(stops):
    """按景点包围盒计算地图中心点与合适的缩放级别。"""
    lngs = [stop["lng"] for stop in stops]
    lats = [stop["lat"] for stop in stops]
    center = ((min(lngs) + max(lngs)) / 2, (min(lats) + max(lats)) / 2)

    if len(stops) == 1:
        return center, 15

    mid_lat = center[1]
    span_x = (max(lngs) - min(lngs)) * 111320 * math.cos(math.radians(mid_lat))
    span_y = (max(lats) - min(lats)) * 110540
    width, height = STATIC_MAP_SIZE
    meters_per_pixel = max(span_x / (width * 0.8), span_y / (height * 0.8), 1)

    zoom = math.log2(40075016.686 * math.cos(math.radians(mid_lat)) / (256 * meters_per_pixel))
    return center, max(3, min(17, int(zoom)))


def _marker_label(index):
    """用 A、B、C… 标记景点顺序。"""
    return chr(ord("A") + index) if index < 26 else str(index + 1)


def _sample(points, limit):
    """等间隔采样，控制静态地图 URL 长度。"""
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]
