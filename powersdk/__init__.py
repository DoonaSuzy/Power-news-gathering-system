"""电力数据中台统一接口 SDK（power-data-sdk）。

一行定位：**业务人员 3 行代码拿到标准化时序数据**。

.. code-block:: python

    from powersdk import PowerDataClient

    client = PowerDataClient()
    df = client.query("load", region="广东", start="2025-01-01",
                      end="2025-01-02", granularity="hourly")

能力清单
--------
* 12 类时序数据（天气 / 电价 / 负荷 / 新能源出力 / 风电 / 光伏 / 水电 / 煤价 /
  气价 / 用电量 / 电力缺口 / 预测），支持 raw / hourly / daily / monthly 多粒度；
* JSON 解析 + 时间戳自动补全 + 时区归一（Asia/Shanghai）；
* 全流程 Pandas / NumPy 向量化，单批次 10 万条实测毫秒级；
* 20+ 气象参数 → 插值方法映射表（配置与逻辑解耦）；
* 多环境配置体系（dev / test / prod，``POWERSDK_ENV`` 切换）；
* 内置数据校验与告警（准确率阈值 0.998，可插拔 AlertSink）；
* pypinyin 地区索引（中文名 / 拼音 / 缩写 / 行政区划码互通）+ loguru 日志。

真实数据 vs 演示数据
--------------------
12 类都在注册表里，但**只有 4 类在真实库里有数**（见 ``backed_categories()``）：

===========================  =====================================
类别                          真实落点
===========================  =====================================
``weather``                   24 张 ``wx_{大区}_{产品}`` 宽表
``electricity_price``          ``market_prices`` / ``power_spot``
``coal_price``                 ``market_prices`` / ``ceci_import``
``lng_price``                  ``market_prices`` / ``lng_spot``
===========================  =====================================

其余 8 类（负荷 / 风光水出力 / 用电量 / 缺口 / 预测）没有采集器在写，
接上 ``MySQLTimeSeriesSource`` 或 ``HttpTimeSeriesSource`` 后会抛
:class:`UnsupportedCategoryError` 说清「没有这份数据」，
而**不会**返回一份看起来很像的合成曲线。想要演示数据请用默认的
:class:`InMemoryTimeSeriesSource`（确定性合成，仅用于跑通流程）。
"""

from __future__ import annotations

