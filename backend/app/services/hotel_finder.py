"""酒店房价：通过本地技能服务（飞猪 flyai）检索酒店/民宿/青旅及真实房价。

高内聚：只做请求、校验与格式化；
低耦合：走 HTTP 接口 /api/hotels，不依赖 CLI 与技能实现细节。
"""

import re

from services import skill_service

# 需大于 Node 侧排队 + CLI 总预算（约 9s），小于会话预算 18s
TIMEOUT = 12
MAX_LIMIT = 10
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PAYLOAD_KEYS = (
    "name",
    "price",
    "price_text",
    "star",
    "score",
    "score_desc",
    "address",
    "image",
    "url",
    "tags",
    "review",
)


class HotelError(Exception):
    """缺少城市或房价服务不可用。"""


def search(city, hotel=None, preference=None, limit=6, check_in=None, check_out=None):
    """检索城市酒店或指定酒店的实时房价，返回精简结构。

    check_in / check_out 为可选的 YYYY-MM-DD 日期，未传时由技能服务按默认日期查询。
    """
    city = (city or "").strip()
    if not city:
        raise HotelError("缺少城市名称")

    params = {
        "city": city[:20],
        "limit": max(1, min(int(limit or 6), MAX_LIMIT)),
    }
    if hotel and hotel.strip():
        params["hotel"] = hotel.strip()[:40]
    elif preference and preference.strip():
        params["preference"] = preference.strip()[:20]

    for key, value in (("check_in", check_in), ("check_out", check_out)):
        text = str(value or "").strip()
        if _DATE_PATTERN.fullmatch(text):
            params[key] = text

    try:
        data = skill_service.get_json("/api/hotels", params, timeout=TIMEOUT)
    except skill_service.SkillServiceError as exc:
        raise HotelError(str(exc)) from exc

    items = [
        item
        for item in (data.get("items") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    if not items:
        raise HotelError(f"暂时没有找到{city}符合条件的酒店房价")

    return {
        "city": data.get("city") or city,
        "hotel": data.get("hotel") or (hotel or "").strip(),
        "preference": data.get("preference") or (preference or "").strip(),
        "check_in": str(data.get("check_in") or ""),
        "check_out": str(data.get("check_out") or ""),
        "fallback": bool(data.get("fallback")),
        "items": items,
        "total": data.get("total") or len(items),
        "system_message": data.get("system_message") or "",
        "source": data.get("source") or "fliggy",
    }


def render_summary(result):
    """把房价结果转成供大模型阅读的文本。"""
    items = result["items"]
    preference = f"，偏好：{result['preference']}" if result["preference"] else ""
    stay = ""
    if result.get("check_in"):
        stay = f"，{result['check_in']} 入住"
        if result.get("check_out"):
            stay += f"、{result['check_out']} 退房"
    if result.get("fallback"):
        title = (
            f"飞猪暂未收录「{result['hotel']}」，"
            f"以下是{result['city']}同类住宿实时起价（{len(items)} 家{preference}{stay}）："
        )
    else:
        title = f"{result['city']}酒店实时起价（{len(items)} 家{preference}{stay}，飞猪数据）："

    lines = [title]
    for index, item in enumerate(items, 1):
        parts = [
            item.get("name"),
            item.get("price_text") or "价格以下单页为准",
            item.get("star"),
            f"评分 {item['score']}" if item.get("score") else "",
        ]
        lines.append(f"{index}. " + "｜".join(part for part in parts if part))
    lines.append("房价与房态以下单页为准；酒店卡片已在页面展示，可点击查看详情并预订。")
    return "\n".join(lines)


def to_payload(result):
    """转成前端渲染用的精简结构。"""
    return {
        "city": result["city"],
        "hotel": result["hotel"],
        "preference": result["preference"],
        "check_in": result.get("check_in") or "",
        "check_out": result.get("check_out") or "",
        "fallback": bool(result.get("fallback")),
        "source": result["source"],
        "items": [
            {key: item.get(key) for key in PAYLOAD_KEYS if item.get(key) not in (None, "")}
            for item in result["items"]
        ],
    }
