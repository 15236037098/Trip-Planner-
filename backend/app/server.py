import asyncio
import contextvars
import copy
import logging
import os
import re
import threading
import time
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import List, Optional

import requests
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import ToolMessage
from langchain_core.pydantic_v1 import BaseModel, Field
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel as PydanticBaseModel
from pydantic import Field as PydanticField

from services import (
    amap_client,
    attraction_finder,
    food_finder,
    hotel_finder,
    product_finder,
    route_planner,
    skill_service,
    stay_finder,
    transport_finder,
)

_ENV_CANDIDATES = (
    os.getenv("ENV_FILE"),
    os.path.join(os.path.dirname(__file__), "..", "..", ".env"),
    os.path.join(os.path.dirname(__file__), "config", ".env"),
)
for _env_file in _ENV_CANDIDATES:
    if _env_file and os.path.exists(_env_file):
        load_dotenv(_env_file)
        break

GAODE_API_KEY = os.getenv("GAODE_API_KEY")

_MAP_LOCATION = re.compile(r"^-?\d{1,3}(\.\d+)?,-?\d{1,3}(\.\d+)?$")
_MAP_ZOOM = re.compile(r"^\d{1,2}$")
_MAP_SAFE = re.compile(r"^[0-9a-zA-Z,;:|.\-]+$")

logger = logging.getLogger("trip-planner")

RATE_LIMIT = 15  # 每个客户端每分钟最多请求数
IP_RATE_LIMIT = int(os.getenv("IP_RATE_LIMIT", "300"))  # 每个 IP 的兜底上限
RATE_LIMIT_REPLIES = {
    "zh-CN": "请求过于频繁，请稍后再试。",
    "en": "Too many requests. Please try again shortly.",
    "ja": "リクエストが多すぎます。しばらくしてから再試行してください。",
    "ko": "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
    "es": "Demasiadas solicitudes. Inténtalo de nuevo en un momento.",
    "fr": "Trop de demandes. Veuillez réessayer dans un instant.",
}
RATE_WINDOW = 60
_rate_buckets = defaultdict(deque)
_rate_lock = threading.Lock()


class WeatherInput(BaseModel):
    """fetch_weather 的入参约束"""
    city: str = Field(
        ...,
        description="要查询的城市中文名称，如'北京'、'上海'",
        min_length=2,
        max_length=20,
        regex=r"^[\u4e00-\u9fa5]+$",
    )
    need_forecast: bool = Field(
        default=True,
        description="是否需要未来天气预报",
    )


