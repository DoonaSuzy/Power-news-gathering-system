"""时序接口（气象 24 分表 + 行情）的分层测试。

全程离线：SQL 用记录型假会话断言「拼出了什么、绑了什么」，不连库。
真实数据的一致性由人工对拍负责（见 docs 的实测记录）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import newsplatform.services.timeseries_service as svc_module
from newsagg.weather.models import VALUE_COLUMNS
from newsplatform.api.v1 import market as market_module
from newsplatform.api.v1 import weather as weather_module
from newsplatform.core.database import get_collector_session
from newsplatform.core.exception_handlers import register_exception_handlers
from newsplatform.core.exceptions import BadRequest
from newsplatform.core.ratelimit import enforce_rate_limit
from newsplatform.core.security import AuthContext, require_auth
from newsplatform.repositories.timeseries import (
    MarketRepository,
    WeatherRepository,
    province_basis,
    province_projection,
    resolve_table,
    select_columns,
)
from newsplatform.services.timeseries_service import (
    MAX_SERIES_ROWS,
    TimeSeriesService,
    resolve_region,
)

# --------------------------------------------------------------------------- 假会话


class _Result:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict]:
        return list(self._rows)

    def first(self) -> dict | None:
        return self._rows[0] if self._rows else None


class FakeSession:
    """记录 execute 调用并返回预置行。"""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):  # noqa: ANN001, ANN201
        self.calls.append((str(stmt), dict(params or {})))
        return _Result(self.rows)

    @property
    def last_sql(self) -> str:
        return self.calls[-1][0]

    @property
    def last_params(self) -> dict:
        return self.calls[-1][1]


def run(coro):  # noqa: ANN001, ANN202
    return asyncio.run(coro)


# --------------------------------------------------------------------------- 白名单


def test_select_columns_defaults_to_all_ten_in_ddl_order():
    cols = select_columns(None)
    assert len(cols) == 10
    assert cols[0] == "temperature_2m"


def test_select_columns_keeps_table_order_not_user_order():
    """用户先写 precipitation 也应返回 DDL 里的列序，保证响应形状稳定。"""
    assert select_columns(["precipitation", "temperature_2m"]) == (
        "temperature_2m",
        "precipitation",
    )


def test_select_columns_rejects_unknown_param_loudly():
    """拼错列名必须报错——静默忽略会返回错列数据，比 500 更坏。"""
    with pytest.raises(ValueError, match="未知气象参数"):
        select_columns(["temprature_2m"])


def test_resolve_table_accepts_only_known_region_and_product():
    assert resolve_table("华北", "hist_rt") == "wx_huabei_hist_rt"
    with pytest.raises(ValueError, match="未知大区"):
        resolve_table("华南东", "hist_rt")
    with pytest.raises(ValueError, match="未知产品"):
        resolve_table("华北", "hourly_fc")


def test_resolve_region_accepts_chinese_and_ascii_code():
    assert resolve_region("华北") == "华北"
    assert resolve_region("huabei") == "华北"
    assert resolve_region("GANGAT") == "港澳台"
    with pytest.raises(BadRequest, match="未知大区"):
        resolve_region("火星")


# --------------------------------------------------------------------------- 仓储 SQL


def test_weather_series_sql_binds_granularity_and_limits():
    session = FakeSession()
    run(
        WeatherRepository(session).series(
            table="wx_huabei_hist_rt",
            columns=("temperature_2m", "precipitation"),
            granularity="daily",
            start=datetime(2026, 1, 1),
            end=datetime(2026, 1, 31, 23, 59, 59),
            limit=100,
            offset=20,
        )
    )
    sql, params = session.last_sql, session.last_params
    assert "FROM `wx_huabei_hist_rt`" in sql
    assert "granularity = :_gran" in sql
    assert params["_gran"] == "daily"
    assert "ts >= :_start" in sql and "ts <= :_end" in sql
    assert "LIMIT 100 OFFSET 20" in sql
    assert "AVG" not in sql


def test_weather_series_province_level_groups_by_ts_and_province():
    session = FakeSession()
    run(
        WeatherRepository(session).series(
            table="wx_huanan_daily_fc",
            columns=("temperature_2m",),
            granularity="daily",
            level="province",
        )
    )
    sql = session.last_sql
    assert "ROUND(AVG(`temperature_2m`), 4)" in sql
    assert "GROUP BY ts, province_code" in sql
    assert "station_id" not in sql


def test_province_projection_uses_circular_mean_for_wind_direction():
    """角度是圆周量：AVG(350°, 10°) = 180° 是错的，正确答案是 0°。"""
    sql = province_projection("wind_direction_10m")
    assert "MOD(DEGREES(ATAN2(AVG(SIN(RADIANS(`wind_direction_10m`)))" in sql
    assert "AVG(COS(RADIANS(`wind_direction_10m`))))" in sql
    assert "% 360" not in sql  # 裸 % 会被 pymysql 当作格式化占位符
    assert "AVG(`wind_direction_10m`)" not in sql


def test_province_projection_uses_max_for_weather_code():
    """WMO 代码是枚举，平均出来的 5.12 无意义；取省内最严重。"""
    assert province_projection("weather_code") == "MAX(`weather_code`) AS `weather_code`"


def test_province_projection_defaults_to_avg_and_labels_every_column():
    assert province_projection("temperature_2m") == (
        "ROUND(AVG(`temperature_2m`), 4) AS `temperature_2m`"
    )
    basis = province_basis(list(VALUE_COLUMNS))
    assert len(basis) == 10
    assert "圆均值" in basis["wind_direction_10m"]
    assert "最严重" in basis["weather_code"]
    assert basis["precipitation"] == "省内各站均值"


def test_weather_series_filters_province_and_station_as_in_lists():
    session = FakeSession()
    run(
        WeatherRepository(session).series(
            table="wx_huabei_hist_rt",
            columns=("temperature_2m",),
            granularity="hourly",
            province_codes=["110000", "120000"],
            station_ids=["1010101000"],
        )
    )
    sql, params = session.last_sql, session.last_params
    assert "province_code IN (:_p0, :_p1)" in sql
    assert "station_id IN (:_s0)" in sql
    assert params["_p0"] == "110000" and params["_p1"] == "120000"
    assert params["_s0"] == "1010101000"


def test_weather_series_rejects_non_numeric_limit():
    """limit/offset 走 int() 再拼进 SQL；非数字必须炸，而不是被当字符串插进去。"""
    with pytest.raises(ValueError, match="invalid literal"):
        run(
            WeatherRepository(FakeSession()).series(
                table="wx_huabei_hist_rt",
                columns=("temperature_2m",),
                granularity="hourly",
                limit="10; DROP TABLE wx_huabei_hist_rt",
            )
        )


def test_market_series_rejects_unknown_dataset():
    with pytest.raises(ValueError, match="未知数据集"):
        run(MarketRepository(FakeSession()).series(dataset="not_a_dataset"))


def test_market_series_sql_uses_bound_dataset_and_date_range():
    session = FakeSession()
    run(
        MarketRepository(session).series(
            dataset="power_spot",
            start=datetime(2026, 9, 1),
            end=datetime(2026, 10, 4, 23, 59, 59),
            regions=["广东"],
            grades=["日前", "实时"],
            limit=50,
        )
    )
    sql, params = session.last_sql, session.last_params
    assert "FROM market_prices" in sql
    assert params["_ds"] == "power_spot"
    assert "price_date >= :_start" in sql and "price_date <= :_end" in sql
    assert "region IN (:_r0)" in sql and "grade IN (:_g0, :_g1)" in sql


def test_market_coverage_lists_every_declared_dataset():
    """库里没有的数据集也要出现在目录里（行数为 0），否则调用方以为接口缺功能。"""
    session = FakeSession(
        [
            {
                "dataset": "lng_spot",
                "rows_total": 140,
                "first_date": None,
                "last_date": None,
                "regions": 1,
                "grades": 2,
            }
        ]
    )
    rows = run(MarketRepository(session).coverage())
    assert len(rows) == 6
    by_code = {r["dataset"]: r for r in rows}
    assert by_code["lng_spot"]["rows_total"] == 140
    assert by_code["power_spot"]["rows_total"] == 0
    assert all(r["label"] for r in rows)


# --------------------------------------------------------------------------- 服务层


def test_service_maps_region_and_product_to_physical_table():
    session = FakeSession()
    data = run(
        TimeSeriesService(session).weather_series(
            region="huabei", product="hist_rt", granularity="hourly", limit=999_999
        )
    )
    assert data["meta"]["table"] == "wx_huabei_hist_rt"
    assert data["meta"]["limit"] == MAX_SERIES_ROWS
    assert data["meta"]["units"]["temperature_2m"] == "°C"
    assert data["meta"]["labels"]["weather_code"] == "天气代码"


def test_service_attaches_daily_basis_only_for_daily_rows():
    hourly = run(
        TimeSeriesService(FakeSession()).weather_series(
            region="华北", product="hist_rt", granularity="hourly"
        )
    )
    daily = run(
        TimeSeriesService(FakeSession()).weather_series(
            region="华北",
            product="hist_rt",
            granularity="daily",
            params=["shortwave_radiation", "wind_direction_10m"],
        )
    )
    assert hourly["meta"]["daily_basis"] == {}
    assert daily["meta"]["daily_basis"]["shortwave_radiation"] == "日累计（MJ/m²）"
    assert "风速加权" in daily["meta"]["daily_basis"]["wind_direction_10m"]
    # 口径说明对了还不够：units 仍写 W/m² 时，同一列同时带着两个互斥的单位，
    # 按 meta.units 换算的调用方会把 14.7 MJ 当成 14.7 W 用（差两个数量级）。
    assert hourly["meta"]["units"]["shortwave_radiation"] == "W/m²"
    assert daily["meta"]["units"]["shortwave_radiation"] == "MJ/m²"
    assert daily["meta"]["units"]["wind_direction_10m"] == "°"


def test_service_marks_province_basis_and_avoids_station_daily_confusion():
    """省级汇总的口径标签必须随响应返回——它与站点逐日的「风速加权」不是一回事。"""
    session = FakeSession()
    data = run(
        TimeSeriesService(session).weather_series(
            region="华南",
            product="daily_fc",
            granularity="daily",
            level="province",
            params=["temperature_2m", "wind_direction_10m", "weather_code"],
        )
    )
    assert (
        data["meta"]["province_basis"]["wind_direction_10m"]
        == "省内各站简单圆均值（非风速加权）"
    )
    assert data["meta"]["province_basis"]["temperature_2m"] == "省内各站均值"
    assert "ATAN2" in session.last_sql
    # 站点级不该带省级标签
    flat = run(
        TimeSeriesService(FakeSession()).weather_series(
            region="华南", product="daily_fc", granularity="daily"
        )
    )
    assert flat["meta"]["province_basis"] == {}


def test_service_rejects_bad_granularity_level_and_product():
    svc = TimeSeriesService(FakeSession())
    with pytest.raises(BadRequest, match="granularity"):
        run(svc.weather_series(region="华北", product="hist_rt", granularity="monthly"))
    with pytest.raises(BadRequest, match="level"):
        run(svc.weather_series(region="华北", product="hist_rt", level="nation"))
    with pytest.raises(BadRequest, match="未知气象产品"):
        run(svc.weather_series(region="华北", product="forecast", granularity="daily"))


def test_service_resolves_province_name_to_code(monkeypatch):
    """「广东」→ 440000 这一步必须落到 SQL 里，而不是靠用户记代码。"""

    async def fake_load_rows(session):  # noqa: ANN001
        return [{"code": "440000", "name": "广东省", "short_name": "广东"}]

    monkeypatch.setattr(svc_module, "load_rows", fake_load_rows)
    session = FakeSession()
    run(
        TimeSeriesService(session).weather_series(
            region="华南", product="hist_rt", province="广东"
        )
    )
    assert session.last_params["_p0"] == "440000"
    assert "province_code IN" in session.last_sql


def test_weather_coverage_pairs_region_and_product(monkeypatch):
    """24 张表与 (大区, 产品) 按 all_tables 顺序成对——错一位就全错。"""
    from newsagg.weather.models import all_tables

    rows = [
        {
            "table": t,
            "rows_total": 0,
            "first_ts": None,
            "last_ts": None,
            "stations": 0,
        }
        for t in all_tables()
    ]

    async def fake_coverage_all(self):  # noqa: ANN001, ANN002
        return rows

    monkeypatch.setattr(WeatherRepository, "coverage_all", fake_coverage_all)
    data = run(TimeSeriesService(FakeSession()).weather_coverage())
    assert len(data["items"]) == 24
    assert data["items"][0]["table"] == "wx_huabei_hist_rt"
    assert data["items"][0]["region"] == "华北"
    assert data["items"][0]["product"] == "hist_rt"
    assert data["items"][-1]["table"] == "wx_gangat_daily_fc"
    assert data["items"][-1]["region"] == "港澳台"


def test_weather_params_directory_is_self_consistent():
    catalog = TimeSeriesService.weather_params()
    assert len(catalog["items"]) == 10
    assert {i["daily_kind"] for i in catalog["items"]} == {
        "mean",
        "sum",
        "max",
        "dominant",
        "radiation_sum",
    }
    assert len(catalog["regions"]) == 8
    assert [p["code"] for p in catalog["products"]] == ["hist_rt", "hist_fc", "daily_fc"]


# --------------------------------------------------------------------------- HTTP 面


def build_app(session: FakeSession) -> FastAPI:
    app = FastAPI()
    app.include_router(weather_module.router, prefix="/api/v1")
    app.include_router(market_module.router, prefix="/api/v1")
    register_exception_handlers(app)
    app.dependency_overrides[get_collector_session] = lambda: session
    app.dependency_overrides[require_auth] = lambda: AuthContext(None, "test", {"admin"})
    app.dependency_overrides[enforce_rate_limit] = lambda: 0
    return app


def test_coverage_route_is_not_swallowed_by_region_product_path():
    """``/weather/coverage`` 声明在 ``/{region}/{product}`` 之前，否则会被当大区名解析。"""
    session = FakeSession(
        [
            {
                "table": "wx_huabei_hist_rt",
                "rows_total": 1,
                "first_ts": None,
                "last_ts": None,
                "stations": 1,
            }
        ]
    )
    resp = TestClient(build_app(session)).get("/api/v1/weather/coverage")
    assert resp.status_code == 200
    assert resp.json()["code"] == 0


def test_params_route_returns_catalog_without_db():
    resp = TestClient(build_app(FakeSession())).get("/api/v1/weather/params")
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert {"temperature_2m", "et0_fao_evapotranspiration"} <= {i["param"] for i in items}


def test_weather_series_route_ok_and_envelope():
    session = FakeSession(
        [
            {
                "ts": datetime(2026, 1, 1),
                "station_id": "x",
                "province_code": "110000",
                "adcode": 1,
                "temperature_2m": 3.5,
            }
        ]
    )
    resp = TestClient(build_app(session)).get(
        "/api/v1/weather/huabei/hist_rt?granularity=daily&limit=5"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["meta"]["table"] == "wx_huabei_hist_rt"
    assert body["data"]["items"][0]["temperature_2m"] == 3.5


def test_weather_series_route_accepts_chinese_region():
    resp = TestClient(build_app(FakeSession())).get("/api/v1/weather/华北/hist_rt")
    assert resp.status_code == 200


def test_weather_series_route_bad_region_is_400():
    resp = TestClient(build_app(FakeSession())).get("/api/v1/weather/火星/hist_rt")
    assert resp.status_code == 400
    assert "未知大区" in resp.json()["detail"]


def test_weather_series_route_bad_param_is_400():
    resp = TestClient(build_app(FakeSession())).get(
        "/api/v1/weather/huabei/hist_rt?params=nonsense_2m"
    )
    assert resp.status_code == 400
    assert "未知气象参数" in resp.json()["detail"]


def test_weather_series_route_splits_comma_params():
    session = FakeSession()
    resp = TestClient(build_app(session)).get(
        "/api/v1/weather/huabei/hist_rt?params=temperature_2m,precipitation"
    )
    assert resp.status_code == 200
    assert session.last_sql.count("`") // 2 >= 2
    assert "`temperature_2m`" in session.last_sql and "`precipitation`" in session.last_sql


def test_market_datasets_route_lists_six_datasets():
    resp = TestClient(build_app(FakeSession([]))).get("/api/v1/market/datasets")
    assert resp.status_code == 200
    assert len(resp.json()["data"]["items"]) == 6


def test_market_series_route_and_unknown_dataset_400():
    session = FakeSession(
        [
            {
                "price_date": datetime(2026, 10, 1),
                "region": "CN",
                "grade": "日前",
                "value": 350.0,
                "unit": "元/兆瓦时",
            }
        ]
    )
    client = TestClient(build_app(session))
    ok_resp = client.get("/api/v1/market/power_spot?limit=10")
    assert ok_resp.status_code == 200
    assert ok_resp.json()["data"]["meta"]["label"] == "电力现货市场价格"
    bad = client.get("/api/v1/market/not_a_dataset")
    assert bad.status_code == 400
