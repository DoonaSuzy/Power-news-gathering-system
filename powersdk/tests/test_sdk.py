"""powersdk 测试套件（完全离线、确定性）。

覆盖简历口径的全部能力点：

* 3 行代码用法返回正确的标准化 DataFrame；
* 11 类时序数据全部可查、4 种时间粒度、跨粒度数值一致性；
* 20+ 气象参数插值方法映射表 + 解析兜底 + 各方法正确性；
* JSON 解析（多种结构/中英文列别名）+ 时间戳自动补全 + 时区归一；
* 数据校验准确率 ≥ 0.998（干净种子数据）与脏数据告警；
* pypinyin 地区索引（广东 → guangdong / GD / 440000）；
* dev/test/prod 多环境配置切换；
* 单批次 10 万条向量化处理（断言走向量化路径且结果正确，不断言耗时）；
* 通用工具函数：重采样 / 补缺 / 透视 / 清洗 / 单位归一 / 记录导出。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

import powersdk
from powersdk import (
    CATEGORY_BINDINGS,
    DEFAULT_METHOD,
    INTERP_METHODS,
    METHOD_CATALOG,
    Alert,
    AlertSink,
    Category,
    CollectingAlertSink,
    ConfigError,
    DataSourceError,
    HttpTimeSeriesSource,
    InMemoryTimeSeriesSource,
    LoguruAlertSink,
    MySQLTimeSeriesSource,
    PowerDataClient,
    PowerSDKError,
    Settings,
    UnavailableParamError,
    UnknownCategoryError,
    UnknownRegionError,
    UnsupportedCategoryError,
    ValidationSpec,
    apply_interpolation,
    backed_categories,
    canonical_method,
    categories,
    clean_outliers,
    complete_timestamps,
    fill_gaps,
    parse_json_records,
    pivot_region,
    region_code,
    region_pinyin,
    resolve_method,
    standardize_units,
    to_pinyin_abbr,
    to_records,
    unbacked_categories,
    validate,
    validate_and_alert,
    vectorized_transform,
    weather_column,
    weather_unit,
)
from powersdk.config import ENV_PROFILES, get_settings, reset_settings
from powersdk.pinyin import default_region_index, normalize_region
from powersdk.timeseries import (
    CATEGORY_REGISTRY,
    GRANULARITIES,
    WEATHER_PARAMS,
    resolve_granularity,
)

SEED = 20250101


@pytest.fixture(autouse=True)
def _test_env(monkeypatch):
    """每个用例都固定为 test 环境，避免环境污染导致的不确定性。"""
    monkeypatch.setenv("POWERSDK_ENV", "test")
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def source() -> InMemoryTimeSeriesSource:
    """确定性内存数据源（2025 年 1 月逐小时，34 省级行政区）。"""
    return InMemoryTimeSeriesSource(seed=SEED)


@pytest.fixture
def client(source) -> PowerDataClient:
    return PowerDataClient(source=source, env="test")


# ------------------------------------------------------- 3 行代码 / 门面
def test_three_line_usage_returns_standard_frame():
    """简历口径：业务人员 3 行代码即可获取标准化数据。"""
    from powersdk import PowerDataClient  # 第 1 行

    client = PowerDataClient()  # 第 2 行
    df = client.query(  # 第 3 行
        "load", region="广东", start="2025-01-01", end="2025-01-02", granularity="hourly"
    )

    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["ts", "region", "value", "unit"]
    assert len(df) == 48  # 2 天 × 24 小时
    assert set(df["region"]) == {"广东"}
    assert set(df["unit"]) == {"MW"}
    assert df["value"].notna().all()
    assert df["ts"].is_monotonic_increasing
    assert df["ts"].min() == pd.Timestamp("2025-01-01 00:00")
    assert df["ts"].max() == pd.Timestamp("2025-01-02 23:00")
    assert client.last_accuracy() >= 0.998
    assert client.alerts == ()


def test_all_11_categories_queryable(client):
    """11 类时序数据全部可查，且输出结构一致、准确率达标。"""
    names = categories()
    assert len(names) == 11
    assert len(set(names)) == 11
    assert set(Category) == {Category(name) for name in names}

    for name in names:
        df = client.query(name, region="广东", start="2025-01-01", end="2025-01-03", granularity="hourly")
        assert not df.empty, name
        assert list(df.columns) == ["ts", "region", "value", "unit"]
        assert set(df["unit"]) == {CATEGORY_REGISTRY[name].unit}
        assert df["value"].notna().all(), name
        assert len(df) == 72, name  # 3 天 × 24 小时（end 含当天）
        report = client.last_report
        assert report is not None and report.accuracy >= 0.998, name
        assert report.violations == (), name


def test_category_alias_and_enum_resolution(client):
    """中文别名 / 枚举 / 大小写混写都能解析到同一类别。"""
    assert powersdk.resolve_category("负荷") is Category.LOAD
    assert powersdk.resolve_category("LOAD") is Category.LOAD
    assert powersdk.resolve_category(Category.LOAD) is Category.LOAD
    assert powersdk.resolve_category("风电") is Category.WIND_POWER

    with pytest.raises(UnknownCategoryError):
        client.query("不存在的类别")


def test_multi_granularity_consistency(client):
    """raw/hourly/daily/monthly 四档粒度，且跨粒度数值一致。"""
    raw = client.query("load", region="广东", start="2025-01-01", end="2025-01-31", granularity="raw")
    hourly = client.query("load", region="广东", start="2025-01-01", end="2025-01-31", granularity="hourly")
    daily = client.query("load", region="广东", start="2025-01-01", end="2025-01-31", granularity="daily")
    monthly = client.query("load", region="广东", start="2025-01-01", end="2025-01-31", granularity="monthly")

    assert len(raw) == len(hourly) == 744  # 1 月整月逐小时
    assert len(daily) == 31
    assert len(monthly) == 1
    # raw 数据本身即逐小时、无缺口，因此 hourly 与原值一致
    assert np.allclose(hourly["value"].to_numpy(), raw["value"].to_numpy())
    # 日均值 == 该日 24 个小时值的均值（跨粒度一致性）
    expected_daily = hourly.set_index("ts")["value"].resample("D").mean().to_numpy()
    assert np.allclose(daily["value"].to_numpy(), expected_daily)
    # 月均值 == 全部小时值的均值
    assert monthly["value"].iloc[0] == pytest.approx(float(raw["value"].mean()))

    assert resolve_granularity("h") == "hourly"
    assert resolve_granularity("日") == "daily"
    with pytest.raises(ValueError):
        resolve_granularity("fortnightly")


# --------------------------------------------------------------- 插值方法表
def test_interpolation_method_table():
    """20+ 气象参数 → 插值方法映射表（配置与逻辑解耦）。"""
    assert len(INTERP_METHODS) >= 20
    assert len(set(INTERP_METHODS.values())) >= 20
    assert len(METHOD_CATALOG) >= 20
    assert all(name in METHOD_CATALOG for name in set(INTERP_METHODS.values()))

    required = {
        "linear", "nearest", "cubic", "spline", "idw", "bilinear", "akima", "pchip",
        "quadratic", "zero", "previous", "next", "time", "index", "area", "dropped",
        "pad", "ffill", "bfill",
    }
    assert required <= set(METHOD_CATALOG)
    assert required <= set(INTERP_METHODS.values())

    # 配置驱动：降水是累量 → 零阶保持；温度 → 线性
    assert resolve_method("precipitation") == "zero"
    assert resolve_method("temperature_2m") == "linear"
    # override 优先于配置表
    assert resolve_method("precipitation", override="linear") == "linear"
    assert resolve_method("precipitation", override="PAD") == "pad"
    # 未知参数回退默认方法
    assert resolve_method("不存在的参数") == DEFAULT_METHOD
    assert resolve_method(None) == DEFAULT_METHOD
    # 表内每个参数都能解析出目录中已登记的方法
    for param in INTERP_METHODS:
        assert resolve_method(param) in METHOD_CATALOG


def test_apply_interpolation_methods_are_correct():
    """各插值方法的数值正确性（含无 SciPy 环境下的降级行为）。"""
    series = pd.Series([1.0, np.nan, np.nan, 4.0, np.nan])

    def equals(got, expected):
        assert len(got) == len(expected)
        assert all(
            (pd.isna(a) and pd.isna(b)) or a == pytest.approx(b)
            for a, b in zip(got, expected, strict=True)
        )

    equals(apply_interpolation(series, method="linear").tolist(), [1.0, 2.0, 3.0, 4.0, 4.0])
    equals(apply_interpolation(series, method="nearest").tolist(), [1.0, 1.0, 4.0, 4.0, 4.0])
    equals(apply_interpolation(series, method="zero").tolist(), [1.0, 1.0, 1.0, 4.0, 4.0])
    equals(apply_interpolation(series, method="previous").tolist(), [1.0, 1.0, 1.0, 4.0, 4.0])
    equals(apply_interpolation(series, method="bfill").tolist(), [1.0, 4.0, 4.0, 4.0, np.nan])
    equals(apply_interpolation(series, method="next").tolist(), [1.0, 4.0, 4.0, 4.0, np.nan])
    # dropped：直接丢弃缺失
    assert apply_interpolation(series, method="dropped").tolist() == [1.0, 4.0]
    # idw：反距离权重（位置 1 距左 1、距右 2 → (1·1 + 0.25·4)/1.25 = 1.6）
    equals(apply_interpolation(series, method="idw").tolist()[1:3], [1.6, 3.4])
    # SciPy 方法在本环境降级为 linear，且结果与 linear 完全一致
    for scipy_only in ("cubic", "spline", "akima", "pchip", "quadratic"):
        equals(
            apply_interpolation(series, method=scipy_only).tolist(),
            apply_interpolation(series, method="linear").tolist(),
        )
    # 无缺失时原样返回
    clean = pd.Series([1.0, 2.0, 3.0])
    assert apply_interpolation(clean, method="linear").tolist() == [1.0, 2.0, 3.0]


def test_interpolate_frame_wide(client):
    """宽表逐列插值：全 linear 走快路径，与逐列结果一致。"""
    from powersdk import interpolate_frame

    wide = pd.DataFrame(
        {"temperature_2m": [1.0, np.nan, 3.0], "precipitation": [0.0, np.nan, 2.0]},
        index=pd.date_range("2025-01-01", periods=3, freq="h"),
    )
    fast = interpolate_frame(wide)
    per_column = interpolate_frame(wide, methods={"temperature_2m": "linear", "precipitation": "zero"})
    assert np.allclose(fast["temperature_2m"].to_numpy(), [1.0, 2.0, 3.0])
    assert np.allclose(fast["precipitation"].to_numpy(), [0.0, 1.0, 2.0])
    # 降水配置为 zero（前值保持），与默认 linear 快路径不同
    assert per_column["precipitation"].tolist() == [0.0, 0.0, 2.0]


# ------------------------------------------------------- JSON 解析 / 时间戳
def test_complete_timestamps_fills_missing_parts_and_normalizes_tz():
    """时间戳自动补全：缺日期 / 缺年份 / 缺分秒 / 全角 / 中文时 / 时区 / epoch。"""
    series = pd.Series(
        [
            "2025-01-01 05:00",     # 完整
            "05:00",                # 缺日期 → 补上下文日期
            "01-02 06:00",          # 缺年份 → 补上下文年份
            "2025-01-03 7",         # 缺分秒
            "2025/1/4 8时",          # 斜杠 + 中文「时」
            "2025-01-05T09:00:00+08:00",  # 带时区 → 转 naive
            "2025-01-05 01:00:00Z",       # UTC → 东八区 09:00
            1735689600,             # epoch 秒
        ]
    )
    parsed = complete_timestamps(series, context_date="2025-01-01")
    expected = [
        "2025-01-01 05:00:00",
        "2025-01-01 05:00:00",
        "2025-01-02 06:00:00",
        "2025-01-03 07:00:00",
        "2025-01-04 08:00:00",
        "2025-01-05 09:00:00",
        "2025-01-05 09:00:00",
        "2025-01-01 08:00:00",
    ]
    assert [str(pd.Timestamp(v)) for v in parsed.tolist()] == expected
    assert parsed.dt.tz is None  # 统一成 naive（Asia/Shanghai 墙钟）


def test_parse_json_records_multiple_shapes_and_aliases():
    """JSON 解析：多种外层结构 + 中英文列别名归一。"""
    # 1) {"data": [...]} + 中文列名 + 缺单位
    df = parse_json_records(
        {"data": [{"时间": "2025-01-01 05:00", "地区": "广东", "数值": 100}]},
        unit="MW",
    )
    assert list(df.columns) == ["ts", "region", "value", "unit"]
    assert df.iloc[0].to_dict() == {
        "ts": pd.Timestamp("2025-01-01 05:00"),
        "region": "广东",
        "value": 100.0,
        "unit": "MW",
    }

    # 2) {"code":0,"result":{"rows":[...]}} + 英文别名 + 混合字段名
    payload = {
        "code": 0,
        "result": {
            "rows": [
                {"timestamp": "2025-01-01 01:00", "province": "江苏", "pv": 10},
                {"time": "2025-01-01 02:00", "province": "江苏", "value": 20},
            ]
        },
    }
    df2 = parse_json_records(payload, unit="MW")
    assert len(df2) == 2
    assert df2["value"].tolist() == [10.0, 20.0]
    assert set(df2["region"]) == {"江苏"}

    # 3) 裸列表 / DataFrame / JSON 文本
    assert len(parse_json_records([{"ts": "2025-01-01 01:00", "region": "A", "value": 1}])) == 1
    frame = pd.DataFrame({"ts": ["2025-01-01 01:00"], "region": ["A"], "value": [1.0], "unit": ["MW"]})
    assert len(parse_json_records(frame)) == 1
    assert len(parse_json_records(json.dumps([{"ts": "2025-01-01 01:00", "region": "A", "value": 1}]))) == 1

    # 4) 非法 JSON / 找不到记录列表
    with pytest.raises(PowerSDKError):
        parse_json_records("{这不是合法 JSON")
    with pytest.raises(PowerSDKError):
        parse_json_records({"code": 0, "message": "ok"})


def test_standardize_records_sorts_and_dedups():
    """标准化：排序 + 同 (ts, region) 去重（保留最后一条）。"""
    frame = pd.DataFrame(
        {
            "ts": ["2025-01-01 05:00", "2025-01-01 04:00", "2025-01-01 05:00"],
            "region": ["广东", "广东", "广东"],
            "value": [200, 100, 300],
            "unit": ["MW", "MW", "MW"],
        }
    )
    out = powersdk.standardize_records(frame)
    assert out["ts"].tolist() == [pd.Timestamp("2025-01-01 04:00"), pd.Timestamp("2025-01-01 05:00")]
    assert out["value"].tolist() == [100.0, 300.0]  # 05:00 的两条保留后者


# ------------------------------------------------------------ 校验与告警
def test_validation_accuracy_on_clean_seeded_data(client, source):
    """干净种子数据准确率 ≥ 0.998（实测 1.0），且不产生告警。"""
    spec = ValidationSpec(value_min=1000.0, value_max=120000.0, unit="MW", name="load")
    for category in ("load", "forecast", "consumption", "coal_price"):
        frame = source.fetch(category, region="广东", start="2025-01-01", end="2025-01-31")
        report = validate(frame, spec_for(category))
        assert report.total_rows == 744
        assert report.accuracy >= 0.998, category
        assert report.accuracy == 1.0, category
        assert report.violations == (), category

    sink = CollectingAlertSink()
    frame = source.fetch("load", region="广东", start="2025-01-01", end="2025-01-31")
    report = validate_and_alert(frame, spec, sink, threshold=0.998)
    assert report.passed(0.998)
    assert len(sink) == 0

    # 完整查询链路同样达标
    df = client.query("load", region="广东", start="2025-01-01", end="2025-01-05")
    assert len(df) == 120
    assert client.last_accuracy() >= 0.998
    assert client.alerts == ()


def spec_for(category: str) -> ValidationSpec:
    """按类别画像派生校验规则（测试辅助）。"""
    from powersdk import spec_for_category

    return spec_for_category(category)


def test_validation_detects_violations_and_emits_alerts():
    """脏数据：准确率掉到阈值以下，并逐条发出告警。"""
    frame = pd.DataFrame(
        {
            "ts": pd.date_range("2025-01-01", periods=6, freq="h"),
            "region": ["广东"] * 6,
            "value": [100.0, 200.0, np.nan, 9e9, 300.0, 400.0],
            "unit": ["MW", "MW", "MW", "MW", "GW", "MW"],
        }
    )
    duplicated = frame.iloc[[0]].copy()
    frame = pd.concat([frame, duplicated], ignore_index=True)

    spec = ValidationSpec(value_min=0.0, value_max=120000.0, unit="MW", name="load")
    report = validate(frame, spec)
    assert report.accuracy < 0.998
    codes = set(report.codes)
    assert {"null_value", "out_of_range", "duplicate_key", "unit_mismatch"} <= codes
    assert any(v.kind == "error" for v in report.errors)
    assert any(v.kind == "warning" for v in report.warnings)
    assert not report.passed(0.998)

    sink = CollectingAlertSink()
    validate_and_alert(frame, spec, sink, threshold=0.998)
    alert_codes = set(sink.codes)
    assert "out_of_range" in alert_codes
    assert "accuracy_below_threshold" in alert_codes
    assert any(a.level == "critical" for a in sink)

    # 自定义 AlertSink（协议兼容性）
    class ListSink:
        def __init__(self) -> None:
            self.seen: list[Alert] = []

        def emit(self, alert: Alert) -> None:
            self.seen.append(alert)

    custom = ListSink()
    assert isinstance(custom, AlertSink)
    validate_and_alert(frame, spec, custom, threshold=0.998)
    assert custom.seen

    # loguru 出口不抛异常即可（stderr 由 pytest 捕获）
    validate_and_alert(frame, spec, LoguruAlertSink(), threshold=0.998)


def test_client_alerts_when_source_is_dirty():
    """端到端：脏数据源 → 客户端查询后能读到告警与掉分的准确率。"""

    class DirtySource:
        def __init__(self, frame: pd.DataFrame) -> None:
            self.frame = frame

        def fetch(self, category, region=None, start=None, end=None, granularity="raw", param=None):
            return self.frame.copy()

    dirty = pd.DataFrame(
        {
            "ts": pd.date_range("2025-01-01", periods=10, freq="h"),
            "region": ["广东"] * 10,
            "value": [1.0] * 9 + [9e9],
            "unit": ["MW"] * 10,
        }
    )
    client = PowerDataClient(source=DirtySource(dirty), env="test", fill=False)
    df = client.query("load", region="广东", start="2025-01-01", end="2025-01-01")
    assert len(df) == 10
    assert client.last_accuracy() < 0.998
    assert "out_of_range" in {a.code for a in client.alerts}
    assert client.healthcheck()["status"] == "ok"


def test_client_fills_missing_values_and_raises_alerts():
    """缺失值：关闭补缺时准确率下降；开启补缺后恢复达标。"""
    source = InMemoryTimeSeriesSource(seed=SEED, missing_rate=0.05)
    strict = PowerDataClient(source=source, env="test", fill=False)
    df = strict.query("load", region="广东", start="2025-01-01", end="2025-01-02", granularity="hourly")
    assert df["value"].isna().any()
    assert strict.last_accuracy() < 0.998
    assert strict.alerts

    filled = PowerDataClient(source=source, env="test", fill=True)
    df2 = filled.query("load", region="广东", start="2025-01-01", end="2025-01-02", granularity="hourly")
    assert df2["value"].notna().all()
    assert filled.last_accuracy() >= 0.998


# ------------------------------------------------------------- 地区拼音索引
def test_pinyin_region_index():
    """pypinyin 地区索引：中文名 / 拼音 / 缩写 / 单字简称 / 区划码互通。"""
    assert region_pinyin("广东") == "guangdong"
    assert to_pinyin_abbr("广东") == "gd"
    assert region_code("广东") == "440000"
    assert region_code("440000") == "440000"
    assert normalize_region(" 广东省 ") == "广东"
    assert normalize_region("内蒙古自治区") == "内蒙古"
    assert normalize_region("广西壮族自治区") == "广西"

    index = default_region_index()
    assert len(index) == 34
    for query in ("广东", "广东省", "guangdong", "GuangDong", "GD", "gd", "粤", "440000"):
        record = index.lookup(query)
        assert record is not None, query
        assert record.name == "广东", query
        assert record.code == "440000"
        assert record.pinyin == "guangdong"
        assert record.abbr == "gd"
        assert "粤" in record.aliases

    assert index.lookup("不存在的地名") is None
    assert region_code("不存在的地名") is None
    assert "广东" in index and "火星" not in index
    with pytest.raises(UnknownRegionError):
        index.resolve("火星")
    # 索引可导出为 DataFrame 便于人工核对
    frame = index.to_frame()
    assert {"code", "name", "pinyin", "abbr"} <= set(frame.columns)


# ------------------------------------------------------------- 多环境配置
def test_config_env_switching(monkeypatch):
    """dev / test / prod 三套环境画像与 POWERSDK_ENV 切换。"""
    assert set(ENV_PROFILES) == {"dev", "test", "prod"}

    dev = Settings.for_env("dev")
    test = Settings.for_env("test")
    prod = Settings.for_env("prod")
    assert dev.env == "dev" and test.env == "test" and prod.env == "prod"
    assert dev.mysql_database == "power_data_dev"
    assert test.mysql_database == "power_data_test"
    assert prod.mysql_database == "power_data_prod"
    assert dev.log_level == "DEBUG" and test.log_level == "INFO" and prod.log_level == "WARNING"
    assert dev.accuracy_threshold < test.accuracy_threshold < prod.accuracy_threshold
    assert prod.batch_size == 100_000 > test.batch_size > dev.batch_size
    assert prod.is_production and test.is_test and not dev.is_production

    # 环境变量选择环境
    monkeypatch.setenv("POWERSDK_ENV", "prod")
    reset_settings()
    assert get_settings().env == "prod"
    assert powersdk.current_env() == "prod"
    assert powersdk.get_settings().dsn.startswith("mysql+pymysql://powersdk_ro@mysql-master.internal:3306/power_data_prod")

    monkeypatch.setenv("POWERSDK_ENV", "test")
    reset_settings()
    assert get_settings().env == "test"

    # 未知环境回退 dev（不崩溃）
    monkeypatch.setenv("POWERSDK_ENV", "staging")
    reset_settings()
    assert get_settings().env == "dev"

    # DSN：密码做 URL 转义
    dsn = Settings.for_env("dev", mysql_password="p@ss/word").dsn
    assert "p%40ss%2Fword" in dsn
    assert dsn.endswith("?charset=utf8mb4")

    # 覆盖项与 as_dict（脱敏）
    custom = Settings.from_env({"POWERSDK_ENV": "test", "POWERSDK_MYSQL_HOST": "10.0.0.8",
                                "POWERSDK_BATCH_SIZE": "50000", "POWERSDK_ACCURACY_THRESHOLD": "0.999"})
    assert custom.mysql_host == "10.0.0.8"
    assert custom.batch_size == 50_000
    assert custom.accuracy_threshold == 0.999
    assert "mysql_password" not in custom.as_dict()
    assert custom.as_dict()["env"] == "test"

    # reset_settings 让缓存失效
    reset_settings()
    monkeypatch.delenv("POWERSDK_ENV", raising=False)
    assert get_settings().env == "dev"


def test_client_env_selection(source):
    """客户端可用 env 参数切换环境画像。"""
    client = PowerDataClient(source=source, env="prod")
    assert client.settings.env == "prod"
    assert client.settings.accuracy_threshold == 0.999
    assert client.settings.mysql_database == "power_data_prod"
    health = client.healthcheck()
    assert health["status"] == "ok"
    assert health["env"] == "prod"
    assert "mysql_password" not in health["settings"]


# --------------------------------------------------- 10 万条向量化处理
def test_vectorized_transform_processes_100k_rows():
    """单批次 10 万条：断言走的是向量化路径且结果正确（不依赖耗时断言）。"""
    hours = -(-100_000 // 34)  # 34 个地区 → 至少 10 万行
    source = InMemoryTimeSeriesSource(seed=SEED, hours=hours)
    raw = source.fetch("load")
    assert len(raw) >= 100_000

    result = vectorized_transform(raw, granularity="hourly", fill="interpolate", method="linear")

    # 1) 走向量化路径
    assert result.row_loops == 0
    assert result.rows >= 100_000
    assert result.regions == 34
    assert result.granularity == "hourly"
    assert result.used("interpolate") and result.used("groupby_resample")
    assert "standardize:sort_dedup" in result.vectorized_ops
    assert result.as_dict()["row_loops"] == 0

    # 2) 结构正确
    frame = result.frame
    assert list(frame.columns) == ["ts", "region", "value", "unit"]
    assert frame["value"].notna().all()
    assert frame["ts"].is_monotonic_increasing
    assert frame.drop_duplicates(["ts", "region"]).shape[0] == len(frame)

    # 3) 数值正确：与「透视 + resample」参照实现逐点一致
    reference = pivot_region(raw).resample("h").mean()
    produced = pivot_region(frame)
    assert list(produced.columns) == list(reference.columns)
    assert np.allclose(produced.to_numpy(), reference.to_numpy(), equal_nan=True)

    # 4) 数值正确：逐地区与 NumPy 参照均值一致
    for region, group in raw.groupby("region", sort=False):
        got = frame.loc[frame["region"] == region, "value"].to_numpy()
        assert np.allclose(got, group["value"].to_numpy())
        assert float(got.mean()) == pytest.approx(float(group["value"].mean()))

    # 5) 聚合口径正确：daily 均值与 numpy 参照一致
    daily = vectorized_transform(raw, granularity="daily", fill="none")
    assert daily.row_loops == 0
    expected_days = len(pd.date_range(raw["ts"].min(), raw["ts"].max(), freq="D"))
    assert daily.rows == expected_days * 34
    first_region = raw["region"].iloc[0]
    subset = raw[raw["region"] == first_region]
    expected = subset.set_index("ts")["value"].resample("D").mean().to_numpy()
    produced_daily = daily.frame.loc[daily.frame["region"] == first_region, "value"].to_numpy()
    assert np.allclose(produced_daily, expected)


# ------------------------------------------------------------- 通用工具函数
def test_utils_resample_fill_pivot_units_records(source):
    """重采样 / 补缺 / 透视 / 清洗 / 单位归一 / 记录导出。"""
    frame = source.fetch("load", region="广东", start="2025-01-01", end="2025-01-02")

    # 重采样
    daily = powersdk.resample(frame, "daily")
    assert len(daily) == 2
    assert daily["value"].iloc[0] == pytest.approx(float(frame["value"].iloc[:24].mean()))
    raw = powersdk.resample(frame, "raw")
    assert len(raw) == len(frame)
    assert powersdk.resample(frame, freq="12h").shape[0] == 4

    # 补缺：人为挖掉 3 个小时
    holed = frame.drop(index=[5, 6, 7]).reset_index(drop=True)
    filled = fill_gaps(holed, method="linear", freq="h")
    assert len(filled) == 48
    assert filled["value"].notna().all()
    # 04:00 与 08:00 之间线性插值：05:00 取 1/4，06:00 取中点，07:00 取 3/4
    left, right = float(frame["value"].iloc[4]), float(frame["value"].iloc[8])
    assert filled["value"].iloc[5] == pytest.approx(left + (right - left) / 4)
    assert filled["value"].iloc[6] == pytest.approx((left + right) / 2)
    assert filled["value"].iloc[7] == pytest.approx(left + 3 * (right - left) / 4)
    # zero（前值保持）与 linear 结果不同
    zero_filled = fill_gaps(holed, method="zero", freq="h")
    assert zero_filled["value"].iloc[5] == pytest.approx(left)
    assert zero_filled["value"].iloc[5] != pytest.approx(filled["value"].iloc[5])

    # 透视
    wide = pivot_region(source.fetch("load", region=["广东", "江苏"], start="2025-01-01", end="2025-01-01"))
    assert list(wide.columns) == ["广东", "江苏"]
    assert wide.shape == (24, 2)

    # 清洗：明显尖峰被削到「中位数 + 4σ(MAD)」上界
    spiky = frame.copy()
    spiky.loc[10, "value"] = 1e9
    values = spiky["value"].to_numpy(dtype="float64")
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median))) * 1.4826
    clipped = clean_outliers(spiky, n_sigma=4.0, action="clip")
    assert clipped["value"].iloc[10] < 1e9
    assert float(clipped["value"].iloc[10]) == pytest.approx(median + 4.0 * mad)
    # 其余点不受影响
    kept = clipped.drop(index=10)
    assert np.allclose(kept["value"].to_numpy(), spiky.drop(index=10)["value"].to_numpy())
    dropped = clean_outliers(spiky, action="drop")
    assert dropped["value"].iloc[10] != dropped["value"].iloc[10]  # NaN

    # 单位归一：GW → MW
    in_gw = frame.assign(unit="GW", value=frame["value"] / 1000.0)
    converted = standardize_units(in_gw, target="MW")
    assert set(converted["unit"]) == {"MW"}
    assert np.allclose(converted["value"].to_numpy(), frame["value"].to_numpy())
    # 无法识别的单位（价格）保持原样
    price = source.fetch("electricity_price", region="广东", start="2025-01-01", end="2025-01-01")
    assert np.allclose(standardize_units(price, target="MW")["value"].to_numpy(), price["value"].to_numpy())

    # 记录导出
    records = to_records(frame.head(2))
    assert len(records) == 2
    assert records[0]["ts"] == "2025-01-01T00:00:00"
    assert set(records[0]) == {"ts", "region", "value", "unit"}
    assert to_records(frame.head(2), ts_format="epoch")[0]["ts"] == 1735689600


def test_source_determinism():
    """同一 seed 数据完全可复现；不同 seed 数据不同。"""
    a = InMemoryTimeSeriesSource(seed=SEED).fetch("load", region="广东", start="2025-01-01", end="2025-01-02")
    b = InMemoryTimeSeriesSource(seed=SEED).fetch("load", region="广东", start="2025-01-01", end="2025-01-02")
    c = InMemoryTimeSeriesSource(seed=SEED + 1).fetch("load", region="广东", start="2025-01-01", end="2025-01-02")
    pd.testing.assert_frame_equal(a, b)
    assert not np.allclose(a["value"].to_numpy(), c["value"].to_numpy())
    # 查询顺序不影响结果（惰性生成 + 按 key 派生种子）
    source = InMemoryTimeSeriesSource(seed=SEED)
    first = source.fetch("weather", param="precipitation", start="2025-01-01", end="2025-01-01")
    second = InMemoryTimeSeriesSource(seed=SEED).fetch(
        "weather", param="precipitation", start="2025-01-01", end="2025-01-01"
    )
    pd.testing.assert_frame_equal(first, second)


# ------------------------------------------------ 地区 / 气象参数 / MySQL 源
def test_client_region_variants_and_multi_region(client):
    """地区参数支持中文名 / 拼音 / 缩写 / 区划码 / 列表。"""
    single = client.query("load", region="guangdong", start="2025-01-01", end="2025-01-01")
    assert set(single["region"]) == {"广东"}
    assert set(client.query("load", region="粤", start="2025-01-01", end="2025-01-01")["region"]) == {"广东"}
    assert set(client.query("load", region="440000", start="2025-01-01", end="2025-01-01")["region"]) == {"广东"}

    multi = client.query("load", region=["广东", "江苏"], start="2025-01-01", end="2025-01-01")
    assert set(multi["region"]) == {"广东", "江苏"}
    assert len(multi) == 48
    assert client.last_accuracy() >= 0.998

    all_regions = client.query("load", start="2025-01-01", end="2025-01-01")
    assert all_regions["region"].nunique() == 34
    assert len(client.regions()) == 34

    with pytest.raises(UnknownRegionError):
        client.query("load", region="火星")


def test_client_weather_param_uses_interp_config(client):
    """气象参数查询：单位、取值范围与插值方法都来自配置表。"""
    df = client.query(
        "weather", region="北京", param="precipitation",
        start="2025-01-01", end="2025-01-03", granularity="daily",
    )
    assert set(df["unit"]) == {"mm"}
    assert (df["value"] >= 0).all()
    assert len(df) == 3
    assert client.last_accuracy() >= 0.998

    # 每个气象参数都能查到，且单位/范围与配置一致
    for name, profile in WEATHER_PARAMS.items():
        frame = client.query("weather", region="北京", param=name, start="2025-01-01", end="2025-01-02")
        assert set(frame["unit"]) == {profile.unit}, name
        assert frame["value"].between(profile.minimum, profile.maximum).all(), name
        # resolve_method 会把别名折叠成规范名（next → bfill），与配置表归一后一致
        assert resolve_method(name) == canonical_method(INTERP_METHODS[name])

    # 非气象类别不支持 param
    with pytest.raises(PowerSDKError):
        client.query("load", region="广东", param="temperature_2m")
    # 未知气象参数
    with pytest.raises(PowerSDKError):
        client.query("weather", region="北京", param="不存在的参数")


def test_client_query_json_endpoint(client):
    """query_json：直接消费上游 JSON 载荷。"""
    payload = {
        "data": [
            {"时间": "2025-01-01 01:00", "地区": "广东", "数值": 30000},
            {"时间": "2025-01-01 02:00", "地区": "广东", "数值": 31000},
        ]
    }
    df = client.query_json(payload, category="load")
    assert list(df.columns) == ["ts", "region", "value", "unit"]
    assert len(df) == 2
    assert client.last_accuracy() >= 0.998


def test_mysql_source_stub_is_offline_safe():
    """MySQL 源：SQL 拼装 / 行转表 / 注入连接取数可单测，缺配置时给出明确报错。"""

    source = MySQLTimeSeriesSource(
        dsn="mysql+pymysql://u:p@127.0.0.1:3306/power_data_test?charset=utf8mb4"
    )

    # 表定位：大区拼音 + 产品，不再假设「一类一张长表」
    assert source.weather_table("华北") == "wx_huabei_hist_rt"
    assert (
        MySQLTimeSeriesSource(product="daily_fc").weather_table("港澳台")
        == "wx_gangat_daily_fc"
    )
    with pytest.raises(DataSourceError, match="未知大区"):
        source.weather_table("华西")
    with pytest.raises(ValueError, match="未知气象产品"):
        MySQLTimeSeriesSource(product="weekly")

    sql, params = source.build_weather_sql(
        "wx_huanan_hist_rt",
        "temperature_2m",
        province_codes=("440000",),
        start="2025-01-01",
        end="2025-01-02",
    )
    assert "ROUND(AVG(`temperature_2m`), 4) AS value" in sql
    assert "GROUP BY ts, province_code" in sql
    assert "province_code IN (%s)" in sql and "ts >= %s" in sql and "ts <= %s" in sql
    # WHERE 子句顺序 == 参数顺序，错位时 MySQL 不报错只返回错值
    assert params[0] == "hourly"
    assert params[1] == pd.Timestamp("2025-01-01").to_pydatetime()
    # 区间上界取到「当日 23:59:59」整秒：HTTP 侧 parse_day 只认到秒，
    # 两条路径必须是同一个上界，否则换个数据源就多一行/少一行
    assert params[2] == datetime(2025, 1, 2, 23, 59, 59)
    assert params[3] == "440000"

    # 风向是圆周量：圆均值 + 先 +360 再 MOD（MySQL 的 MOD 符号跟随被除数）
    direction_sql, _ = source.build_weather_sql("wx_huabei_hist_rt", "wind_direction_10m")
    assert "MOD(DEGREES(ATAN2(AVG(SIN(RADIANS(`wind_direction_10m`)))" in direction_sql
    assert "+ 360, 360)" in direction_sql
    assert "MAX(`weather_code`) AS value" in source.build_weather_sql(
        "wx_huabei_hist_rt", "weather_code"
    )[0]
    with pytest.raises(UnavailableParamError, match="未采集"):
        source.build_weather_sql("wx_huabei_hist_rt", "visibility")

    # 游标行 → 标准长表
    rows = [("2025-01-01 01:00", "广东", 30000.0, "MW")]
    frame = MySQLTimeSeriesSource.rows_to_frame(rows)
    assert list(frame.columns) == ["ts", "region", "value", "unit"]
    assert frame.iloc[0]["region"] == "广东"

    # 缺 DSN / 连接时给出可读错误（离线安全，不触网）
    with pytest.raises(PowerSDKError, match="DSN"):
        MySQLTimeSeriesSource().connect()

    client = PowerDataClient(source=MySQLTimeSeriesSource(), env="test")
    health = client.healthcheck()
    assert health["status"] == "degraded"
    assert "DSN" in health["detail"]


class FakeConnection:
    """按 SQL 里的表名关键词路由结果集，模拟 wx_stations / wx_* / market_prices。

    路由项为 ``(关键词, 行, 列名?)``；``dict_rows=True`` 时按列名包成 dict，
    模拟 ``pymysql.cursors.DictCursor``（项目里 ``newsagg.db`` 的默认游标）。
    """

    def __init__(self, routes: list[tuple], *, dict_rows: bool = False) -> None:
        self.routes = [
            (route[0], list(route[1]), route[2] if len(route) > 2 else None)
            for route in routes
        ]
        self.dict_rows = dict_rows
        self.executed: list[tuple[str, tuple]] = []

    def cursor(self):
        outer = self

        class _Cursor:
            rows: list = []
            names = None

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            @property
            def description(self):
                return tuple((name,) for name in (self.names or ()))

            def execute(self, sql, params=()):
                outer.executed.append((sql, tuple(params)))
                self.rows, self.names = next(
                    ((rows, names) for _key, rows, names in outer.routes if _key in sql),
                    ([], None),
                )

            def fetchall(self):
                if outer.dict_rows and self.names:
                    return [dict(zip(self.names, row, strict=True)) for row in self.rows]
                return list(self.rows)

        return _Cursor()


def test_mysql_source_fetches_weather_as_province_series():
    """宽表 → 长表：按省汇总、省份码翻成 SDK 地区名、单位取库口径（风速是 km/h）。"""
    connection = FakeConnection(
        [
            ("`wx_stations`", [("440000", "华南")]),
            (
                "`wx_huanan_hist_rt`",
                [("2025-01-01 00:00:00", "440000", 18.5), ("2025-01-01 01:00:00", "440000", 18.1)],
            ),
        ]
    )
    source = MySQLTimeSeriesSource(connection=connection)
    frame = source.fetch("weather", region="广东省", start="2025-01-01", end="2025-01-01")

    assert list(frame.columns) == ["ts", "region", "value", "unit"]
    assert len(frame) == 2
    assert set(frame["region"]) == {"广东"}
    assert list(frame["value"]) == [18.5, 18.1]
    assert set(frame["unit"]) == {"°C"}
    # 只查了广东所在的华南表，没有把 8 张表都扫一遍
    tables = [sql for sql, _ in connection.executed if "AS value" in sql]
    assert len(tables) == 1 and "`wx_huanan_hist_rt`" in tables[0]

    wind = MySQLTimeSeriesSource(
        connection=FakeConnection(
            [
                ("`wx_stations`", [("150000", "华北")]),
                ("`wx_huabei_hist_rt`", [("2025-01-01 00:00:00", "150000", 12.3)]),
            ]
        )
    )
    gust = wind.fetch("气象", region="内蒙古", param="wind_speed_10m")
    assert gust.iloc[0]["region"] == "内蒙古"
    assert gust.iloc[0]["unit"] == "km/h"


def test_mysql_source_maps_param_names_to_real_columns():
    """SDK 的参数名与库列名不同名（cloudcover vs cloud_cover），改名表必须生效。"""
    assert weather_column("cloudcover") == "cloud_cover"
    assert weather_column("evapotranspiration") == "et0_fao_evapotranspiration"
    assert weather_column(None) == "temperature_2m"
    assert weather_column("cloud_cover") == "cloud_cover"
    for missing in ("visibility", "apparent_temperature", "sunshine_duration"):
        with pytest.raises(UnavailableParamError, match="未采集"):
            weather_column(missing)


def test_mysql_source_market_uses_bound_dataset_and_grade():
    """行情类别绑到 dataset + 默认口径：不定 grade 就没法压成一条序列。"""
    connection = FakeConnection(
        [
            (
                "market_prices",
                [("2026-01-01", "广东", 318.5, "元/兆瓦时"), ("2026-01-02", "广东", 322.0, "元/兆瓦时")],
            )
        ]
    )
    source = MySQLTimeSeriesSource(connection=connection)
    frame = source.fetch("电价", region="广东", start="2026-01-01", end="2026-01-31")

    sql, params = connection.executed[0]
    assert "SELECT price_date AS ts, region, value, unit FROM `market_prices`" in sql
    assert params == ("power_spot", "广东", "日前", date(2026, 1, 1), date(2026, 1, 31))
    assert list(frame["unit"]) == ["元/兆瓦时", "元/兆瓦时"]
    assert frame.iloc[1]["value"] == 322.0

    # param 就是口径开关；全国写法折叠成采集侧约定（全国用 CN，不用 NULL）
    connection.executed.clear()
    source.fetch("coal_price", region="全国", param="到岸标煤单价")
    _sql, params = connection.executed[0]
    assert params[0] == "ceci_import" and params[1] == "CN" and params[2] == "到岸标煤单价"


def test_mysql_source_refuses_to_fabricate_unbacked_categories():
    """负荷/风光水/用电量……库里没有，必须报错而不是返回合成曲线或空表。"""
    assert backed_categories() == ("weather", "electricity_price", "coal_price")
    assert "load" in unbacked_categories()

    source = MySQLTimeSeriesSource(connection=FakeConnection([]))
    for category in ("load", "风电", "supply_gap", "forecast"):
        with pytest.raises(UnsupportedCategoryError, match="没有") as excinfo:
            source.fetch(category)
        # 消息里必须给出「哪几类有数」和「想要演示数据该用什么」，
        # 否则调用方只能回来翻源码猜。
        message = str(excinfo.value)
        assert "weather" in message and "InMemoryTimeSeriesSource" in message


def test_mysql_source_missing_station_rows_is_loud():
    """省份在 wx_stations 里没有站点 → 报错列出缺谁，而不是悄悄少返一省。"""
    source = MySQLTimeSeriesSource(
        connection=FakeConnection([("`wx_stations`", [("440000", "华南")])])
    )
    with pytest.raises(DataSourceError, match="530000"):
        source.fetch("weather", region=["广东", "云南"])


def test_mysql_source_is_cursor_class_agnostic():
    """注入 DictCursor 的连接不能换结果——项目里的 newsagg.db 就是 dict 游标。

    dict 迭代出来的是**列名不是值**，不归一化就会把字符串 "province_code"
    当行政区划码去解析，报一个看起来像数据问题的错。
    """
    routes = [
        ("`wx_stations`", [("440000", "华南")], ("province_code", "region")),
        (
            "`wx_huanan_hist_rt`",
            [("2025-01-01 00:00:00", "440000", 18.5), ("2025-01-01 01:00:00", "440000", 18.1)],
            ("ts", "province_code", "value"),
        ),
    ]
    plain = MySQLTimeSeriesSource(connection=FakeConnection(routes)).fetch(
        "weather", region="广东"
    )
    mapped = MySQLTimeSeriesSource(connection=FakeConnection(routes, dict_rows=True)).fetch(
        "weather", region="广东"
    )
    pd.testing.assert_frame_equal(plain, mapped)
    assert mapped["region"].tolist() == ["广东", "广东"]
    assert mapped["value"].tolist() == [18.5, 18.1]


def test_category_bindings_are_the_single_source_of_truth():
    """11 类都要在落点表里有格子，行情类必须钉死默认口径。

    ``market_prices`` 同一个 (region, price_date) 有多条 grade 行，
    dataset 或 default_grade 少一个，取数就会把不同口径平均在一起——
    那不是「少个参数」，是给出一个没人认得的数字。
    """
    assert set(CATEGORY_BINDINGS) == {c.value for c in Category}
    for category in backed_categories():
        binding = CATEGORY_BINDINGS[category]
        if binding.kind == "market":
            assert binding.dataset and binding.default_grade, category
        else:
            assert binding.kind == "weather", category
    assert all(CATEGORY_BINDINGS[c] is None for c in unbacked_categories())


# --------------------------------------------------------- HTTP 数据源
def _envelope(items, meta=None):
    """统一响应信封：``{"code":0,"data":{"meta":...,"items":...}}``。"""
    return {
        "code": 0,
        "data": {"meta": meta or {}, "items": items},
        "request_id": "test-request",
    }


class Recorder:
    """假 transport：记录 URL/头部，按 URL 子串路由到脚本化的响应队列（不触网）。

    气象一次 fetch 会问 8 个大区，真实服务端对不含目标省的大区返回**空行**，
    所以这里也必须「命中路由才有数」：若所有路径都吐同一页，
    用例里就会出现 8 倍重复行，长表形状是否唯一就测不出来了。
    队列取完后返回空信封。
    """

    def __init__(self, routes=()):
        self.routes = [(needle, list(script)) for needle, script in routes]
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        for needle, script in self.routes:
            if needle in url and script:
                return script.pop(0)
        return _envelope([])


def test_http_source_requests_province_level_from_api():
    """HTTP 源把「同省多站」的汇总交给服务端，自己只负责长表形状。"""
    rows = [
        {"ts": "2025-01-01 00:00:00", "province_code": "440000", "temperature_2m": 18.5},
        {"ts": "2025-01-01 01:00:00", "province_code": "440000", "temperature_2m": 18.1},
    ]
    recorder = Recorder(
        [("华南", [_envelope(rows, {"units": {"temperature_2m": "°C"}, "limit": 5000, "returned": 2})])]
    )
    source = HttpTimeSeriesSource(
        base_url="http://midgate.test/api/v1", token="pk_test", transport=recorder
    )
    frame = source.fetch("weather", region="广东", start="2025-01-01", end="2025-01-01")

    assert list(frame.columns) == ["ts", "region", "value", "unit"]
    assert len(frame) == 2
    assert frame.iloc[0]["region"] == "广东"
    assert frame.iloc[1]["value"] == 18.1
    # 省份落在哪个大区不由 SDK 猜：8 个大区各问一次，只有华南有数
    assert len(recorder.calls) == 8

    url, headers = recorder.calls[0]
    assert url.startswith("http://midgate.test/api/v1/weather/")
    assert "level=province" in url and "granularity=hourly" in url
    assert "params=temperature_2m" in url and "province=440000" in url
    # parse_day 不吃 ISO 的 T 分隔符，必须发空格格式
    assert "since=2025-01-01+00%3A00%3A00" in url
    assert "until=2025-01-01+23%3A59%3A59" in url
    assert headers["Authorization"] == "Bearer pk_test"


def test_http_source_paginates_until_short_page():
    """翻页终止条件是「本页不足一页」——服务端 truncated 只在正好取满时为真。"""
    page1 = _envelope(
        [{"ts": f"2025-01-01 0{i}:00:00", "province_code": "440000", "temperature_2m": i} for i in (1, 2)],
        {"units": {"temperature_2m": "°C"}, "limit": 2},
    )
    page2 = _envelope(
        [{"ts": "2025-01-01 03:00:00", "province_code": "440000", "temperature_2m": 3}],
        {"units": {"temperature_2m": "°C"}, "limit": 2},
    )
    recorder = Recorder([("华南", [page1, page2])])
    source = HttpTimeSeriesSource(
        base_url="http://x/api/v1", transport=recorder, page_size=2, product="daily_fc"
    )
    frame = source.fetch("weather", region="广东")

    assert len(frame) == 3
    # 大区间翻页：华南那页取满 2 行后按 offset 续取，其余 7 个大区返回空
    offsets = [int(p.split("offset=")[1].split("&")[0]) for p, _ in recorder.calls]
    assert offsets.count(0) == 8 and 2 in offsets
    assert all("/weather/" in p and "/daily_fc" in p for p, _ in recorder.calls)


def test_http_source_market_sends_dataset_and_grade():
    recorder = Recorder(
        [
            (
                "/market/power_spot",
                [
                    _envelope(
                        [
                            {"price_date": "2026-01-01", "region": "广东", "value": 318.5, "unit": "元/兆瓦时"},
                            {"price_date": "2026-01-02", "region": "广东", "value": 322.0, "unit": "元/兆瓦时"},
                        ],
                        {"dataset": "power_spot", "limit": 5000},
                    )
                ],
            )
        ]
    )
    source = HttpTimeSeriesSource(base_url="http://x/api/v1", transport=recorder)
    frame = source.fetch("电价", region="广东", start="2026-01-01", end="2026-01-31")

    assert list(frame["unit"]) == ["元/兆瓦时", "元/兆瓦时"]
    assert frame.iloc[1]["value"] == 322.0
    url = recorder.calls[0][0]
    assert "/market/power_spot" in url
    assert "grade=%E6%97%A5%E5%89%8D" in url  # 「日前」percent 编码


def test_http_source_surfaces_server_errors_loudly():
    """业务码非 0 / 缺 base_url / 要求 station 层级，都要在客户端就炸出来。"""
    error = {"code": 3001, "detail": "未知气象产品 'hourly_rt'"}
    source = HttpTimeSeriesSource(
        base_url="http://x/api/v1", transport=Recorder([("华南", [error])])
    )
    with pytest.raises(DataSourceError, match="未知气象产品"):
        source.fetch("weather", region="广东")

    # 地区名在发请求前就被索引拦住，不该浪费一次 HTTP
    silent = Recorder()
    with pytest.raises(UnknownRegionError, match="火星"):
        HttpTimeSeriesSource(base_url="http://x/api/v1", transport=silent).fetch(
            "weather", region="火星"
        )
    assert silent.calls == []

    with pytest.raises(ConfigError, match="缺少中台地址"):
        HttpTimeSeriesSource(
            settings=Settings.for_env("test", api_base_url=""), transport=silent
        ).fetch("weather")

    with pytest.raises(ValueError, match="level='province'"):
        HttpTimeSeriesSource(base_url="http://x/api/v1", level="station")

    with pytest.raises(UnsupportedCategoryError, match="HTTP"):
        HttpTimeSeriesSource(base_url="http://x/api/v1", transport=silent).fetch("load")


def test_http_and_mysql_sources_agree_on_shape():
    """两条路径必须给出同一个形状与同一口径，否则换源就换数。"""

    mysql = MySQLTimeSeriesSource(
        connection=FakeConnection(
            [
                ("`wx_stations`", [("440000", "华南")]),
                ("`wx_huanan_hist_rt`", [("2025-01-01 00:00:00", "440000", 18.5)]),
            ]
        )
    )
    http = HttpTimeSeriesSource(
        base_url="http://x/api/v1",
        transport=Recorder(
            [
                (
                    "华南",
                    [
                        _envelope(
                            [
                                {
                                    "ts": "2025-01-01 00:00:00",
                                    "province_code": "440000",
                                    "temperature_2m": 18.5,
                                }
                            ],
                            {"units": {"temperature_2m": "°C"}},
                        )
                    ],
                )
            ]
        ),
    )
    a = mysql.fetch("weather", region="广东", start="2025-01-01", end="2025-01-01")
    b = http.fetch("weather", region="广东", start="2025-01-01", end="2025-01-01")
    assert list(a.columns) == list(b.columns)
    assert a["region"].tolist() == b["region"].tolist()
    assert a["unit"].tolist() == b["unit"].tolist()
    assert float(a["value"].iloc[0]) == float(b["value"].iloc[0])


def test_daily_radiation_unit_follows_granularity():
    """逐日辐照是「日累计 MJ/m²」，逐时是「W/m²」——同一个列名两种量纲。

    量纲差着 ×24/1e6，贴着 W/m² 返回日累计等于「单位错、数字对」，
    调用方按小时均值用它就把一天算成 24 天。两条数据源都得跟着粒度走。
    """
    assert weather_unit("shortwave_radiation") == "W/m²"
    assert weather_unit("shortwave_radiation", "daily") == "MJ/m²"
    assert weather_unit("temperature_2m", "daily") == "°C"

    routes = [
        ("`wx_stations`", [("440000", "华南")]),
        ("`wx_huanan_hist_rt`", [("2024-01-01", "440000", 13.4515)]),
    ]
    source = MySQLTimeSeriesSource(connection=FakeConnection(routes))
    daily = source.fetch(
        "weather", region="广东", granularity="daily", param="shortwave_radiation"
    )
    hourly = source.fetch("weather", region="广东", param="shortwave_radiation")
    assert set(daily["unit"]) == {"MJ/m²"}
    assert set(hourly["unit"]) == {"W/m²"}

    http = HttpTimeSeriesSource(
        base_url="http://x/api/v1",
        transport=Recorder(
            [
                (
                    "华南",
                    [
                        _envelope(
                            [
                                {
                                    "ts": "2024-01-01",
                                    "province_code": "440000",
                                    "shortwave_radiation": 13.4515,
                                }
                            ]
                        )
                    ],
                )
            ]
        ),
    )
    via_http = http.fetch(
        "weather", region="广东", granularity="daily", param="shortwave_radiation"
    )
    assert set(via_http["unit"]) == {"MJ/m²"}


def test_public_api_exports():
    """包级公开 API 完整可用。"""
    assert powersdk.__version__
    for name in powersdk.__all__:
        assert hasattr(powersdk, name), name
    assert len(categories()) == 11
    assert len(GRANULARITIES) == 4
    assert isinstance(InMemoryTimeSeriesSource(seed=SEED), powersdk.TimeSeriesSource)