@tool(args_schema=WeatherInput)
def fetch_weather(city: str, need_forecast: bool = True) -> str:
    """查询指定城市的实时天气和未来天气预报。当用户询问天气时调用此工具。
    """
    url = "https://restapi.amap.com/v3/weather/weatherInfo"
    params = {
        "key": GAODE_API_KEY,
        "city": city,
        "extensions": "all" if need_forecast else "base",
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return "天气查询暂时不可用，请稍后重试。"

    if not isinstance(data, dict) or str(data.get("status")) != "1":
        info = data.get("info", "未知错误") if isinstance(data, dict) else "未知错误"
        return f"查询失败：{info}"

    lives = data.get("lives") or []
    forecasts = data.get("forecasts") or []
    result_parts = []

    if lives:
        live = lives[0]
        result_parts.append(
            f"【{live.get('city', city)} 实时天气】\n"
            f"天气：{live.get('weather', '未知')}\n"
            f"温度：{live.get('temperature', '未知')}°C\n"
            f"风向：{live.get('winddirection', '未知')}\n"
            f"风力：{live.get('windpower', '未知')}级\n"
            f"湿度：{live.get('humidity', '未知')}%"
        )

    if forecasts:
        forecast = forecasts[0]
        result_parts.append(f"\n【{forecast.get('city', city)} 天气预报】")
        for day in forecast.get("casts") or []:
            result_parts.append(
                f"{day.get('date', '')} | {day.get('dayweather', '')} | "
                f"{day.get('nighttemp', '')}°C ~ {day.get('daytemp', '')}°C | "
                f"{day.get('daywind', '')}风 {day.get('daypower', '')}级"
            )

    return "\n".join(result_parts) or "暂时没有查到天气数据。"


class RouteInput(BaseModel):
    """plan_trip_route 的入参约束"""
    attractions: List[str] = Field(
        ...,
        description="要游玩的景点名称列表，按用户提及顺序排列，2-8 个",
    )
    city: Optional[str] = Field(
        default=None,
        description="景点所在城市，如'上海'；用户未提城市时不传",
    )
    mode: Optional[str] = Field(
        default="distance",
        description="规划目标：distance=总距离最短；time=总耗时最少",
    )


def _build_route_plan(attractions, city=None, mode="distance"):
    """执行路线规划，返回 (给模型的文本, 给前端的地图数据)。"""
    try:
        plan = route_planner.plan_route(attractions, city=city, mode=mode or "distance")
    except route_planner.RoutePlanError as exc:
        return f"路线规划失败：{exc}", None
    return route_planner.render_summary(plan), plan


@tool(args_schema=RouteInput)
def plan_trip_route(
    attractions: List[str],
    city: Optional[str] = None,
    mode: Optional[str] = "distance",
):
    """规划多个景点的游玩路线。当用户想去多个景点、询问游玩顺序或路线时调用。
    输入景点名称列表，返回最优游览顺序、全程距离和耗时，并在页面地图上展示路线。
    """
    return _build_route_plan(attractions, city, mode)[0]


def execute_plan_trip_route(args):
    """按 schema 校验模型入参后执行路线规划。"""
    payload = RouteInput(**args)
    return _build_route_plan(payload.attractions, payload.city, payload.mode)


class AttractionInput(BaseModel):
    """recommend_attractions 的入参约束"""
    city: str = Field(..., description="城市中文名称，如'上海'、'杭州'")
    preference: Optional[str] = Field(
        default=None,
        description="用户偏好，如'亲子'、'历史人文'、'自然风光'、'夜景'；用户没说就不传",
    )
    exclude: Optional[List[str]] = Field(
        default=None,
        description="用户已看过、本次不希望重复的景点名称",
    )
    limit: Optional[int] = Field(default=3, description="推荐数量，默认 3，最多 5")


def _build_attraction_recommendation(city, preference=None, exclude=None, limit=3):
    """执行景点推荐，返回 (给模型的文本, 给前端的图片数据)。"""
    try:
        result = attraction_finder.recommend(
            city,
            preference=preference,
            exclude=exclude,
            limit=limit or 3,
        )
    except attraction_finder.AttractionError as exc:
        return f"景点推荐失败：{exc}", None
    return attraction_finder.render_summary(result), attraction_finder.to_payload(result)


@tool(args_schema=AttractionInput)
def recommend_attractions(
    city: str,
    preference: Optional[str] = None,
    exclude: Optional[List[str]] = None,
    limit: Optional[int] = 3,
):
    """按城市推荐热门景点。当用户询问某城市有什么景点、值得去哪玩时调用。
    返回景点名称、评分、类型、地址和实景图片；用户不满意时带上 preference 与 exclude 再次推荐。
    """
    return _build_attraction_recommendation(city, preference, exclude, limit)[0]


def execute_recommend_attractions(args):
    """按 schema 校验模型入参后执行景点推荐。"""
    payload = AttractionInput(**args)
    return _build_attraction_recommendation(
        payload.city,
        payload.preference,
        payload.exclude,
        payload.limit,
    )


class FoodInput(BaseModel):
    """recommend_food 的入参约束"""
    city: Optional[str] = Field(default=None, description="城市中文名称，如'上海'")
    location: Optional[str] = Field(
        default=None,
        description="当前位置，支持坐标 'lng,lat' 或地点名，如'人民广场'；用户没提供就不传",
    )
    mode: Optional[str] = Field(
        default="nearby",
        description="nearby=附近餐馆；snack=城市特色小吃",
    )
    preference: Optional[str] = Field(
        default=None,
        description="用户偏好，如'川菜'、'面食'、'清淡'、'夜宵'；人数由系统自动标注，不要传",
    )
    exclude: Optional[List[str]] = Field(
        default=None,
        description="此前已推荐过、本次不希望重复的店名",
    )
    limit: Optional[int] = Field(default=3, description="推荐数量，默认 3，最多 5")


def _build_food_recommendation(
    city=None,
    location=None,
    mode="nearby",
    preference=None,
    exclude=None,
    limit=3,
):
    """执行餐饮推荐，返回 (给模型的文本, 给前端的图片数据)。"""
    try:
        result = food_finder.recommend(
            city=city,
            location=location,
            mode=mode,
            preference=preference,
            exclude=exclude,
            limit=limit or 3,
        )
    except food_finder.FoodError as exc:
        return f"餐饮推荐失败：{exc}", None
    return food_finder.render_summary(result), food_finder.to_payload(result)


@tool(args_schema=FoodInput)
def recommend_food(
    city: Optional[str] = None,
    location: Optional[str] = None,
    mode: Optional[str] = "nearby",
    preference: Optional[str] = None,
    exclude: Optional[List[str]] = None,
    limit: Optional[int] = 3,
):
    """推荐餐馆或城市特色小吃。当用户问附近有什么好吃的、推荐餐厅、当地小吃去哪吃时调用。
    mode=nearby 按位置就近推荐餐馆并标注适合 1 人还是多人；
    mode=snack 推荐城市特色小吃。返回店名、评分、距离、适合人数、地址和图片。
    """
    return _build_food_recommendation(city, location, mode, preference, exclude, limit)[0]


def execute_recommend_food(args):
    """按 schema 校验模型入参后执行餐饮推荐。"""
    payload = FoodInput(**args)
    return _build_food_recommendation(
        payload.city,
        payload.location,
        payload.mode,
        payload.preference,
        payload.exclude,
        payload.limit,
    )


class StayInput(BaseModel):
    """recommend_stay 的入参约束"""
    city: Optional[str] = Field(default=None, description="城市中文名称，如'上海'")
    location: Optional[str] = Field(
        default=None,
        description="当前位置，支持坐标 'lng,lat' 或地点名，如'人民广场'；用户没提供就不传",
    )
    preference: Optional[str] = Field(
        default=None,
        description="住宿偏好，如'青年旅舍'、'民宿'、'酒店'、'便宜'；用户没说就不传",
    )
    exclude: Optional[List[str]] = Field(
        default=None,
        description="此前已推荐过、本次不希望重复的住宿名称",
    )
    limit: Optional[int] = Field(default=3, description="推荐数量，默认 3，最多 5")


def _build_stay_recommendation(
    city=None,
    location=None,
    preference=None,
    exclude=None,
    limit=3,
):
    """执行住宿推荐，返回 (给模型的文本, 给前端的图片数据)。"""
    try:
        result = stay_finder.recommend(
            city=city,
            location=location,
            preference=preference,
            exclude=exclude,
            limit=limit or 3,
        )
    except stay_finder.StayError as exc:
        return f"住宿推荐失败：{exc}", None
    return stay_finder.render_summary(result), stay_finder.to_payload(result)


@tool(args_schema=StayInput)
def recommend_stay(
    city: Optional[str] = None,
    location: Optional[str] = None,
    preference: Optional[str] = None,
    exclude: Optional[List[str]] = None,
    limit: Optional[int] = 3,
):
    """推荐附近的住宿，包括青年旅舍、酒店、民宿。
    当用户询问住哪里、酒店/民宿/青年旅舍推荐时调用。
    返回名称、类型、评分、距离、房型参考、联系电话、地址和图片；
    高德没有实时房价与房态，价格与是否有房需电话确认。
    """
    return _build_stay_recommendation(city, location, preference, exclude, limit)[0]


def execute_recommend_stay(args):
    """按 schema 校验模型入参后执行住宿推荐。"""
    payload = StayInput(**args)
    return _build_stay_recommendation(
        payload.city,
        payload.location,
        payload.preference,
        payload.exclude,
        payload.limit,
    )


class HotelDealInput(BaseModel):
    """recommend_hotel_deals 的入参约束"""
    city: str = Field(..., description="城市中文名称，如'上海'、'杭州'")
    preference: Optional[str] = Field(
        default=None,
        description="住宿偏好：便宜、高端、舒适、民宿、青旅；用户没说就不传",
    )
    hotel: Optional[str] = Field(
        default=None,
        description="具体酒店名称，用户点名某家酒店时才传",
    )
    check_in: Optional[str] = Field(
        default=None,
        description="入住日期，格式 YYYY-MM-DD；用户没说就不传，系统默认明天",
    )
    check_out: Optional[str] = Field(
        default=None,
        description="退房日期，格式 YYYY-MM-DD；用户没说就不传，系统默认入住次日",
    )
    limit: Optional[int] = Field(default=6, description="返回数量，默认 6，最多 10")


def _build_hotel_deals(
    city,
    preference=None,
    hotel=None,
    check_in=None,
    check_out=None,
    limit=6,
):
    """检索酒店实时房价，返回 (给模型的文本, 给前端的房价数据)。"""
    try:
        result = hotel_finder.search(
            city,
            hotel=hotel,
            preference=preference,
            check_in=check_in,
            check_out=check_out,
            limit=limit or 6,
        )
    except hotel_finder.HotelError as exc:
        return f"酒店房价检索失败：{exc}", None
    return hotel_finder.render_summary(result), hotel_finder.to_payload(result)


@tool(args_schema=HotelDealInput)
def recommend_hotel_deals(
    city: str,
    preference: Optional[str] = None,
    hotel: Optional[str] = None,
    check_in: Optional[str] = None,
    check_out: Optional[str] = None,
    limit: Optional[int] = 6,
):
    """按城市检索酒店/民宿/青旅的实时房价（数据来自飞猪）。
    当用户询问便宜/高端/舒适的酒店、预算、房价或想直接订酒店时调用；
    preference 传'便宜'、'高端'、'舒适'、'民宿'、'青旅'；
    用户点名某家酒店时传 hotel；
    用户提到入住/退房日期时按 YYYY-MM-DD 传 check_in、check_out，没说就不传；
    只问附近住宿且需要电话、距离时，应使用 recommend_stay。
    """
    return _build_hotel_deals(city, preference, hotel, check_in, check_out, limit)[0]


def execute_recommend_hotel_deals(args):
    """按 schema 校验模型入参后执行酒店房价检索。"""
    payload = HotelDealInput(**args)
    return _build_hotel_deals(
        payload.city,
        payload.preference,
        payload.hotel,
        payload.check_in,
        payload.check_out,
        payload.limit,
    )


class ProductInput(BaseModel):
    """recommend_products 的入参约束"""
    poi: str = Field(..., description="具体景点名称，如'上海迪士尼'、'东方明珠'、'外滩'")
    city: Optional[str] = Field(
        default=None,
        description="景点所在城市，如'上海'；从上下文已知时传入",
    )
    limit: Optional[int] = Field(default=6, description="返回项目数量，默认 6，最多 10")


def _build_products(poi, city=None, limit=6):
    """检索景点可预订项目，返回 (给模型的文本, 给前端的项目数据)。"""
    try:
        result = product_finder.search(poi, city=city, limit=limit or 6)
    except product_finder.ProductError as exc:
        return f"项目检索失败：{exc}", None
    return product_finder.render_summary(result), product_finder.to_payload(result)


@tool(args_schema=ProductInput)
def recommend_products(
    poi: str,
    city: Optional[str] = None,
    limit: Optional[int] = 6,
):
    """检索某个具体景点的可预订项目、门票和价格（数据来自飞猪）。
    当用户说出具体景点并询问门票、项目、价格、怎么玩时调用；
    只说了城市、还没说具体景点时，应先推荐景点，不要调用本工具。
    返回项目名称、价格、图片和预订链接。
    """
    return _build_products(poi, city, limit)[0]


def execute_recommend_products(args):
    """按 schema 校验模型入参后执行项目检索。"""
    payload = ProductInput(**args)
    return _build_products(payload.poi, payload.city, payload.limit)


class TransportInput(BaseModel):
    """plan_transport 的入参约束"""
    origin: str = Field(..., description="出发城市或地点，如'上海'、'人民广场'")
    destination: str = Field(..., description="目的地城市或景点，如'杭州'、'西湖'")
    city: Optional[str] = Field(
        default=None,
        description="目的地所在城市；destination 是景点时尽量传入，如 destination='西湖' 时传'杭州'",
    )
    waypoints: Optional[List[str]] = Field(
        default=None,
        description="途经景点名称列表，按游玩顺序排列；用户没提就不传",
    )
    date: Optional[str] = Field(
        default=None,
        description="出发日期，格式 YYYY-MM-DD；用户没说就不传，系统默认明天",
    )
    people: Optional[int] = Field(default=1, description="出行人数，默认 1")


def _build_transport(
    origin,
    destination,
    city=None,
    waypoints=None,
    date=None,
    people=1,
):
    """比较出行方式，返回 (给模型的文本, 给前端的卡片数据)。"""
    try:
        result = transport_finder.plan_transport(
            origin,
            destination,
            city=city,
            waypoints=waypoints,
            date=date,
            people=people or 1,
        )
    except transport_finder.TransportError as exc:
        return f"出行方案查询失败：{exc}", None
    return transport_finder.render_summary(result), transport_finder.to_payload(result)


@tool(args_schema=TransportInput)
def plan_transport(
    origin: str,
    destination: str,
    city: Optional[str] = None,
    waypoints: Optional[List[str]] = None,
    date: Optional[str] = None,
    people: Optional[int] = 1,
):
    """比较飞机、高铁/动车、自驾、大巴/公交的价格、总花费和耗时。
    当用户询问怎么去某城市/景点、坐什么交通、票价多少、要多久时调用。
    返回每种方式的单人价、总花费、耗时和预订链接。
    """
    return _build_transport(origin, destination, city, waypoints, date, people)[0]


def execute_plan_transport(args):
    """按 schema 校验模型入参后执行出行方式比较。"""
    payload = TransportInput(**args)
    return _build_transport(
        payload.origin,
        payload.destination,
        payload.city,
        payload.waypoints,
        payload.date,
        payload.people,
    )


CHAT_BUDGET_SECONDS = float(os.getenv("CHAT_BUDGET_SECONDS", "18"))
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "8"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "0"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "800"))
MAX_CONCURRENT_LLM = int(os.getenv("MAX_CONCURRENT_LLM", "200"))
MAX_CONCURRENT_TOOLS = int(os.getenv("MAX_CONCURRENT_TOOLS", "40"))
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "3"))
RESPONSE_CACHE_TTL = int(os.getenv("RESPONSE_CACHE_TTL", "120"))
RESPONSE_CACHE_SIZE = int(os.getenv("RESPONSE_CACHE_SIZE", "512"))

