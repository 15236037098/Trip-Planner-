"""景点项目与价格：通过本地技能服务（飞猪 flyai）检索门票/体验项目。

高内聚：只做请求、校验与格式化；
低耦合：走 HTTP 接口 /api/products，不依赖 CLI 与技能实现细节。
"""

from services import skill_service

# 需大于 Node 侧排队 + CLI 总预算（约 9s），小于会话预算 18s
TIMEOUT = 12
MAX_LIMIT = 10
PAYLOAD_KEYS = ("name", "price", "price_text", "image", "url", "tags")


class ProductError(Exception):
    """缺少景点名称或预订服务不可用。"""


def search(poi, city=None, limit=6):
    """检索指定景点的可预订项目，返回精简结构。"""
    poi = (poi or "").strip()
    if not poi:
        raise ProductError("缺少景点名称")

    params = {
        "poi": poi[:40],
        "limit": max(1, min(int(limit or 6), MAX_LIMIT)),
    }
    if city and city.strip():
        params["city"] = city.strip()[:20]

    try:
        data = skill_service.get_json("/api/products", params, timeout=TIMEOUT)
    except skill_service.SkillServiceError as exc:
        raise ProductError(str(exc)) from exc

    items = [
        item
        for item in (data.get("items") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    if not items:
        raise ProductError(f"暂时没有找到「{poi}」的可预订项目")

    return {
        "city": data.get("city") or (city or "").strip(),
        "poi": data.get("poi") or poi,
        "items": items,
        "total": data.get("total") or len(items),
        "system_message": data.get("system_message") or "",
        "source": data.get("source") or "fliggy",
    }


def render_summary(result):
    """把检索结果转成供大模型阅读的文本。"""
    items = result["items"]
    lines = [f"「{result['poi']}」可预订项目（{len(items)} 个，飞猪实时数据）："]
    for index, item in enumerate(items, 1):
        tags = "／".join(item.get("tags") or [])
        suffix = f"｜{tags}" if tags else ""
        lines.append(f"{index}. {item['name']}｜{item.get('price_text') or '价格以下单页为准'}{suffix}")
    lines.append("价格来自飞猪实时结果，以下单页为准；项目卡片已在页面展示。")
    return "\n".join(lines)


def to_payload(result):
    """转成前端渲染用的精简结构。"""
    return {
        "city": result["city"],
        "poi": result["poi"],
        "source": result["source"],
        "items": [
            {key: item.get(key) for key in PAYLOAD_KEYS if item.get(key) is not None}
            for item in result["items"]
        ],
    }
