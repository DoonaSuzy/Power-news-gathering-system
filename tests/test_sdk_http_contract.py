"""SDK 的 HTTP 数据源 ↔ 时序接口的参数契约（离线，不起服务）。

为什么要单独测
--------------
``HttpTimeSeriesSource`` 拼 URL、服务端解 URL，同一份约定写两遍就会漂。
SDK 自己的单测两端都是它写的，漂了也测不出来；而漂的后果是**每次调用都 400**
（更糟的一种是参数名写错、被服务端当缺省值，静默返回另一种口径的数据）。

这里不比对返回内容，只比对三件事：

1. SDK 发出的 query 参数名，接口签名里是否都存在；
2. SDK 发出的取值（时间 / 粒度 / 层级 / 参数列 / 数据集），服务端自己的解析器能不能收；
3. 一次气象 fetch 的路径形态（8 个大区各问一次）是否符合约定。
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import pytest
from fastapi import FastAPI

from newsagg.market.models import DATASETS
from newsagg.weather.models import GRANULARITIES, VALUE_COLUMNS
from newsplatform.api.v1 import market as market_module
from newsplatform.api.v1 import weather as weather_module
from newsplatform.core.deps import parse_day
from newsplatform.services.timeseries_service import (
    MAX_SERIES_ROWS,
    resolve_product,
    resolve_region,
)
from powersdk import HttpTimeSeriesSource

WEATHER_PATH = "/api/v1/weather/{region}/{product}"
MARKET_PATH = "/api/v1/market/{dataset}"

BASE = "http://contract.test/api/v1"


@pytest.fixture(scope="module")
def paths() -> dict:
    """只挂两个时序路由的 OpenAPI paths（服务端「认识什么」的唯一权威来源）。"""
    app = FastAPI()
    app.include_router(weather_module.router, prefix="/api/v1")
    app.include_router(market_module.router, prefix="/api/v1")
    return app.openapi()["paths"]


class Sender:
    """记录 SDK 实际发出的 URL，响应一律为空页（翻页第一轮就停）。"""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, url: str, headers: dict) -> dict:
        self.urls.append(url)
        return {"code": 0, "data": {"meta": {}, "items": []}}


def split_url(url: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urlparse(url)
    query = {
        key: [unquote(value) for value in values]
        for key, values in parse_qs(parsed.query).items()
    }
    return unquote(parsed.path), query


def declared(paths: dict, path: str) -> set[str]:
    return {p["name"] for p in paths[path]["get"].get("parameters", []) if p["in"] == "query"}


def test_weather_query_params_are_all_declared(paths):
    """SDK 发的每个气象 query 名都必须在接口签名里——名字写错服务端只会当「没传」。"""
    sender = Sender()
    HttpTimeSeriesSource(base_url=BASE, transport=sender).fetch(
        "weather",
        region="广东",
        start="2024-01-01",
        end="2024-01-02",
        granularity="daily",
        param="temperature_2m",
    )
    assert len(sender.urls) == 8, "8 个大区各问一次，省份归属交给服务端过滤"

    allowed = declared(paths, WEATHER_PATH)
    for url in sender.urls:
        path, query = split_url(url)
        assert path.startswith("/api/v1/weather/")
        assert set(query) <= allowed, sorted(set(query) - allowed)

        region, product = path.rsplit("/", 2)[-2:]
        assert resolve_region(region) == region, region
        assert resolve_product(product) == product, product

        assert query["granularity"] == ["daily"] and query["granularity"][0] in GRANULARITIES
        assert query["level"] == ["province"]
        assert query["params"] == ["temperature_2m"]
        assert set(query["params"]) <= set(VALUE_COLUMNS)
        assert query["province"] == ["440000"]
        assert 1 <= int(query["limit"][0]) <= MAX_SERIES_ROWS
        assert int(query["offset"][0]) >= 0


def test_weather_time_values_parse_on_the_server(paths):
    """``since`` / ``until`` 要能被服务端 ``parse_day`` 解析（它不吃 ISO 的 ``T``）。"""
    sender = Sender()
    HttpTimeSeriesSource(base_url=BASE, transport=sender).fetch(
        "weather", region=["广东", "云南"], start="2024-01-01 08:00", end="2024-01-02"
    )

    for url in sender.urls:
        _, query = split_url(url)
        assert parse_day(query["since"][0]) is not None
        assert parse_day(query["until"][0], end_of_day=True) is not None
        # 上界到当天 23:59:59，否则「按天查」会静默漏掉一整天
        assert query["until"][0].endswith("23:59:59")
        assert query["province"] == ["440000,530000"]


def test_market_dataset_and_grade_are_server_values(paths):
    """行情侧：数据集在 ``DATASETS`` 里，grade 原样透传（默认口径由绑定表钉死）。"""
    sender = Sender()
    HttpTimeSeriesSource(base_url=BASE, transport=sender).fetch(
        "煤价", region="全国", start="2026-01-01", end="2026-01-31"
    )
    assert len(sender.urls) == 1

    path, query = split_url(sender.urls[0])
    assert set(query) <= declared(paths, MARKET_PATH), sorted(set(query) - declared(paths, MARKET_PATH))
    assert path.rsplit("/", 1)[-1] in DATASETS
    assert query["region"] == ["CN"]
    assert query["grade"] == ["到岸标煤单价"]