model = ChatOpenAI(
    model=os.getenv("MODEL") or "gpt-4o-mini",
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url=os.getenv("URL"),
    temperature=0.7,
    timeout=LLM_TIMEOUT_SECONDS,
    max_retries=LLM_MAX_RETRIES,
    max_tokens=LLM_MAX_TOKENS,
)

model_with_tools = model.bind_tools(
    [
        fetch_weather,
        plan_trip_route,
        recommend_attractions,
        recommend_food,
        recommend_stay,
        recommend_hotel_deals,
        recommend_products,
        plan_transport,
    ]
)

# 进程内并发闸门：LLM 与工具分别限流，避免上游限流把线程/连接打满
_LLM_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_LLM)
_TOOL_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_TOOLS)
_TOOL_POOL = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_TOOLS, thread_name_prefix="tool"
)

SYSTEM_PROMPT = """你是一个旅行助手。规则如下：
1. 如果用户询问天气，调用 fetch_weather 工具获取真实数据后回答。
2. 如果用户想去多个景点、询问游玩顺序或路线，调用 plan_trip_route 工具：
   attractions 按用户提及顺序传入全部景点名称；用户提到城市时同时传 city；
   用户要求"最省时间/最快"时 mode 传 time，否则默认 distance。
3. 如果用户询问某城市有什么景点、热门景点推荐，调用 recommend_attractions 工具，
   默认推荐 3 个；推荐后主动询问用户是否满意；
   用户不满意时先询问偏好（如亲子、历史人文、自然风光、夜景），
   拿到偏好后再调用工具（preference 传偏好，exclude 传此前已推荐的景点名称）。
4. 如果用户询问附近餐馆、好吃的、当地小吃，调用 recommend_food 工具：
   找附近餐馆时 mode 传 nearby，用户给出当前位置（坐标或地点名）时传 location，否则用 city；
   想吃城市特色小吃时 mode 传 snack，必须传 city；每次默认 3 个，除非用户明确要多几个。
   推荐后询问是否满意；
   如果用户已经看过推荐、又说"再推荐/换一批"但未给出偏好，禁止直接调用工具，必须先询问偏好
   （口味/面食/甜点/夜宵）；拿到偏好后才再次调用，
   preference 传偏好，exclude 传此前已推荐过的店名；
   人数不用作为偏好传入，系统会自动标注"适合1人/适合多人"。
5. 住宿分两种场景：
   a) 找附近住宿、需要电话/距离、青年旅舍或民宿，调用 recommend_stay 工具：
      用户给出当前位置时传 location，否则传 city；默认 3 个，
      优先覆盖青年旅舍、酒店、民宿各一家；
      高德不提供实时房价和房态，价格只能表述为"需电话咨询"，
      不得编造价格或承诺有房，要提醒用户电话确认房态；
      推荐后询问是否满意；用户说"还有别的吗"时带 exclude（已推荐的住宿名）再次调用。
   b) 问便宜/高端/舒适的酒店、预算、房价，或想直接订酒店、点名某家酒店，
      调用 recommend_hotel_deals 工具：city 必传；
      preference 传"便宜"、"高端"、"舒适"、"民宿"、"青旅"，用户没说就不传；
      用户点名某家酒店时传 hotel；默认 6 家；
      用户提到入住/退房日期（如"12月5日到12月7日"）时，必须换算成 YYYY-MM-DD 后传
      check_in、check_out；用户没说日期才省略；
      房价与房态来自飞猪实时数据，必须注明以下单页为准，不得编造；
      回复中要说明价格对应的入住日期（未指定日期时系统按明天入住查询），价格是起价；
      卡片已在页面展示，回答控制在100字内并提示点击卡片查看详情预订；
      用户说"还有别的吗"时可换偏好或增加 limit 再次调用。
6. 当用户询问怎么去某城市/景点、坐什么交通、票价、耗时、总花费时，调用 plan_transport 工具：
   origin 传出发城市或地点；用户没说出发地时，先用一句话询问出发地，不要猜；
   destination 传目的地景点或城市；目的地是景点时尽量传 city（如"西湖"配"杭州"）；
   用户提到途经景点时传 waypoints（按顺序）；人数大于1时传 people；
   日期未知时不要编造，系统会按明天查询；
   火车/航班价格与时刻来自飞猪实时数据，自驾油费和大巴票价是估算，必须注明；
   卡片已在页面展示，回答控制在150字内，点明最省和最快的方案及总花费。
7. 当用户提到具体景点（如"上海迪士尼""东方明珠""外滩"）并询问门票、项目、价格或怎么玩时，
   调用 recommend_products 工具：poi 传景点名称，city 从上下文补全；
   项目与价格来自飞猪实时数据，不得编造，需说明以下单页为准；
   项目卡片已在页面展示，回答控制在100字以内，并提示点击卡片可查看详情与预订。
   如果用户只说了城市、还没说具体景点，先调用 recommend_attractions 推荐景点，
   并提示"点击景点卡片可查看门票和项目"，此时不要调用 recommend_products。
8. 如果用户问其他问题，直接回答，不要调用工具。
9. 回答简洁友好，控制在200字以内；路线已在地图中展示，不必重复罗列细节。"""

