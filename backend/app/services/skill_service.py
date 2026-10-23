"""本地技能服务（frontend Express）HTTP 客户端。

统一服务地址、超时与错误转换，供商品/房价/出行服务复用。
"""

import contextvars
import logging
import os

import requests

DEFAULT_BASE_URL = "http://127.0.0.1:3000"

# 当前请求的客户端标识，用于把限流维度透传给 Node 技能服务
CLIENT_ID = contextvars.ContextVar("client_id", default="")

logger = logging.getLogger("skill-service")


class SkillServiceError(Exception):
    """技能服务不可用或返回格式异常。"""


def base_url():
    """返回技能服务地址；兼容 Render 内网 host:port 形式。"""
    raw = (os.getenv("SKILL_SERVICE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    if raw.startswith(("http://", "https://")):
        return raw
    return f"http://{raw}"


def get_json(path, params, timeout=15):
    """请求技能服务并返回 JSON 对象。"""
    headers = {}
    client_id = CLIENT_ID.get()
    if client_id:
        headers["X-Client-Id"] = client_id

    try:
        response = requests.get(
            f"{base_url()}{path}", params=params, headers=headers, timeout=timeout
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.warning("技能服务请求失败：%s", type(exc).__name__)
        raise SkillServiceError("技能服务暂时不可用，请稍后重试") from None
    except ValueError:
        raise SkillServiceError("技能服务返回格式异常") from None

    if not isinstance(data, dict):
        raise SkillServiceError("技能服务返回格式异常")
    return data
