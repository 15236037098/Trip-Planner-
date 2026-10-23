"""景点游玩路线规划。

流程：地理编码 → 驾车距离矩阵 → 最优顺序 → 路径几何 → 静态地图地址。
不依赖 Web 框架与 LangChain，可独立复用和测试。
"""

import copy
import logging
import threading
import time
from itertools import permutations

from services import amap_client, keyed_lock

MAX_STOPS = 8
MODE_LABELS = {"distance": "最短距离", "time": "最省时间"}
_TIME_MODE_ALIASES = {"time", "duration", "fastest", "least_time"}
CACHE_TTL = 600
MAX_CACHE = 100

logger = logging.getLogger("route")

_cache = {}
_cache_lock = threading.Lock()


class RoutePlanError(Exception):
    """路线规划失败（参数、地理编码或接口异常）。"""


def plan_route(attractions, city=None, mode="distance"):
    """按最优顺序规划景点路线，返回可直接交给前端渲染的结构化结果。"""
    names = [name.strip() for name in attractions if name and name.strip()]
    if len(names) < 2:
        raise RoutePlanError("至少需要两个景点才能规划路线")
    if len(names) > MAX_STOPS:
        raise RoutePlanError(f"一次最多规划 {MAX_STOPS} 个景点")

    mode = _normalize_mode(mode)
    cache_key = (tuple(names), city or "", mode)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with keyed_lock.lock_for(("route", cache_key)):
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            stops = amap_client.geocode_many(names, city)
            distances, durations = amap_client.driving_matrix(stops)
        except amap_client.AmapError as exc:
            raise RoutePlanError(str(exc)) from exc

        order = _optimal_order(durations if mode == "time" else distances)
        ordered_stops = [stops[index] for index in order]

        legs = [
            {
                "from": stops[order[k]]["name"],
                "to": stops[order[k + 1]]["name"],
                "distance_m": distances[order[k]][order[k + 1]],
                "duration_s": durations[order[k]][order[k + 1]],
            }
            for k in range(len(order) - 1)
        ]

        # 途经点一次请求取回整条路线几何，长路线从 2(N-1) 次调用降到 1 次
        polyline = []
        try:
            polyline = amap_client.driving_geometry(
                ordered_stops[0], ordered_stops[-1], ordered_stops[1:-1]
            )
        except amap_client.AmapError as exc:
            # 折线只用于地图展示，取不到时仍返回站点顺序与距离
            logger.warning("路线几何获取失败：%s", exc)

        map_url = amap_client.static_map_url(ordered_stops, polyline)

        result = {
            "mode": mode,
            "mode_label": MODE_LABELS[mode],
            "city": city or "",
            "stops": [
                {**stop, "order": index + 1}
                for index, stop in enumerate(ordered_stops)
            ],
            "legs": legs,
            "total_distance_m": sum(leg["distance_m"] for leg in legs),
            "total_duration_s": sum(leg["duration_s"] for leg in legs),
            "map_url": map_url,
        }
        _cache_set(cache_key, result)
        return result


def _cache_get(key):
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if entry and now - entry[0] < CACHE_TTL:
            return copy.deepcopy(entry[1])
        if entry:
            _cache.pop(key, None)
    return None


def _cache_set(key, value):
    now = time.time()
    with _cache_lock:
        _cache[key] = (now, copy.deepcopy(value))
        while len(_cache) > MAX_CACHE:
            oldest = min(_cache, key=lambda item: _cache[item][0])
            _cache.pop(oldest, None)


def render_summary(plan):
    """把规划结果转成供大模型阅读的简洁文本。"""
    lines = [
        f"已按「{plan['mode_label']}」规划 {len(plan['stops'])} 个景点的游玩顺序"
        f"（从 {plan['stops'][0]['name']} 出发）："
    ]
    lines.extend(f"{stop['order']}. {stop['name']}" for stop in plan["stops"])
    lines.append(
        f"全程约 {plan['total_distance_m'] / 1000:.1f} 公里，"
        f"驾车约 {format_duration(plan['total_duration_s'])}。"
    )
    lines.append("路线地图已展示在页面中。")
    return "\n".join(lines)


def format_duration(seconds):
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时 {minutes % 60} 分钟"


def _normalize_mode(mode):
    return "time" if str(mode or "").lower() in _TIME_MODE_ALIASES else "distance"


def _optimal_order(cost):
    """枚举所有顺序求最优解，固定第一个景点为出发点。"""
    count = len(cost)
    if count <= 2:
        return list(range(count))

    best_order = list(range(count))
    best_total = None
    for tail in permutations(range(1, count)):
        order = (0, *tail)
        total = sum(cost[order[k]][order[k + 1]] for k in range(count - 1))
        if best_total is None or total < best_total:
            best_order, best_total = list(order), total
    return best_order