# 支持的回答语言，键为前端传入的语言代码
SUPPORTED_LANGUAGES = {
    "zh-CN": "简体中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "es": "Español",
    "fr": "Français",
}


def _normalize_language(value: Optional[str]) -> str:
    """把前端传入的语言代码归一化为受支持的代码，未知语言回退到简体中文。"""
    code = (value or "").strip().lower().replace("_", "-")
    if not code:
        return "zh-CN"
    if code.startswith("zh"):
        return "zh-CN"
    base = code.split("-")[0]
    return base if base in SUPPORTED_LANGUAGES else "zh-CN"


def _build_system_prompt(language: str) -> str:
    """在基础提示词上追加日期与语言约束，保证回复语言与界面语言一致。"""
    prompt = (
        SYSTEM_PROMPT
        + f"\n\n当前日期：{date.today().isoformat()}。"
        "把用户说的“明天、后天、下周末、12月5日”等换算成日期时以此为准，"
        "日期一律写成 YYYY-MM-DD。"
    )
    if language == "zh-CN":
        return prompt

    name = SUPPORTED_LANGUAGES[language]
    return (
        prompt
        + f"""

10. 界面语言设置为{name}。你的所有回复必须全程使用{name}，包括追问、推荐理由、提醒和错误说明；
    即使用户用中文提问，也继续用{name}回答。
11. 调用工具前，先把用户提到的城市、景点、酒店、餐馆等名称转换成中文常用名再传参
    （例如 Beijing→北京，The Bund→外滩，West Lake→西湖）。
12. 工具返回的内容是中文，请在回复中翻译成{name}说明；景点、酒店、车站等专有名词
    首次出现时可附中文原名，方便现场问路、打车和出示给工作人员。
"""
    )


