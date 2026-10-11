"""气象模块单测：表路由、时间切片、计费估算、响应解析、日聚合口径。

全部为**纯函数/纯对象**测试，不需要 MySQL、不需要联网。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from newsagg.weather import client as wclient
from newsagg.weather import models as wm
from newsagg.weather.client import (
    RateLimiter,
    Station,
    build_params,
    classify_rate_limit,
    daily_mean_by_date,
    error_reason,
    parse_payload,
    resolve_window,
)
from newsagg.weather.models import WeatherPoint

# ======================================================================
# 1. 表名与路由：24 张表、大区口径
# ======================================================================


def test_all_tables_is_24_and_unique():
    tables = wm.all_tables()
    assert len(tables) == 24
    assert len(set(tables)) == 24, "24 张表名必须互不相同"


def test_all_tables_is_region_major_ordered():
    """顺序必须是「大区优先」，便于 CLI 输出与人工核对。"""
    tables = wm.all_tables()
    assert tables[:3] == ["wx_huabei_hist_rt", "wx_huabei_hist_fc", "wx_huabei_daily_fc"]
    assert tables[-1] == "wx_gangat_daily_fc"


def test_table_name_known_values():
    assert wm.table_name("华北", "hist_rt") == "wx_huabei_hist_rt"
    assert wm.table_name("港澳台", "daily_fc") == "wx_gangat_daily_fc"


def test_table_name_rejects_unknown_region():
    """未知大区必须报错而不是兜底到某张「其它」表——静默兜底会让数据落错分区。"""
    with pytest.raises(ValueError, match="未知大区"):
        wm.table_name("华西", "hist_rt")


def test_table_name_rejects_unknown_product():
    with pytest.raises(ValueError, match="未知产品"):
        wm.table_name("华北", "weekly")


def test_region_of_province_uses_shared_dictionary():
    assert wm.region_of_province("440000") == "华南"
    assert wm.region_of_province("110000") == "华北"
    assert wm.region_of_province("710000") == "港澳台"


def test_region_of_province_rejects_unknown_code():
    with pytest.raises(ValueError, match="未知省级行政区划代码"):
        wm.region_of_province("999999")


def test_region_codes_cover_all_regions_and_are_ascii():
    """表名段必须全 ASCII 小写：中文表名会让命令行/备份工具频繁踩坑。"""
    assert set(wm.REGION_CODES) == set(wm.REGIONS)
    for code in wm.REGION_CODES.values():
        assert code.isascii() and code.islower()


# ======================================================================
# 2. 时间切片与计费估算
# ======================================================================


def test_chunk_periods_exact_multiple():
    chunks = wm.chunk_periods(date(2024, 1, 1), date(2024, 1, 28), days=14)
    assert chunks == [
        (date(2024, 1, 1), date(2024, 1, 14)),
        (date(2024, 1, 15), date(2024, 1, 28)),
    ]


def test_chunk_periods_remainder_is_not_padded():
    """末段不做补齐——补齐会带上未来日期，归档接口对未发布日期会报错。"""
    chunks = wm.chunk_periods(date(2024, 1, 1), date(2024, 1, 20), days=14)
    assert chunks[-1] == (date(2024, 1, 15), date(2024, 1, 20))


def test_chunk_periods_single_day():
    assert wm.chunk_periods(date(2024, 3, 5), date(2024, 3, 5)) == [
        (date(2024, 3, 5), date(2024, 3, 5))
    ]


def test_chunk_periods_rejects_reversed_range():
    with pytest.raises(ValueError, match="早于"):
        wm.chunk_periods(date(2024, 1, 5), date(2024, 1, 1))


def test_estimate_call_cost_baseline_is_one():
    """1 个地点 × 10 变量 × 14 天是计费基准 1.0。"""
    assert wm.estimate_call_cost(locations=1, days=14, variables=10) == 1.0


def test_estimate_call_cost_doubles_past_ten_variables():
    assert wm.estimate_call_cost(locations=1, days=14, variables=20) == 2.0


def test_estimate_call_cost_doubles_past_two_weeks():
    assert wm.estimate_call_cost(locations=1, days=28, variables=10) == 2.0


def test_estimate_call_cost_matches_documented_example():
    """官方文档的例子：2 周 15 变量 = 1.5 次（单地点口径）。"""
    assert wm.estimate_call_cost(locations=1, days=14, variables=15) == 1.5


def test_estimate_call_cost_scales_with_locations():
    """地点数按 1:1 计入权重——实测校准，不是官方文档的推论。

    证据：连发「100 站 × 14 天 × 17 变量」第 8 个触发 429；
    同样条件「1 站」连发 12 个全部通过。若地点数不计费，两者应同样被限。
    """
    assert wm.estimate_call_cost(locations=1, days=14, variables=10) == 1.0
    assert wm.estimate_call_cost(locations=100, days=14, variables=10) == 100.0
    assert wm.estimate_call_cost(locations=500, days=14, variables=10) == 500.0


def test_estimate_call_cost_realistic_batch():
    """本项目默认批量的单次权重，用于核对免费档额度是否够用。"""
    # 100 站 × 14 天 × 17 变量（10 逐时 + 7 日聚合）
    assert wm.estimate_call_cost(locations=100, days=14, variables=17) == 170.0


@pytest.mark.parametrize("kwargs", [
    {"locations": 0, "days": 14, "variables": 10},
    {"locations": 1, "days": 0, "variables": 10},
    {"locations": 1, "days": 14, "variables": 0},
])
def test_estimate_call_cost_rejects_non_positive(kwargs):
    with pytest.raises(ValueError):
        wm.estimate_call_cost(**kwargs)


# ======================================================================
# 3. 请求参数
# ======================================================================

STATIONS_2 = [
    Station("CN110101", 110101, 116.4167, 39.9129, "110000"),
    Station("CN310101", 310101, 121.4844, 31.2305, "310000"),
]


def test_build_params_sends_all_ten_hourly_variables():
    params = build_params(
        product="hist_rt", stations=STATIONS_2,
        start=date(2024, 1, 1), end=date(2024, 1, 14),
    )
    hourly = params["hourly"].split(",")
    assert len(hourly) == 10
    assert hourly == [v[0] for v in wm.HOURLY_VARS]
    assert len(params["daily"].split(",")) == len(wm.DAILY_FROM_API) == 7


def test_build_params_joins_multiple_locations():
    params = build_params(
        product="hist_rt", stations=STATIONS_2,
        start=date(2024, 1, 1), end=date(2024, 1, 1),
    )
    assert params["latitude"] == "39.9129,31.2305"
    assert params["longitude"] == "116.4167,121.4844"


def test_build_params_date_range_products_use_start_end():
    for product in ("hist_rt", "hist_fc"):
        params = build_params(
            product=product, stations=STATIONS_2,
            start=date(2024, 1, 1), end=date(2024, 1, 3),
        )
        assert params["start_date"] == "2024-01-01"
        assert params["end_date"] == "2024-01-03"
        assert "past_days" not in params


def test_build_params_rolling_product_uses_past_and_forecast_days():
    params = build_params(
        product="daily_fc", stations=STATIONS_2,
        start=date(2026, 10, 11), end=date(2026, 10, 17),
        today=date(2026, 10, 11),
    )
    assert params["past_days"] == "0"
    assert params["forecast_days"] == "7"
    assert "start_date" not in params


def test_build_params_rolling_rejects_past_days_over_92():
    with pytest.raises(ValueError, match="past_days 上限 92"):
        build_params(
            product="daily_fc", stations=STATIONS_2,
            start=date(2026, 1, 1), end=date(2026, 10, 17),
            today=date(2026, 10, 11),
        )


def test_build_params_rolling_rejects_forecast_days_over_16():
    with pytest.raises(ValueError, match="forecast_days 上限 16"):
        build_params(
            product="daily_fc", stations=STATIONS_2,
            start=date(2026, 10, 11), end=date(2026, 11, 30),
            today=date(2026, 10, 11),
        )


def test_build_params_rejects_unknown_product():
    with pytest.raises(ValueError, match="未知产品"):
        build_params(
            product="monthly", stations=STATIONS_2,
            start=date(2024, 1, 1), end=date(2024, 1, 1),
        )


# ======================================================================
# 4. 日聚合
# ======================================================================


def test_daily_mean_skips_missing_hours():
    """缺测不能当 0 算——否则日均会被系统性拉低。"""
    times = ["2024-01-01T00:00", "2024-01-01T01:00", "2024-01-01T02:00"]
    assert daily_mean_by_date(times, [10.0, None, 20.0]) == {date(2024, 1, 1): 15.0}


def test_daily_mean_omits_fully_missing_day():
    """整天缺测的日期不出现在结果里，下游写 NULL 而不是 0。"""
    times = ["2024-01-01T00:00", "2024-01-02T00:00"]
    assert daily_mean_by_date(times, [None, None]) == {}


def test_daily_mean_buckets_by_date():
    times = ["2024-01-01T23:00", "2024-01-02T00:00"]
    out = daily_mean_by_date(times, [4.0, 8.0])
    assert out == {date(2024, 1, 1): 4.0, date(2024, 1, 2): 8.0}


def test_daily_mean_tolerates_length_mismatch():
    """序列比时间轴短时按较短者处理，不抛异常（接口偶发丢尾）。"""
    assert daily_mean_by_date(["2024-01-01T00:00", "2024-01-01T01:00"], [6.0]) == {
        date(2024, 1, 1): 6.0
    }


# ======================================================================
# 5. 响应解析
# ======================================================================


def _payload(station_count: int = 1, hours: int = 2, days: int = 1) -> list[dict]:
    out = []
    for _ in range(station_count):
        times = [f"2024-01-0{i + 1}T{h:02d}:00" for i in range(days) for h in range(hours)]
        hourly = {"time": times}
        for col, _, _ in wm.HOURLY_VARS:
            if col in wm.DAILY_DERIVED:
                hourly[col] = [50.0 + i for i in range(len(times))]
            elif col == "wind_direction_10m":
                hourly[col] = [180] * len(times)
            elif col == "weather_code":
                hourly[col] = [3] * len(times)
            else:
                hourly[col] = [1.5] * len(times)
        daily = {
            "time": [f"2024-01-0{i + 1}" for i in range(days)],
            "temperature_2m_mean": [2.5] * days,
            "precipitation_sum": [0.0] * days,
            "wind_speed_10m_max": [7.0] * days,
            "wind_direction_10m_dominant": [90] * days,
            "shortwave_radiation_sum": [11.0] * days,
            "et0_fao_evapotranspiration": [0.3] * days,
            "weather_code": [61] * days,
        }
        out.append({"hourly": hourly, "daily": daily})
    return out


def test_parse_payload_produces_hourly_and_daily_rows():
    stations = [STATIONS_2[0]]
    points = parse_payload(_payload(hours=3, days=2), stations, product="hist_rt")
    hourly = [p for p in points if p.granularity == "hourly"]
    daily = [p for p in points if p.granularity == "daily"]
    assert len(hourly) == 6  # 2 天 × 3 小时
    assert len(daily) == 2
    assert hourly[0].ts == datetime(2024, 1, 1, 0, 0)
    assert daily[0].ts == datetime(2024, 1, 1, 0, 0)


def test_parse_payload_fills_daily_from_api_columns():
    points = parse_payload(_payload(hours=2, days=1), [STATIONS_2[0]], product="hist_rt")
    day = next(p for p in points if p.granularity == "daily")
    assert day.values["temperature_2m"] == 2.5   # 日平均
    assert day.values["precipitation"] == 0.0    # 日累计
    assert day.values["wind_speed_10m"] == 7.0   # 日最大
    assert day.values["shortwave_radiation"] == 11.0
    assert day.values["weather_code"] == 61.0


def test_parse_payload_derives_humidity_cloud_pressure_locally():
    """这三个变量接口没有日聚合，必须由逐时值本地取均值。"""
    points = parse_payload(_payload(hours=2, days=1), [STATIONS_2[0]], product="hist_rt")
    day = next(p for p in points if p.granularity == "daily")
    # 逐时值为 50.0、51.0 → 均值 50.5
    assert day.values["relative_humidity_2m"] == pytest.approx(50.5)
    assert day.values["cloud_cover"] == pytest.approx(50.5)
    assert day.values["pressure_msl"] == pytest.approx(50.5)


def test_parse_payload_daily_derived_is_none_when_hourly_missing():
    """逐时全缺时，派生列必须是 None 而不是 0。"""
    payload = _payload(hours=2, days=1)
    for col in wm.DAILY_DERIVED:
        payload[0]["hourly"][col] = [None, None]
    points = parse_payload(payload, [STATIONS_2[0]], product="hist_rt")
    day = next(p for p in points if p.granularity == "daily")
    for col in wm.DAILY_DERIVED:
        assert day.values[col] is None


def test_parse_payload_keeps_none_for_missing_hourly_value():
    payload = _payload(hours=3, days=1)
    payload[0]["hourly"]["temperature_2m"] = [1.0, None, 3.0]
    points = parse_payload(payload, [STATIONS_2[0]], product="hist_rt")
    hourly = [p for p in points if p.granularity == "hourly"]
    assert [p.values["temperature_2m"] for p in hourly] == [1.0, None, 3.0]


def test_parse_payload_rejects_length_mismatch():
    """响应站点数与请求不一致时必须报错，不能按下标错配——错配会把 A 站数据写进 B 站。"""
    with pytest.raises(ValueError, match="不一致"):
        parse_payload(_payload(station_count=2), [STATIONS_2[0]], product="hist_rt")


def test_parse_payload_accepts_single_location_object():
    """单地点时接口回对象而非数组，也必须能解析。"""
    points = parse_payload(_payload()[0], [STATIONS_2[0]], product="hist_rt")
    assert points


def test_parse_payload_routes_to_region_table():
    points = parse_payload(_payload(), [STATIONS_2[1]], product="daily_fc")  # 上海
    assert {p.table for p in points} == {"wx_huadong_daily_fc"}


def test_parse_payload_accepts_all_products():
    for product in wm.PRODUCTS:
        points = parse_payload(_payload(), [STATIONS_2[0]], product=product)
        assert points
        assert points[0].source == wm.PRODUCT_SOURCE[product]


# ======================================================================
# 6. WeatherPoint
# ======================================================================


def _point(**kw) -> WeatherPoint:
    base = dict(
        station_id="CN110101", adcode=110101, province_code="110000",
        product="hist_rt", granularity="hourly",
        ts=datetime(2024, 1, 1, 0, 0), values={}, source="test",
    )
    base.update(kw)
    return WeatherPoint(**base)


def test_weather_point_rejects_bad_granularity():
    with pytest.raises(ValueError, match="未知粒度"):
        _point(granularity="weekly")


def test_weather_point_rejects_unknown_variable():
    with pytest.raises(ValueError, match="未知变量列"):
        _point(values={"temp": 1.0})


def test_weather_point_as_row_matches_ddl_column_count():
    """16 列 = station_id + adcode + province_code + granularity + ts + 10 变量 + source。"""
    row = _point(values={c: 1.0 for c in wm.VALUE_COLUMNS}).as_row()
    assert len(row) == 5 + len(wm.VALUE_COLUMNS) + 1 == 16
    assert row[0] == "CN110101"
    assert row[3] == "hourly"


def test_weather_point_table_follows_province():
    assert _point(province_code="440000").table == "wx_huanan_hist_rt"
    assert _point(province_code="710000").table == "wx_gangat_hist_rt"


def test_weather_point_missing_variable_becomes_none():
    row = _point(values={"temperature_2m": 3.0}).as_row()
    assert row[5] == 3.0
    assert row[6] is None  # relative_humidity_2m 没给


# ======================================================================
# 7. 窗口收敛
# ======================================================================


def test_resolve_window_archive_ends_five_days_ago():
    start, end = resolve_window(
        "hist_rt", start=date(2024, 1, 1), end=date(2026, 10, 11),
        today=date(2026, 10, 11),
    )
    assert end == date(2026, 10, 6), "ERA5 滞后 5 天"


def test_resolve_window_historical_forecast_clamps_to_coverage_start():
    start, _ = resolve_window(
        "hist_fc", start=date(2020, 1, 1), end=date(2026, 10, 11),
        today=date(2026, 10, 11),
    )
    assert start == date(2022, 11, 24)


def test_resolve_window_rolling_starts_today():
    start, _ = resolve_window(
        "daily_fc", start=date(2024, 1, 1), end=date(2026, 10, 17),
        today=date(2026, 10, 11),
    )
    assert start == date(2026, 10, 11)


def test_resolve_window_rejects_unknown_product():
    with pytest.raises(ValueError, match="未知产品"):
        resolve_window("x", start=date(2024, 1, 1), end=date(2024, 1, 2))


# ======================================================================
# 7b. 回填工作单元（回归：滚动产品不能被时间切片）
# ======================================================================


def test_plan_units_does_not_chunk_rolling_product():
    """``daily_fc`` 的窗口恒被夹到「今天起」。

    若照常切成 73 段，每段解析后都指向同一天 → 同一天被请求 73 次并互相覆盖。
    """
    from newsagg.weather.cli import _plan_units

    units = _plan_units(
        products=["daily_fc"], regions=["华北"],
        start=date(2024, 1, 1), end=date(2026, 10, 11), period_days=14,
    )
    assert units == [("daily_fc", "华北", date(2024, 1, 1), date(2026, 10, 11))]


def test_plan_units_chunks_date_range_products():
    from newsagg.weather.cli import _plan_units

    for product in ("hist_rt", "hist_fc"):
        units = _plan_units(
            products=[product], regions=["华北"],
            start=date(2024, 1, 1), end=date(2024, 1, 28), period_days=14,
        )
        assert len(units) == 2, f"{product} 应被切成 2 段"


# ======================================================================
# 8. 限速器
# ======================================================================


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_rate_limiter_prunes_old_stamps():
    clock = _Clock()
    limiter = RateLimiter(per_day=10_000, clock=clock)
    limiter.acquire()
    assert len(limiter._stamps) == 1
    clock.t += 86_400 + 1
    limiter._prune(clock.t)
    assert len(limiter._stamps) == 0


def test_rate_limiter_sleeps_when_minute_budget_exhausted(monkeypatch):
    """触到分钟额度时必须真的等待，而不是继续硬打。"""
    slept: list[float] = []
    clock = _Clock()

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.t += seconds

    monkeypatch.setattr(wclient.time, "sleep", fake_sleep)
    limiter = RateLimiter(per_minute=1, per_hour=10_000, per_day=10_000, clock=clock)

    assert limiter.acquire() == 0.0
    limiter.acquire()  # 第 2 次会撞到 per_minute=1
    assert slept == [60.0]
    assert limiter.waits == pytest.approx(60.0)


def test_rate_limiter_accounts_for_request_weight(monkeypatch):
    """一次批量请求消耗 170 单位权重；只数请求条数会让限速形同虚设。

    实测教训：按「请求条数」限速时，5 个 100 站请求就打进了 429。
    """
    slept: list[float] = []
    clock = _Clock()

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.t += seconds

    monkeypatch.setattr(wclient.time, "sleep", fake_sleep)
    limiter = RateLimiter(per_minute=600, per_hour=10_000, per_day=10_000, clock=clock)

    for _ in range(3):  # 3 × 170 = 510 < 600，不应等待
        assert limiter.acquire(170.0) == 0.0
    assert slept == []

    limiter.acquire(170.0)  # 第 4 次：510 + 170 = 680 > 600 → 必须等待
    assert slept, "权重超限时必须等待"
    assert len(limiter._stamps) == 4


def test_rate_limiter_rejects_non_positive_weight():
    limiter = RateLimiter(clock=_Clock())
    with pytest.raises(ValueError, match="weight 必须为正"):
        limiter.acquire(0)


def test_rate_limiter_honours_min_gap(monkeypatch):
    slept: list[float] = []
    clock = _Clock()
    monkeypatch.setattr(wclient.time, "sleep", lambda s: (slept.append(s), clock.__setattr__("t", clock.t + s)))
    limiter = RateLimiter(per_minute=1000, min_gap=5.0, clock=clock)
    limiter.acquire()
    limiter.acquire()
    assert slept == [5.0]


def test_classify_rate_limit_reads_the_official_reason():
    """429 的 reason 是整句英文；只有认出窗口才能决定等 1 分钟还是 1 小时。

    实测（2026-10-11 回填）：对方返回
    ``{"error":true,"reason":"Hourly API request limit exceeded. Please try again in the next hour."}``
    而旧实现只会退避重试，于是在同一个已耗尽的小时里连打 4 次、全部 429，
    整片被记成 failed。
    """
    assert classify_rate_limit(
        "Hourly API request limit exceeded. Please try again in the next hour."
    ) == "hour"
    assert classify_rate_limit("Minute API request limit exceeded.") == "minute"
    assert classify_rate_limit("Daily API request limit exceeded.") == "day"
    assert classify_rate_limit("Monthly API request limit exceeded.") == "month"
    # 认不出来必须返回 None（退化为退避重试），不能瞎猜一个窗口
    assert classify_rate_limit("") is None
    assert classify_rate_limit("something else entirely") is None


def test_error_reason_survives_non_json_bodies():
    assert error_reason({"error": True, "reason": "Hourly ..."}) == "Hourly ..."
    assert error_reason({}) == ""
    assert error_reason(None) == ""
    assert error_reason("not a dict") == ""


def test_block_until_marks_the_window_full(monkeypatch):
    """记满「小时」窗口后，下一次 acquire 必须睡到该窗口翻页。"""
    slept: list[float] = []
    clock = _Clock()

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.t += seconds

    monkeypatch.setattr(wclient.time, "sleep", fake_sleep)
    # per_minute 调高，隔离出「小时窗口」这一个变量
    limiter = RateLimiter(per_minute=100_000, per_hour=5_000, clock=clock)

    assert limiter.block_until("hour") == 3_600.0
    limiter.acquire(1.0)  # 小时额度已被记满 → 必须等到翻页
    assert slept == [3_600.0]


def test_block_until_rejects_unknown_window():
    limiter = RateLimiter(clock=_Clock())
    with pytest.raises(ValueError, match="未知配额窗口"):
        limiter.block_until("fortnight")


def test_request_waits_for_the_next_hour_instead_of_burning_retries(monkeypatch):
    """触到「小时额度用满」后必须睡到下一小时，而不是连打 max_attempts 次。"""
    slept: list[float] = []
    calls = {"n": 0}
    clock = _Clock()  # 必须让 fake_sleep 推进同一个时钟，否则限速器永远等不到翻页

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={
                "error": True,
                "reason": "Hourly API request limit exceeded. "
                          "Please try again in the next hour.",
            })
        return httpx.Response(200, json={
            "latitude": [39.9], "longitude": [116.4],
            "hourly": {"time": ["2024-01-01T00:00"], "temperature_2m": [1.0]},
        })

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.t += seconds

    monkeypatch.setattr(wclient.time, "sleep", fake_sleep)
    client = wclient.WeatherClient(
        limiter=RateLimiter(per_minute=100_000, per_hour=5_000, clock=clock),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        payload = client._request(
            "https://example.invalid/v1/archive", {"latitude": "39.9"}, weight=10.0,
        )
    finally:
        client.close()

    assert payload["hourly"]["temperature_2m"] == [1.0]
    assert calls["n"] == 2, "不该在同一个耗尽的小时里重复打多次"
    assert slept == [3_600.0], f"必须睡到小时窗口翻页，实际 {slept}"


def test_monthly_quota_exhaustion_aborts_immediately():
    """月额度用满时等待没有意义，必须立刻中止而不是把进程挂住。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={
            "error": True, "reason": "Monthly API request limit exceeded.",
        })

    client = wclient.WeatherClient(
        limiter=RateLimiter(clock=_Clock()),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(RuntimeError, match="月额度用满"):
            client._request("https://example.invalid/v1/archive", {}, weight=1.0)
    finally:
        client.close()


# ======================================================================
# 9. 建表 SQL 与源码同步（回归：防止改列漏改 SQL）
# ======================================================================


def test_generated_schema_is_in_sync_with_models():
    """`sql/weather_schema.sql` 必须与 models.py 完全一致。

    24 张表共享同一份列定义，一旦有人只改了源码或只改了 SQL，
    就会出现「某些表少了某列」的不一致——这个断言把漂移挡在提交之前。
    """
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "_gen_weather_schema", root / "scripts" / "gen_weather_schema.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    target = root / "sql" / "weather_schema.sql"
    assert target.exists(), "缺少 sql/weather_schema.sql"
    assert target.read_text(encoding="utf-8") == module.render()


def test_generated_schema_has_24_data_tables():
    root = Path(__file__).resolve().parents[1]
    sql = (root / "sql" / "weather_schema.sql").read_text(encoding="utf-8")
    for table in wm.all_tables():
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in sql
    assert sql.count("CREATE TABLE IF NOT EXISTS wx_") == 26  # 24 数据表 + 站点 + 进度


def test_schema_declares_every_variable_column_with_daily_semantics():
    """每个数值列都要在注释里写清逐日口径——否则「日行这一列是什么意思」只能靠猜。"""
    root = Path(__file__).resolve().parents[1]
    sql = (root / "sql" / "weather_schema.sql").read_text(encoding="utf-8")
    for col in wm.VALUE_COLUMNS:
        assert f"{col} " in sql
        assert f"逐日={wm.DAILY_AGG_LABEL[col]}" in sql
