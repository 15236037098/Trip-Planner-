"""出行方式比价：火车/航班（飞猪）+ 自驾/大巴（高德与里程估算）。

高内聚：聚合多种交通方式，统一输出票价、总花费与耗时；
低耦合：火车/航班走本地技能服务 HTTP，自驾走高德封装，大巴仅按里程估算。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date as date_type
from datetime import datetime, timedelta

from services import amap_client, skill_service

TIMEOUT = 12
MAX_WAYPOINTS = 5
MAX_PEOPLE = 9
FUEL_PER_KM = 0.6
BUS_PER_KM = 0.28
CITY_BUS_PRICE = 5
MIN_BUS_PRICE = 20
MAX_BUS_KM = 1200
CITY_BUS_KM = 60


class TransportError(Exception):
    """缺少出发地/目的地，或所有出行方式都不可用。"""


# 驾车方案与技能服务查询复用进程级线程池，避免每次比价都新建线程
_IO_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="transport")


def plan_transport(origin, destination, city=None, waypoints=None, date=None, people=1):
    """比较多城市/景点之间的出行方式，返回价格、总花费与耗时。"""
    origin = (origin or "").strip()
    destination = (destination or "").strip()
    if not origin or not destination:
        raise TransportError("需要出发地和目的地才能比较出行方式")

    people = max(1, min(int(people or 1), MAX_PEOPLE))
    names = [name.strip() for name in (waypoints or []) if name and name.strip()][:MAX_WAYPOINTS]
    depart = _normalize_date(date)

    try:
        places = amap_client.map_calls(
            lambda pair: amap_client.geocode(pair[0], pair[1]),
            [(origin, None), (destination, city), *[(name, city) for name in names]],
        )
    except amap_client.AmapError as exc:
        raise TransportError(f"地点解析失败：{exc}") from exc

    origin_place, dest_place = places[0], places[1]
    waypoint_places = places[2:]

    origin_city = _city_name(origin_place.get("city")) or origin
    dest_city = _city_name(dest_place.get("city")) or _city_name(city) or destination

    drive_future = _IO_POOL.submit(
        amap_client.driving_route,
        origin_place,
        dest_place,
        waypoint_places,
    )
    # 同城出行不查火车/航班，避免出现城市内部的无效班次
    options_future = None
    if origin_city != dest_city:
        options_future = _IO_POOL.submit(_request_options, origin_city, dest_city, depart)

    drive = None
    drive_error = None
    try:
        drive = drive_future.result()
    except amap_client.AmapError as exc:
        drive_error = str(exc)

    options = None
    options_error = None
    if options_future is not None:
        try:
            options = options_future.result()
        except TransportError as exc:
            options_error = str(exc)

    if not drive and not options:
        raise TransportError(drive_error or options_error or "未能获取出行方案")

    modes = []
    if options:
        train = _best(options.get("trains"))
        if train:
            modes.append(_train_mode(train, people))
        flight = _best(options.get("flights"))
        if flight:
            modes.append(_flight_mode(flight, people))

    distance_km = (drive["distance_m"] / 1000) if drive else 0
    if drive:
        modes.append(_driving_mode(drive, people))
        bus = _bus_mode(distance_km, drive["duration_s"], people)
        if bus:
            modes.append(bus)

    if not modes:
        raise TransportError("未找到可用的出行方式")

    modes.sort(key=lambda item: item["total_price"])
    modes[0]["is_cheapest"] = True
    fastest = min(modes, key=lambda item: item["duration_s"])
    fastest["is_fastest"] = True

    notes = ["价格为交通费用，不含门票、餐饮与住宿"]
    if options_error:
        notes.append(f"火车/航班实时数据暂不可用：{options_error}")
    if drive_error:
        notes.append(f"驾车路线暂不可用：{drive_error}")
    if options:
        notes.extend(options.get("errors") or [])

    return {
        "origin": origin,
        "destination": destination,
        "destination_label": destination if dest_city in destination else f"{dest_city}{destination}",
        "origin_city": origin_city,
        "destination_city": dest_city,
        "waypoints": names,
        "date": depart,
        "people": people,
        "distance_km": round(distance_km, 1),
        "modes": modes,
        "cheapest": modes[0]["mode"],
        "fastest": fastest["mode"],
        "notes": notes,
    }


def render_summary(result):
    """把比价结果转成供大模型阅读的文本。"""
    lines = [
        f"从{result['origin']}到{result['destination_label']}的出行方案"
        f"（{result['people']}人，{result['date']} 出发，全程约 {result['distance_km']} 公里）："
    ]
    for index, mode in enumerate(result["modes"], 1):
        tags = []
        if mode.get("is_cheapest"):
            tags.append("最省")
        if mode.get("is_fastest"):
            tags.append("最快")
        tag = f"（{'、'.join(tags)}）" if tags else ""
        detail = f" {mode['detail']}" if mode.get("detail") else ""
        lines.append(
            f"{index}. {mode['label']}{tag}：{mode['price_text']}，"
            f"总花费 {mode['total_price_text']}，耗时约 {mode['duration_text']}。{detail}"
        )
    lines.append("；".join(result["notes"]) + "。")
    lines.append("出行卡片已展示在页面中，可直接查看详情与预订。")
    return "\n".join(lines)


def to_payload(result):
    """转成前端渲染用的结构。"""
    return {
        "origin": result["origin"],
        "destination": result["destination"],
        "destination_label": result["destination_label"],
        "origin_city": result["origin_city"],
        "destination_city": result["destination_city"],
        "waypoints": result["waypoints"],
        "date": result["date"],
        "people": result["people"],
        "distance_km": result["distance_km"],
        "modes": result["modes"],
        "cheapest": result["cheapest"],
        "fastest": result["fastest"],
        "notes": result["notes"],
    }


def _request_options(origin_city, dest_city, date):
    try:
        data = skill_service.get_json(
            "/api/transport",
            {"origin": origin_city, "destination": dest_city, "date": date},
            timeout=TIMEOUT,
        )
    except skill_service.SkillServiceError as exc:
        raise TransportError(str(exc)) from exc

    trains = data.get("trains")
    flights = data.get("flights")
    errors = data.get("errors")
    return {
        "trains": trains if isinstance(trains, list) else [],
        "flights": flights if isinstance(flights, list) else [],
        "errors": errors if isinstance(errors, list) else [],
    }


def _best(options):
    items = [item for item in (options or []) if item.get("price") and item.get("duration_min")]
    return items[0] if items else None


def _train_mode(item, people):
    price = item["price"]
    total = price * people
    detail = " · ".join(
        part
        for part in (
            item.get("no"),
            item.get("seat"),
            f"{item.get('dep_station')}→{item.get('arr_station')}",
        )
        if part
    )
    return {
        "mode": "train",
        "label": "高铁/动车" if item.get("direct", True) else "火车（含换乘）",
        "price": price,
        "price_text": f"¥{price:g}/人",
        "total_price": total,
        "total_price_text": f"¥{total:g}（{people}人）",
        "duration_s": item["duration_min"] * 60,
        "duration_text": _format_duration(item["duration_min"] * 60),
        "detail": detail,
        "depart_time": item.get("dep_time", ""),
        "url": item.get("url", ""),
        "estimated": False,
        "note": "飞猪实时票价，以下单页为准",
    }


def _flight_mode(item, people):
    price = item["price"]
    total = price * people
    detail = " · ".join(
        part
        for part in (
            item.get("no"),
            item.get("airline"),
            f"{item.get('dep_airport')}→{item.get('arr_airport')}",
        )
        if part
    )
    return {
        "mode": "flight",
        "label": "飞机",
        "price": price,
        "price_text": f"¥{price:g}/人",
        "total_price": total,
        "total_price_text": f"¥{total:g}（{people}人）",
        "duration_s": item["duration_min"] * 60,
        "duration_text": _format_duration(item["duration_min"] * 60),
        "detail": detail,
        "depart_time": item.get("dep_time", ""),
        "url": item.get("url", ""),
        "estimated": False,
        "note": "飞猪实时票价，以下单页为准",
    }


def _driving_mode(drive, people):
    km = drive["distance_m"] / 1000
    fuel = round(km * FUEL_PER_KM)
    tolls = round(drive["tolls"])
    total = fuel + tolls
    per_person = round(total / people) if people else total
    price_text = f"¥{per_person}/人（按{people}人分摊）" if people > 1 else f"¥{total}（油费+过路费）"
    return {
        "mode": "driving",
        "label": "自驾",
        "price": per_person,
        "price_text": price_text,
        "total_price": total,
        "total_price_text": f"¥{total}（整车）",
        "duration_s": drive["duration_s"],
        "duration_text": _format_duration(drive["duration_s"]),
        "detail": f"约 {km:.0f} 公里，油费约 ¥{fuel}、过路费约 ¥{tolls}",
        "depart_time": "",
        "url": "",
        "estimated": True,
        "note": "油费按 0.6 元/公里估算，不含停车费",
    }


def _bus_mode(km, drive_seconds, people):
    if not km:
        return None

    if km <= CITY_BUS_KM:
        price = CITY_BUS_PRICE
        label = "公交/地铁"
        # 市区公交按 15 公里/小时估算，另加 10 分钟候车
        duration_s = int(km * 4 * 60 + 600)
        note = "按城市公交票价估算，不含换乘等待"
    else:
        price = max(MIN_BUS_PRICE, round(km * BUS_PER_KM))
        label = "长途大巴"
        duration_s = int(drive_seconds * 1.5 + 1200)
        note = "按里程估算，班次与票价以车站为准"
        if km > MAX_BUS_KM:
            note = "超长途，通常需中转，建议优先高铁或飞机；" + note

    total = price * people
    return {
        "mode": "bus",
        "label": label,
        "price": price,
        "price_text": f"¥{price:g}/人（估算）",
        "total_price": total,
        "total_price_text": f"¥{total:g}（{people}人，估算）",
        "duration_s": duration_s,
        "duration_text": _format_duration(duration_s),
        "detail": f"约 {km:.0f} 公里",
        "depart_time": "",
        "url": "",
        "estimated": True,
        "note": note,
    }


def _format_duration(seconds):
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时 {rest} 分钟" if rest else f"{hours} 小时"


def _city_name(value):
    text = str(value or "").strip()
    return text[:-1] if text.endswith("市") else text


def _normalize_date(value):
    text = str(value or "").strip()
    if text:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d").date()
            if parsed >= date_type.today():
                return parsed.isoformat()
        except ValueError:
            pass
    return (date_type.today() + timedelta(days=1)).isoformat()