app = FastAPI(title="Trip Planner API")

ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """按客户端 IP 做简单的每分钟限流，避免额度与线程被滥用。"""
    if request.url.path in ("/docs", "/openapi.json", "/redoc", "/map"):
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)

    now = time.time()
    forwarded = request.headers.get("x-forwarded-for", "")
    client_ip = (
        forwarded.split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    client_id = (request.headers.get("x-client-id") or "").strip()[:64]

    checks = []
    if client_id:
        checks.append((f"client:{client_id}", RATE_LIMIT))
        checks.append((f"ip:{client_ip}", IP_RATE_LIMIT))
    else:
        checks.append((f"ip:{client_ip}", RATE_LIMIT))

    with _rate_lock:
        if len(_rate_buckets) > 5000:
            for key in [
                key
                for key, bucket in _rate_buckets.items()
                if not bucket or now - bucket[-1] > RATE_WINDOW
            ]:
                _rate_buckets.pop(key, None)
            if len(_rate_buckets) > 10000:
                _rate_buckets.clear()

        buckets = []
        for key, limit in checks:
            bucket = _rate_buckets[key]
            while bucket and now - bucket[0] > RATE_WINDOW:
                bucket.popleft()
            if len(bucket) >= limit:
                lang_header = request.headers.get("x-language") or request.headers.get(
                    "accept-language", ""
                )
                language = _normalize_language(
                    lang_header.split(",")[0].split(";")[0]
                )
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": RATE_LIMIT_REPLIES.get(
                            language, RATE_LIMIT_REPLIES["en"]
                        )
                    },
                )
            buckets.append(bucket)

        for bucket in buckets:
            bucket.append(now)

    return await call_next(request)