from .client import PowerDataClient, default_source
from .config import (
    DEFAULT_ENV,
    ENV_PROFILES,
    ENV_VAR,
    ENVIRONMENTS,
    EnvProfile,
    Settings,
    current_env,
    get_settings,
    normalize_env,
    reset_settings,
    settings_for_env,
)
from .errors import (
    ConfigError,
    DataSourceError,
    PowerSDKError,
    UnavailableParamError,
    UnknownCategoryError,
    UnknownParamError,
    UnknownRegionError,
    UnsupportedCategoryError,
    ValidationError,
)
from .http_source import HttpTimeSeriesSource
from .interpolation import (
    DEFAULT_METHOD,
    INTERP_METHODS,
    METHOD_ALIASES,
    METHOD_CATALOG,
    NATIVE_METHODS,
    SCIPY_METHODS,
    MethodSpec,
    apply_interpolation,
    canonical_method,
    configured_params,
    interpolate_frame,
    resolve_method,
)
from .parsing import (
    COLUMN_ALIASES,
    complete_timestamps,
    load_json_text,
    parse_json_records,
    standardize_records,
)
from .pinyin import (
    PROVINCES,
    SINGLE_CHAR_ALIASES,
    RegionIndex,
    RegionRecord,
    build_region_index,
    default_region_index,
    normalize_region,
    region_code,
    region_pinyin,
    to_pinyin,
    to_pinyin_abbr,
)
from .timeseries import (
    CANONICAL_COLUMNS,
    CATEGORY_BINDINGS,
    CATEGORY_REGISTRY,
    DAILY_RANGE_OVERRIDES,
    DAILY_UNIT_OVERRIDES,
    DEFAULT_REGIONS,
    GRANULARITIES,
    GRANULARITY_ALIASES,
    GRANULARITY_FREQ,
    PROVINCE_AGGREGATION,
    WEATHER_COLUMN_RANGE,
    WEATHER_COLUMN_UNIT,
    WEATHER_PARAM_TO_COLUMN,
    WEATHER_PARAMS,
    WEATHER_PRODUCTS,
    Category,
    CategorySpec,
    InMemoryTimeSeriesSource,
    MySQLTimeSeriesSource,
    SourceBinding,
    TimeSeriesSource,
    WeatherParamSpec,
    backed_categories,
    categories,
    category_spec,
    granularity_freq,
    province_projection,
    resolve_category,
    resolve_granularity,
    unbacked_categories,
    weather_column,
    weather_range,
    weather_unit,
)
from .utils import (
    UNIT_FACTORS,
    VectorizedResult,
    clean_outliers,
    fill_gaps,
    pivot_region,
    resample,
    standardize_units,
    to_records,
    vectorized_transform,
)
from .validation import (
    DEFAULT_THRESHOLD,
    UNIT_ALIASES,
    Alert,
    AlertSink,
    CollectingAlertSink,
    LoguruAlertSink,
    ValidationReport,
    ValidationSpec,
    Violation,
    alerts_from_report,
    canonical_unit,
    spec_for_category,
    validate,
    validate_and_alert,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # 门面
    "PowerDataClient",
    "default_source",
    # 配置
    "Settings", "EnvProfile", "ENV_PROFILES", "ENVIRONMENTS", "DEFAULT_ENV", "ENV_VAR",
    "get_settings", "reset_settings", "settings_for_env", "current_env", "normalize_env",
    # 类别 / 粒度 / 数据源
    "Category", "CategorySpec", "categories", "category_spec", "resolve_category",
    "CATEGORY_REGISTRY", "GRANULARITIES", "GRANULARITY_FREQ", "GRANULARITY_ALIASES",
    "resolve_granularity", "granularity_freq", "CANONICAL_COLUMNS",
    "TimeSeriesSource", "InMemoryTimeSeriesSource", "MySQLTimeSeriesSource",
    "HttpTimeSeriesSource",
    "DEFAULT_REGIONS", "WEATHER_PARAMS", "WeatherParamSpec",
    # 真实库落点（SDK 类别 ↔ 物理 schema）
    "CATEGORY_BINDINGS", "SourceBinding", "backed_categories", "unbacked_categories",
    "WEATHER_PRODUCTS", "WEATHER_PARAM_TO_COLUMN", "WEATHER_COLUMN_UNIT",
    "DAILY_UNIT_OVERRIDES", "weather_unit",
    "WEATHER_COLUMN_RANGE", "DAILY_RANGE_OVERRIDES", "weather_range",
    "PROVINCE_AGGREGATION", "province_projection", "weather_column",
    # 插值
    "INTERP_METHODS", "METHOD_CATALOG", "METHOD_ALIASES", "NATIVE_METHODS", "SCIPY_METHODS",
    "DEFAULT_METHOD", "MethodSpec", "resolve_method", "canonical_method",
    "apply_interpolation", "interpolate_frame", "configured_params",
    # 解析
    "parse_json_records", "load_json_text", "complete_timestamps", "standardize_records",
    "COLUMN_ALIASES",
    # 校验 / 告警
    "ValidationSpec", "Violation", "ValidationReport", "validate", "validate_and_alert",
    "spec_for_category", "alerts_from_report", "DEFAULT_THRESHOLD",
    "UNIT_ALIASES", "canonical_unit",
    "Alert", "AlertSink", "CollectingAlertSink", "LoguruAlertSink",
    # 工具
    "resample", "fill_gaps", "pivot_region", "clean_outliers", "standardize_units",
    "to_records", "vectorized_transform", "VectorizedResult", "UNIT_FACTORS",
    # 地区索引
    "RegionRecord", "RegionIndex", "build_region_index", "default_region_index",
    "normalize_region", "region_pinyin", "region_code", "to_pinyin", "to_pinyin_abbr",
    "PROVINCES", "SINGLE_CHAR_ALIASES",
    # 异常
    "PowerSDKError", "ConfigError", "DataSourceError", "UnknownCategoryError",
    "UnsupportedCategoryError", "UnavailableParamError",
    "UnknownRegionError", "UnknownParamError", "ValidationError",
]