class ChatMessage(PydanticBaseModel):
    role: str = PydanticField(..., max_length=20)
    content: str = PydanticField(..., max_length=4000)


class ChatRequest(PydanticBaseModel):
    message: str = PydanticField(..., min_length=1, max_length=2000)
    history: list[ChatMessage] = PydanticField(default_factory=list, max_length=20)
    seen_stays: list[str] = PydanticField(default_factory=list, max_length=50)
    language: str = PydanticField(default="zh-CN", max_length=20)


TOOL_EXECUTORS = {
    "fetch_weather": lambda args: (fetch_weather.invoke(args), None),
    "plan_trip_route": execute_plan_trip_route,
    "recommend_attractions": execute_recommend_attractions,
    "recommend_food": execute_recommend_food,
    "recommend_stay": execute_recommend_stay,
    "recommend_hotel_deals": execute_recommend_hotel_deals,
    "recommend_products": execute_recommend_products,
    "plan_transport": execute_plan_transport,
}


ARTIFACT_FIELDS = {
    "plan_trip_route": "route",
    "recommend_attractions": "recommendation",
    "recommend_food": "food",
    "recommend_stay": "stay",
    "recommend_products": "products",
    "recommend_hotel_deals": "hotel_deals",
    "plan_transport": "transport",
}

# 工具返回的失败标识，用于判断本次回复是否可写入响应缓存
TOOL_FAILED_MARK = "工具执行失败"
TOOL_TIMEOUT_MARK = "工具执行超时"

BUSY_REPLIES = {
    "zh-CN": "当前咨询人数较多，请稍后重试。",
    "en": "We're receiving a lot of requests right now. Please try again shortly.",
    "ja": "現在アクセスが集中しています。しばらくしてから再度お試しください。",
    "ko": "현재 접속자가 많습니다. 잠시 후 다시 시도해 주세요.",
    "es": "Estamos recibiendo muchas solicitudes. Inténtalo de nuevo en un momento.",
    "fr": "Nous recevons beaucoup de demandes. Veuillez réessayer dans un instant.",
}

OVERLOAD_REPLIES = {
    "zh-CN": "这个问题需要更多查询，请缩小范围后重试。",
    "en": "This request needs too many lookups. Please narrow it down and try again.",
    "ja": "この質問は検索回数が多すぎます。範囲を絞って再度お試しください。",
    "ko": "이 질문은 조회가 너무 많습니다. 범위를 좁혀 다시 시도해 주세요.",
    "es": "Esta consulta requiere demasiadas búsquedas. Reduce el alcance e inténtalo de nuevo.",
    "fr": "Cette demande nécessite trop de recherches. Précisez-la et réessayez.",
}

_response_cache = OrderedDict()
_response_cache_lock = threading.Lock()


def _response_cache_key(req, language):
    """只缓存首轮、无上下文依赖的问题，避免串话。"""
    if req.history or req.seen_stays:
        return None
    message = " ".join(req.message.split())
    return (language, message) if message else None


def _cache_get(key):
    now = time.time()
    with _response_cache_lock:
        entry = _response_cache.get(key)
        if not entry:
            return None
        expires, value = entry
        if expires <= now:
            _response_cache.pop(key, None)
            return None
        _response_cache.move_to_end(key)
        return copy.deepcopy(value)


def _cache_set(key, value):
    now = time.time()
    with _response_cache_lock:
        _response_cache[key] = (now + RESPONSE_CACHE_TTL, copy.deepcopy(value))
        _response_cache.move_to_end(key)
        while len(_response_cache) > RESPONSE_CACHE_SIZE:
            _response_cache.popitem(last=False)


async def _call_model(messages, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError

    await asyncio.wait_for(_LLM_SEMAPHORE.acquire(), timeout=remaining)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return await asyncio.wait_for(
            model_with_tools.ainvoke(messages), timeout=remaining
        )
    finally:
        _LLM_SEMAPHORE.release()


async def _call_tool(name, args, deadline):
    executor = TOOL_EXECUTORS.get(name)
    if executor is None:
        return f"{TOOL_FAILED_MARK}：不支持的工具「{name}」", None

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError

    await asyncio.wait_for(_TOOL_SEMAPHORE.acquire(), timeout=remaining)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError

        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(_TOOL_POOL, context.run, executor, args),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            raise
        except Exception:
            logger.exception("工具执行失败：%s", name)
            return f"{TOOL_FAILED_MARK}，请稍后重试。", None
    finally:
        _TOOL_SEMAPHORE.release()


async def _run_tool_call(tool_call, req, deadline):
    """执行单个工具调用，返回 (给模型的文本, 给前端的附带数据)。"""
    name = tool_call["name"]
    args = tool_call["args"]
    if name == "recommend_stay" and req.seen_stays:
        args = {
            **args,
            "exclude": list(
                dict.fromkeys([*(args.get("exclude") or []), *req.seen_stays])
            ),
        }
    return await _call_tool(name, args, deadline)


def _tool_failed(content):
    return str(content or "").startswith((TOOL_FAILED_MARK, TOOL_TIMEOUT_MARK))


async def _run_chat(req):
    language = _normalize_language(req.language)
    cache_key = _response_cache_key(req, language)
    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    deadline = time.monotonic() + CHAT_BUDGET_SECONDS
    messages = [("system", _build_system_prompt(language))]
    for item in req.history[-10:]:
        if item.role in ("user", "assistant") and item.content:
            messages.append((item.role, item.content))
    messages.append(("user", req.message))

    result = {
        "reply": "",
        "degraded": False,
        "tool_used": None,
        "route": None,
        "recommendation": None,
        "food": None,
        "stay": None,
        "products": None,
        "hotel_deals": None,
        "transport": None,
    }
    cacheable = True

    try:
        response = await _call_model(messages, deadline)

        # 允许模型连续调用工具（最多 MAX_TOOL_ROUNDS 轮），同一轮的多个工具并发执行
        for _ in range(MAX_TOOL_ROUNDS):
            tool_calls = getattr(response, "tool_calls", None)
            if not tool_calls:
                break

            messages.append(response)
            outcomes = await asyncio.gather(
                *(_run_tool_call(tool_call, req, deadline) for tool_call in tool_calls),
                return_exceptions=True,
            )

            for tool_call, outcome in zip(tool_calls, outcomes):
                name = tool_call["name"]
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                if isinstance(outcome, BaseException):
                    logger.warning("工具执行异常：%s %s", name, type(outcome).__name__)
                    content, artifact = (
                        f"{TOOL_TIMEOUT_MARK}，请稍后重试。"
                        if isinstance(outcome, (asyncio.TimeoutError, TimeoutError))
                        else f"{TOOL_FAILED_MARK}，请稍后重试。"
                    ), None
                else:
                    content, artifact = outcome

                if _tool_failed(content):
                    cacheable = False

                messages.append(
                    ToolMessage(content=content, tool_call_id=tool_call["id"])
                )
                result["tool_used"] = result["tool_used"] or name
                field = ARTIFACT_FIELDS.get(name)
                if field and artifact:
                    result[field] = artifact

            response = await _call_model(messages, deadline)

        reply = getattr(response, "content", "") or ""
        if getattr(response, "tool_calls", None) and not reply:
            reply = OVERLOAD_REPLIES.get(language, OVERLOAD_REPLIES["en"])
            result["degraded"] = True
            cacheable = False
        result["reply"] = reply
    except (asyncio.TimeoutError, TimeoutError):
        result["reply"] = BUSY_REPLIES.get(language, BUSY_REPLIES["en"])
        result["degraded"] = True
        cacheable = False
    except HTTPException:
        raise
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code in (429, 503) or type(exc).__name__ in (
            "RateLimitError",
            "APITimeoutError",
            "APIConnectionError",
        ):
            result["reply"] = BUSY_REPLIES.get(language, BUSY_REPLIES["en"])
            result["degraded"] = True
            cacheable = False
        else:
            logger.exception("chat 处理失败")
            raise HTTPException(
                status_code=502, detail="助手暂时不可用，请稍后重试"
            ) from None

    if cache_key and cacheable and result["reply"]:
        _cache_set(cache_key, result)
    return result


@app.get("/map")
def map_image(
    location: str,
    zoom: str,
    size: str = "750*500",
    scale: str = "2",
    markers: str = "",
    paths: str = "",
):
    """代理高德静态地图，避免把 Key 暴露给浏览器。"""
    if not GAODE_API_KEY:
        raise HTTPException(status_code=500, detail="地图服务未配置")
    if not _MAP_LOCATION.fullmatch(location) or not _MAP_ZOOM.fullmatch(zoom):
        raise HTTPException(status_code=400, detail="地图参数不合法")
    if size != "750*500" or scale not in ("1", "2"):
        raise HTTPException(status_code=400, detail="地图参数不合法")
    if markers and (len(markers) > 2000 or not _MAP_SAFE.fullmatch(markers)):
        raise HTTPException(status_code=400, detail="地图参数不合法")
    if paths and (len(paths) > 8000 or not _MAP_SAFE.fullmatch(paths)):
        raise HTTPException(status_code=400, detail="地图参数不合法")

    params = {
        "key": GAODE_API_KEY,
        "location": location,
        "zoom": zoom,
        "size": size,
        "scale": scale,
    }
    if markers:
        params["markers"] = markers
    if paths:
        params["paths"] = paths

    try:
        content, content_type = amap_client.static_image(params)
    except amap_client.AmapError:
        raise HTTPException(status_code=502, detail="地图加载失败") from None

    if not content_type.startswith("image/"):
        logger.warning("静态地图返回非图片内容")
        raise HTTPException(status_code=502, detail="地图加载失败")

    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=600"},
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    client_id = (request.headers.get("x-client-id") or "").strip()[:64]
    token = skill_service.CLIENT_ID.set(client_id)
    try:
        return await _run_chat(req)
    except HTTPException:
        raise
    except Exception:
        logger.exception("chat 处理失败")
        raise HTTPException(status_code=502, detail="助手暂时不可用，请稍后重试") from None
    finally:
        skill_service.CLIENT_ID.reset(token)


if __name__ == "__main__":
    import uvicorn

    # 本地直跑时默认只监听回环地址，避免绕过前端限流直接访问 /chat
    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )