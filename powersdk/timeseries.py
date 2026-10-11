"""11 类时序数据的类别注册表与数据源抽象。

模块内容
--------
* :class:`Category` —— 11 类时序数据枚举（天气、电价、负荷、新能源出力、风电、
  光伏、水电、煤价、用电量、电力缺口、预测）；
* :data:`CATEGORY_REGISTRY` —— 每类的单位、量级、取值范围、日周期形状、默认
  插值参数、中文别名（**配置化**，新增类别只改这张表）；
* :data:`WEATHER_PARAMS` —— 18 个气象参数的取值画像；
* :data:`CATEGORY_BINDINGS` —— 每类数据在**真实库**里的落点（气象宽表 / 行情长表 /
  尚无数据）。这是「SDK 类别」与「物理 schema」之间唯一的映射表；
* :class:`TimeSeriesSource` —— 数据源协议（Protocol）；
  :class:`InMemoryTimeSeriesSource` 为**确定性**内存源（固定随机种子，保证测试
  可复现），:class:`MySQLTimeSeriesSource` 直连真实库的 ``wx_*`` / ``market_prices``。

真实库长什么样
--------------
气象是 **8 大区 × 3 产品 = 24 张同构宽表** ``wx_{大区拼音}_{产品}``，一行是
「某气象站某时刻的 10 个要素」；行情是单张长表 ``market_prices``
（``dataset / region / grade / price_date / value / unit``）。
**没有** ``power_timeseries_*`` 这种「一类一张长表」的表，所以 SDK 的职责是
把宽表读成长表，而不是假设库长成 SDK 的样子。
"""

from __future__ import annotations

import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from .errors import (
    DataSourceError,
    UnavailableParamError,
    UnknownCategoryError,
    UnknownParamError,
    UnknownRegionError,
    UnsupportedCategoryError,
)
from .pinyin import PROVINCES, RegionIndex, default_region_index

__all__ = [
    "GRANULARITIES",
    "GRANULARITY_FREQ",
    "GRANULARITY_ALIASES",
    "DEFAULT_START",
    "DEFAULT_HOURS",
    "DEFAULT_REGIONS",
    "CANONICAL_COLUMNS",
    "Category",
    "CategorySpec",
    "WeatherParamSpec",
    "WEATHER_PARAMS",
    "CATEGORY_REGISTRY",
    "WEATHER_PRODUCTS",
    "WEATHER_REGION_CODES",
    "WEATHER_PARAM_TO_COLUMN",
    "WEATHER_COLUMN_UNIT",
    "DAILY_UNIT_OVERRIDES",
    "PROVINCE_AGGREGATION",
    "MARKET_REGION_ALIASES",
    "MARKET_REGION_LABELS",
    "SourceBinding",
    "CATEGORY_BINDINGS",
    "UNBACKED_NOTES",
    "backed_categories",
    "unbacked_categories",
    "weather_column",
    "weather_unit",
    "province_projection",
    "categories",
    "category_spec",
    "resolve_category",
    "resolve_granularity",
    "granularity_freq",
    "TimeSeriesSource",
    "InMemoryTimeSeriesSource",
    "MySQLTimeSeriesSource",
]

#: 统一输出列顺序
CANONICAL_COLUMNS: tuple[str, ...] = ("ts", "region", "value", "unit")

#: 支持的时间粒度
GRANULARITIES: tuple[str, ...] = ("raw", "hourly", "daily", "monthly")

#: 粒度 → pandas 频率（``raw`` 表示不重采样）
GRANULARITY_FREQ: dict[str, str | None] = {
    "raw": None,
    "hourly": "h",
    "daily": "D",
    "monthly": "ME",
}

#: 粒度别名（业务侧习惯写法 → 规范粒度）
GRANULARITY_ALIASES: dict[str, str] = {
    "raw": "raw", "tick": "raw", "原始": "raw", "none": "raw",
    "hourly": "hourly", "hour": "hourly", "h": "hourly", "1h": "hourly",
    "60min": "hourly", "小时": "hourly", "时": "hourly",
    "daily": "daily", "day": "daily", "d": "daily", "1d": "daily", "日": "daily",
    "monthly": "monthly", "month": "monthly", "m": "monthly", "月": "monthly",
}

#: 内存源默认时间窗（2025 年 1 月整月，逐小时）
DEFAULT_START: str = "2025-01-01"
DEFAULT_HOURS: int = 744

#: 默认覆盖的 34 个省级行政区
DEFAULT_REGIONS: tuple[str, ...] = tuple(name for _code, name in PROVINCES)


class Category(StrEnum):
    """11 类时序数据类别。"""

    WEATHER = "weather"
    ELECTRICITY_PRICE = "electricity_price"
    LOAD = "load"
    NEW_ENERGY_OUTPUT = "new_energy_output"
    WIND_POWER = "wind_power"
    SOLAR_POWER = "solar_power"
    HYDRO_POWER = "hydro_power"
    COAL_PRICE = "coal_price"
    CONSUMPTION = "consumption"
    SUPPLY_GAP = "supply_gap"
    FORECAST = "forecast"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class CategorySpec:
    """类别画像：单位 / 量级 / 取值范围 / 日周期形状 / 默认插值参数。"""

    value: str
    unit: str
    level: float
    noise: float
    minimum: float
    maximum: float
    shape: str = "load"
    interp_param: str | None = None
    default_granularity: str = "hourly"
    granularities: tuple[str, ...] = GRANULARITIES
    additive: bool = False
    description: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def category(self) -> Category:
        return Category(self.value)


@dataclass(frozen=True)
class WeatherParamSpec:
    """气象参数取值画像。"""

    name: str
    unit: str
    level: float
    noise: float
    minimum: float
    maximum: float
    shape: str = "static"
    description: str = ""


#: 11 类时序数据注册表
CATEGORY_REGISTRY: dict[str, CategorySpec] = {
    spec.value: spec
    for spec in (
        CategorySpec(
            value="weather", unit="°C", level=15.0, noise=0.15, minimum=-40.0, maximum=60.0,
            shape="diurnal", interp_param="temperature_2m",
            description="气象参数（默认 temperature_2m，可用 param 切换）",
            aliases=("天气", "气象", "weather_data"),
        ),
        CategorySpec(
            value="electricity_price", unit="元/MWh", level=520.0, noise=0.12, minimum=0.0, maximum=2000.0,
            shape="price", interp_param=None,
            description="现货/中长期电价",
            aliases=("电价", "电力价格", "electricityprice"),
        ),
        CategorySpec(
            value="load", unit="MW", level=32000.0, noise=0.06, minimum=1000.0, maximum=120000.0,
            shape="load",
            description="系统用电负荷",
            aliases=("负荷", "用电负荷", "系统负荷"),
        ),
        CategorySpec(
            value="new_energy_output", unit="MW", level=9000.0, noise=0.10, minimum=0.0, maximum=60000.0,
            shape="wind", interp_param=None,
            description="新能源（风+光+水）合计出力",
            aliases=("新能源", "新能源出力", "新能源发电"),
        ),
        CategorySpec(
            value="wind_power", unit="MW", level=6500.0, noise=0.18, minimum=0.0, maximum=40000.0,
            shape="wind",
            description="风电出力",
            aliases=("风电", "风力发电"),
        ),
        CategorySpec(
            value="solar_power", unit="MW", level=4200.0, noise=0.22, minimum=0.0, maximum=30000.0,
            shape="solar",
            description="光伏出力（夜间为 0）",
            aliases=("光伏", "太阳能", "光伏发电"),
        ),
        CategorySpec(
            value="hydro_power", unit="MW", level=5000.0, noise=0.06, minimum=0.0, maximum=30000.0,
            shape="load",
            description="水电出力",
            aliases=("水电", "水力发电"),
        ),
        CategorySpec(
            value="coal_price", unit="元/吨", level=850.0, noise=0.03, minimum=300.0, maximum=2000.0,
            shape="flat",
            description="动力煤价格",
            aliases=("煤价", "动力煤价格", "煤炭价格"),
        ),
        CategorySpec(
            value="consumption", unit="亿kWh", level=65.0, noise=0.05, minimum=5.0, maximum=400.0,
            shape="load",
            description="全社会用电量",
            aliases=("用电量", "消费", "电量"),
        ),
        CategorySpec(
            value="supply_gap", unit="MW", level=300.0, noise=0.60, minimum=-20000.0, maximum=20000.0,
            shape="flat", additive=True,
            description="电力供需缺口（可正可负）",
            aliases=("电力缺口", "缺口", "供需缺口"),
        ),
        CategorySpec(
            value="forecast", unit="MW", level=32500.0, noise=0.04, minimum=1000.0, maximum=120000.0,
            shape="load",
            description="负荷预测值",
            aliases=("预测", "负荷预测", "预报"),
        ),
    )
}

#: 18 个气象参数画像
WEATHER_PARAMS: dict[str, WeatherParamSpec] = {
    spec.name: spec
    for spec in (
        WeatherParamSpec("temperature_2m", "°C", 15.0, 0.15, -40.0, 60.0, "diurnal", "2m 气温"),
        WeatherParamSpec("apparent_temperature", "°C", 14.5, 0.16, -40.0, 60.0, "diurnal", "体感温度"),
        WeatherParamSpec("relative_humidity_2m", "%", 62.0, 0.10, 0.0, 100.0, "static", "2m 相对湿度"),
        WeatherParamSpec("dewpoint_2m", "°C", 10.0, 0.18, -40.0, 50.0, "diurnal", "露点温度"),
        WeatherParamSpec("precipitation", "mm", 0.8, 0.50, 0.0, 200.0, "sparse", "降水量"),
        WeatherParamSpec("rain", "mm", 0.7, 0.50, 0.0, 200.0, "sparse", "降雨量"),
        WeatherParamSpec("snowfall", "cm", 0.05, 0.60, 0.0, 100.0, "sparse", "降雪量"),
        WeatherParamSpec("wind_speed_10m", "m/s", 3.6, 0.30, 0.0, 40.0, "wind", "10m 风速"),
        WeatherParamSpec("wind_gusts_10m", "m/s", 6.5, 0.28, 0.0, 50.0, "wind", "10m 阵风"),
        WeatherParamSpec("wind_direction_10m", "°", 180.0, 0.35, 0.0, 360.0, "wrap", "10m 风向"),
        WeatherParamSpec("pressure_msl", "hPa", 1013.0, 0.004, 950.0, 1050.0, "static", "海平面气压"),
        WeatherParamSpec("surface_pressure", "hPa", 1005.0, 0.005, 940.0, 1045.0, "static", "地面气压"),
        WeatherParamSpec("shortwave_radiation", "W/m²", 190.0, 0.35, 0.0, 1000.0, "rad", "短波辐射"),
        WeatherParamSpec("cloudcover", "%", 52.0, 0.30, 0.0, 100.0, "static", "云量"),
        WeatherParamSpec("visibility", "m", 18000.0, 0.25, 0.0, 50000.0, "static", "能见度"),
        WeatherParamSpec("soil_moisture_0_10cm", "m³/m³", 0.28, 0.12, 0.0, 1.0, "static", "0-10cm 土壤湿度"),
        WeatherParamSpec("evapotranspiration", "mm", 0.25, 0.50, 0.0, 20.0, "rad", "蒸散发"),
        WeatherParamSpec("sunshine_duration", "s", 210.0, 0.30, 0.0, 3600.0, "rad", "日照时长"),
    )
}


# ---------------------------------------------------------------------- 真实库映射
#: 气象「产品」→ 表名后缀。与采集侧 ``newsagg.weather.models.PRODUCTS`` 一致。
WEATHER_PRODUCTS: dict[str, str] = {
    "hist_rt": "历史实时（ERA5 再分析实况）",
    "hist_fc": "历史预测（归档的历史预报）",
    "daily_fc": "每日预测（滚动预报）",
}

#: 大区中文名 → 表名里的 ASCII 段（MySQL 表名用拼音，避免中文名在命令行/备份工具里踩坑）。
WEATHER_REGION_CODES: dict[str, str] = {
    "华北": "huabei",
    "东北": "dongbei",
    "华东": "huadong",
    "华中": "huazhong",
    "华南": "huanan",
    "西南": "xinan",
    "西北": "xibei",
    "港澳台": "gangat",
}

#: SDK 参数名 → 真实表列名。
#:
#: SDK 的 18 个参数画像是「Open-Meteo 能给什么」，库里只有 10 列（计费口径卡在
#: 10 个变量/请求）。所以这里既有改名（``cloudcover`` → ``cloud_cover``、
#: ``evapotranspiration`` → ``et0_fao_evapotranspiration``），也有「压根没采」的
#: 参数——后者必须抛 :class:`~powersdk.errors.UnavailableParamError`，
#: 而不是悄悄换成另一列返回。
WEATHER_PARAM_TO_COLUMN: dict[str, str] = {
    "temperature_2m": "temperature_2m",
    "relative_humidity_2m": "relative_humidity_2m",
    "precipitation": "precipitation",
    "wind_speed_10m": "wind_speed_10m",
    "wind_direction_10m": "wind_direction_10m",
    "shortwave_radiation": "shortwave_radiation",
    "cloudcover": "cloud_cover",
    "cloud_cover": "cloud_cover",
    "pressure_msl": "pressure_msl",
    "evapotranspiration": "et0_fao_evapotranspiration",
    "et0_fao_evapotranspiration": "et0_fao_evapotranspiration",
    "weather_code": "weather_code",
}

#: 真实列名 → 单位。**以库为准**：Open-Meteo 的风速是 km/h，不是 m/s，
#: SDK 的 ``WEATHER_PARAMS`` 画像里写的是 m/s（那是合成数据用的形状，不是库口径）。
WEATHER_COLUMN_UNIT: dict[str, str] = {
    "temperature_2m": "°C",
    "relative_humidity_2m": "%",
    "precipitation": "mm",
    "wind_speed_10m": "km/h",
    "wind_direction_10m": "°",
    "shortwave_radiation": "W/m²",
    "cloud_cover": "%",
    "pressure_msl": "hPa",
    "et0_fao_evapotranspiration": "mm",
    "weather_code": "WMO",
}

#: 逐日行里**单位随粒度切换**的列。
#:
#: 辐照度逐时是瞬时强度 ``W/m²``，逐日行存的是「日累计」``MJ/m²``
#: （采集侧口径见 ``newsagg/weather/models.py`` 的 ``DAILY_AGG_LABEL``）。
#: 两者数值差着 ×24/1e6 的量纲，贴着 ``W/m²`` 返回等于给了个错单位是对的数——
#: 调用方按 W/m² 用就会把日累计当成小时均值。
DAILY_UNIT_OVERRIDES: dict[str, str] = {"shortwave_radiation": "MJ/m²"}


def weather_unit(column: str, granularity: str = "hourly") -> str:
    """列名 → 单位（``daily`` 时套用 :data:`DAILY_UNIT_OVERRIDES`）。"""
    if str(granularity) == "daily":
        return DAILY_UNIT_OVERRIDES.get(column, WEATHER_COLUMN_UNIT[column])
    return WEATHER_COLUMN_UNIT[column]

#: 省级汇总口径（把同省多个气象站压成一条省序列）。
#:
#: 必须与 ``newsplatform/repositories/timeseries.py`` 的 ``PROVINCE_AGGREGATION``
#: 一致，否则「SDK 直连库」和「SDK 走 HTTP」同一查询会给出两个风向。
#: - 风向是圆周量：``AVG(350°, 10°)`` = 180°，正确值是 0°，必须走圆均值；
#: - 天气代码是 WMO 枚举不是数量：``AVG`` 得到 5.12 谁也不认，取 ``MAX``（当日最严重）。
PROVINCE_AGGREGATION: dict[str, str] = {
    "wind_direction_10m": "circular_mean",
    "weather_code": "max",
}


def province_projection(column: str) -> str:
    """列名 → 省级汇总表达式（别名固定为 ``value``，与长表形状对齐）。

    圆均值必须先 ``+360`` 再 ``MOD``：MySQL 的 ``MOD()`` 符号跟随被除数，
    ``MOD(-156.27, 360)`` 仍是负数，值域会漏出 ``[0, 360)``。
    用 ``MOD()`` 而不是 ``%``：PyMySQL 会对语句做 ``%`` 格式化，裸 ``%`` 直接报错。
    """
    kind = PROVINCE_AGGREGATION.get(column, "avg")
    if kind == "circular_mean":
        return (
            f"ROUND(MOD(DEGREES(ATAN2(AVG(SIN(RADIANS(`{column}`))), "
            f"AVG(COS(RADIANS(`{column}`))))) + 360, 360), 4) AS value"
        )
    if kind == "max":
        return f"MAX(`{column}`) AS value"
    return f"ROUND(AVG(`{column}`), 4) AS value"


def weather_column(param: str | None) -> str:
    """SDK 参数名 → 真实列名；未采集的参数抛错而不是换一列返回。"""
    name = str(param or "temperature_2m").strip().lower()
    column = WEATHER_PARAM_TO_COLUMN.get(name)
    if column is None:
        raise UnavailableParamError(param, sorted(set(WEATHER_PARAM_TO_COLUMN.values())))
    return column


@dataclass(frozen=True)
class SourceBinding:
    """某个类别在真实库里的落点。

    :param kind: ``weather``（24 张宽表）/ ``market``（``market_prices`` 长表）
    :param dataset: ``market`` 类别对应的数据集标识
    :param default_grade: 未指定 ``param`` 时用的口径
    :param note: 展示用的中文说明
    """

    kind: str
    dataset: str | None = None
    default_grade: str | None = None
    note: str = ""


#: 11 类 → 真实库落点。``None`` 表示**目前没有这份数据**。
#:
#: 这不是偷懒的占位，而是把「哪些类别真的有数」写成一处可审的表：
#: 负荷/风电/光伏/水电/用电量/缺口/预测目前没有任何采集器在写，
#: 让它们响亮地失败比让它们返回合成曲线安全得多。
#:
#: ``default_grade`` 是必须的而不是贴心的默认值：``market_prices`` 里同一个
#: (region, price_date) 挂着多个口径（现货「日前」「实时」+ 分机组价），
#: 不定口径就没法压成一条序列——求平均是把「日前」和「实时」混成一锅，
#: 放任重复键则是让去重随机丢掉一半。
CATEGORY_BINDINGS: dict[str, SourceBinding | None] = {
    Category.WEATHER.value: SourceBinding("weather"),
    Category.ELECTRICITY_PRICE.value: SourceBinding(
        "market",
        dataset="power_spot",
        default_grade="日前",
        note="电力现货市场价格（默认「日前」口径，可用 param 指定 grade）",
    ),
    Category.COAL_PRICE.value: SourceBinding(
        "market",
        dataset="ceci_import",
        default_grade="到岸标煤单价",
        note="中国电煤采购价格指数（进口到岸标煤单价）",
    ),
    Category.LOAD.value: None,
    Category.NEW_ENERGY_OUTPUT.value: None,
    Category.WIND_POWER.value: None,
    Category.SOLAR_POWER.value: None,
    Category.HYDRO_POWER.value: None,
    Category.CONSUMPTION.value: None,
    Category.SUPPLY_GAP.value: None,
    Category.FORECAST.value: None,
}

#: 未接通类别的说明，供异常消息与 ``catalog()`` 展示。
UNBACKED_NOTES: dict[str, str] = {
    Category.LOAD.value: "省级统调负荷需要接入调度口径数据源，当前无采集器",
    Category.NEW_ENERGY_OUTPUT.value: "新能源出力=风电+光伏+水电，随上述类别一并接入",
    Category.WIND_POWER.value: "风电出力需要省级调度公开发布或内网数据",
    Category.SOLAR_POWER.value: "光伏出力需要省级调度公开发布或内网数据",
    Category.HYDRO_POWER.value: "水电出力需要省级调度公开发布或内网数据",
    Category.CONSUMPTION.value: "全社会用电量是月度统计口径，尚未建采集器",
    Category.SUPPLY_GAP.value: "供需缺口需要负荷与机组可用容量两侧数据",
    Category.FORECAST.value: "负荷预测属于调度内部数据，无公开源",
}


def backed_categories() -> tuple[str, ...]:
    """真实库里目前查得到的类别（MySQL / HTTP 数据源共用这份判定）。"""
    return tuple(c for c, b in CATEGORY_BINDINGS.items() if b is not None)


def unbacked_categories() -> tuple[str, ...]:
    """注册表里有、但真实库还没有数据的类别。"""
    return tuple(c for c, b in CATEGORY_BINDINGS.items() if b is None)


#: 全国口径的写法 → 行情表里的 region 值。
#:
#: 采集侧约定「全国用 ``CN``，不用 NULL」（避免 NULL 语义歧义），
#: 但业务同学会写「全国」「中国」，所以在 SDK 侧折叠。
MARKET_REGION_ALIASES: dict[str, str] = {"全国": "CN", "中国": "CN", "cn": "CN"}

#: 行情表里非省级的区域口径，原样透传（值直接来自 ``market_prices.region``）。
MARKET_REGION_LABELS: frozenset[str] = frozenset(
    {"CN", "南方区域", "北方区域", "华北", "华东", "华中", "华南", "西南", "西北", "东北", "川渝", "不限"}
)


# ---------------------------------------------------------------------- 解析
_CATEGORY_ALIASES: dict[str, Category] = {}
for _spec in CATEGORY_REGISTRY.values():
    for _key in {_spec.value, _spec.category.name.lower(), *_spec.aliases}:
        _CATEGORY_ALIASES[str(_key).strip().lower()] = _spec.category


def categories() -> tuple[str, ...]:
    """返回 11 类时序数据的规范名（注册表顺序）。"""
    return tuple(CATEGORY_REGISTRY)


def category_spec(category: Category | str) -> CategorySpec:
    """类别规范名/枚举 → 类别画像。"""
    return CATEGORY_REGISTRY[resolve_category(category).value]


def resolve_category(category: Category | str | None) -> Category:
    """把类别名 / 中文别名 / 枚举解析为 :class:`Category`。"""
    if isinstance(category, Category):
        return category
    if category is None:
        raise UnknownCategoryError(None, categories())
    key = str(category).strip().lower()
    resolved = _CATEGORY_ALIASES.get(key)
    if resolved is None:
        raise UnknownCategoryError(category, categories())
    return resolved


def resolve_granularity(granularity: str | None) -> str:
    """把粒度别名解析为规范粒度（raw/hourly/daily/monthly）。"""
    if granularity is None:
        return "hourly"
    key = str(granularity).strip().lower()
    resolved = GRANULARITY_ALIASES.get(key)
    if resolved is None:
        raise ValueError(f"未知的时间粒度：{granularity!r}，可用：{'/'.join(GRANULARITIES)}")
    return resolved


def granularity_freq(granularity: str | None) -> str | None:
    """规范粒度 → pandas 频率；``raw`` 返回 ``None``。"""
    return GRANULARITY_FREQ[resolve_granularity(granularity)]


def _to_start(value: object) -> pd.Timestamp:
    """起始时间（日期字符串按当日 00:00 处理）。"""
    return pd.Timestamp(value)


def _to_end(value: object) -> pd.Timestamp:
    """结束时间（日期字符串按当日 23:59:59.999999999 处理，保证「按天」查询含整天）。"""
    stamp = pd.Timestamp(value)
    if stamp == stamp.normalize():
        return stamp + pd.Timedelta(days=1) - pd.Timedelta(1, "ns")
    return stamp


def _db_time(value: object, *, end_of_day: bool = False) -> datetime:
    """时间条件落到库/接口用的秒精度 naive datetime。

    抹掉亚秒有两个理由：

    * ``_to_end`` 给的是 ``…59.999999999``，pandas 转 ``datetime`` 每次都告警
      「Discarding nonzero nanoseconds」，库里塞一个用户看不懂的警告不合适；
    * HTTP 侧的 ``parse_day`` 只认到秒，两条路径的闭区间上界必须是同一个值——
      同一查询换个数据源就多一行/少一行，是最难查的那种差异。
    """
    stamp = _to_end(value) if end_of_day else _to_start(value)
    return stamp.floor("s").to_pydatetime()


# ---------------------------------------------------------------------- 数据源
@runtime_checkable
class TimeSeriesSource(Protocol):
    """时序数据源协议。

    任何实现 ``fetch`` 方法的对象都可作为 :class:`powersdk.PowerDataClient`
    的数据源（内存源、MySQL 源、测试 fake 皆可），SDK 通过依赖注入解耦取数与处理。
    """

    def fetch(
        self,
        category: Category | str,
        region: Sequence[str] | str | None = None,
        start: object = None,
        end: object = None,
        granularity: str = "raw",
        param: str | None = None,
    ) -> pd.DataFrame:  # pragma: no cover - 协议定义
        ...


def _diurnal(shape: str, hours: np.ndarray) -> np.ndarray:
    """日周期形状因子（向量化）。"""
    h = hours.astype("float64")
    if shape in ("solar", "rad"):
        return np.clip(np.sin(np.pi * (h - 6.0) / 12.0), 0.0, None)
    if shape == "wind":
        return np.clip(0.65 + 0.45 * np.sin(2 * np.pi * (h - 3.0) / 24.0), 0.05, None)
    if shape == "load":
        return 0.72 + 0.30 * np.sin(2 * np.pi * (h - 7.0) / 24.0) + 0.14 * np.sin(4 * np.pi * h / 24.0)
    if shape == "price":
        return 0.92 + 0.22 * np.sin(2 * np.pi * (h - 9.0) / 24.0) - 0.12 * np.sin(4 * np.pi * h / 24.0)
    if shape == "diurnal":
        return 1.0 + 0.25 * np.sin(2 * np.pi * (h - 9.0) / 24.0)
    return np.ones_like(h)


def _region_spread(level: float, minimum: float, maximum: float) -> float:
    """按「量级到取值边界还剩多少空间」推导地区间离散度。

    例如海平面气压 1013hPa、合法区间 [950, 1050]：上/下空间只有 ±4%，若仍按
    固定 ±45% 生成，所有值都会被 clip 成同一条直线；据此把离散度压到 ±2.6%，
    既保留地区差异又不越界。
    """
    if level <= 0:
        return 0.20
    low_room = max((level - minimum) / abs(level), 0.0)
    high_room = max((maximum - level) / abs(level), 0.0)
    return min(low_room, high_room, 0.45) * 0.6


def _synthesize(
    rng: np.random.Generator,
    *,
    regions: Sequence[str],
    index: pd.DatetimeIndex,
    level: float,
    noise: float,
    minimum: float,
    maximum: float,
    shape: str,
    seasonal: float = 0.05,
    additive: bool = False,
) -> np.ndarray:
    """确定性合成 ``(地区数, 时间点数)`` 的数值矩阵（全部向量化）。"""
    n_regions, n_hours = len(regions), len(index)
    hours = index.hour.to_numpy()
    day_of_year = index.dayofyear.to_numpy(dtype="float64")
    spread = _region_spread(level, minimum, maximum)
    region_factor = rng.uniform(1.0 - spread, 1.0 + spread, n_regions)

    if shape == "sparse":
        # 间歇型（降水/降雪）：12% 概率出现，其余为 0
        hit = rng.random((n_regions, n_hours)) < 0.12
        amount = rng.gamma(2.0, 1.2, (n_regions, n_hours)) * level
        values = np.where(hit, amount, 0.0)
    else:
        factor = _diurnal(shape, hours)
        season = 1.0 + seasonal * np.sin(2 * np.pi * (day_of_year - 15.0) / 365.0)
        if additive:
            noise_matrix = 1.0 + noise * rng.standard_normal((n_regions, n_hours))
        else:
            # 乘性噪声用对数正态，保证量级为正且不产生负值
            noise_matrix = np.exp(noise * rng.standard_normal((n_regions, n_hours)))
        values = (
            level * region_factor[:, None] * factor[None, :] * season[None, :] * noise_matrix
        )
        if shape == "wrap":  # 风向等角度量：模 360
            values = np.mod(values, 360.0)

    values = np.clip(values, minimum, maximum)
    return np.round(values, 3)


class InMemoryTimeSeriesSource:
    """确定性内存数据源（测试 / 演示 / 离线开发默认源）。

    * 同一 ``(seed, category, param)`` 组合永远产出同一份数据（随机种子由
      ``[seed, crc32(category)]`` 派生，与查询顺序无关）；
    * 逐类别惰性生成并缓存，避免构造时一次性算全量；
    * 全部数值由 NumPy 一次性向量化生成，10 万条级别也在毫秒级完成。
    """

    def __init__(
        self,
        *,
        seed: int = 20250101,
        regions: Sequence[str] = DEFAULT_REGIONS,
        start: str = DEFAULT_START,
        hours: int = DEFAULT_HOURS,
        freq: str = "h",
        missing_rate: float = 0.0,
        region_index: RegionIndex | None = None,
    ) -> None:
        if hours <= 0:
            raise ValueError("hours 必须为正整数")
        if not 0.0 <= missing_rate < 1.0:
            raise ValueError("missing_rate 必须在 [0, 1) 区间")
        self.seed = int(seed)
        self.regions: tuple[str, ...] = tuple(str(r) for r in regions)
        self.start = str(start)
        self.hours = int(hours)
        self.freq = freq
        self.missing_rate = float(missing_rate)
        self.region_index = region_index or default_region_index()
        self._index = pd.date_range(self.start, periods=self.hours, freq=self.freq, name="ts")
        self._cache: dict[str, pd.DataFrame] = {}

    # ---------------------------------------------------------------- 元信息
    @property
    def window(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        """数据覆盖的起止时间戳（含端点）。"""
        return self._index[0], self._index[-1]

    def available_regions(self) -> tuple[str, ...]:
        return self.regions

    def row_count(self, category: Category | str) -> int:
        """某类别全量行数（触发惰性生成）。"""
        return len(self._frame(resolve_category(category).value, None))

    def _rng(self, key: str) -> np.random.Generator:
        """按 key 派生确定性 RNG（crc32 稳定，不受 PYTHONHASHSEED 影响）。"""
        return np.random.default_rng([self.seed, zlib.crc32(key.encode("utf-8"))])

    # ---------------------------------------------------------------- 生成
    def _frame(self, category: str, param: str | None) -> pd.DataFrame:
        cache_key = f"{category}|{param or ''}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        spec = CATEGORY_REGISTRY[category]
        if param is not None and category != Category.WEATHER.value:
            raise UnknownCategoryError(param, categories())
        if category == Category.WEATHER.value or param is not None:
            param_name = param or spec.interp_param or "temperature_2m"
            if param_name not in WEATHER_PARAMS:
                raise UnknownParamError(param_name, tuple(WEATHER_PARAMS))
            profile: CategorySpec | WeatherParamSpec = WEATHER_PARAMS[param_name]
        else:
            profile = spec

        rng = self._rng(cache_key)
        values = _synthesize(
            rng,
            regions=self.regions,
            index=self._index,
            level=profile.level,
            noise=profile.noise,
            minimum=profile.minimum,
            maximum=profile.maximum,
            shape=profile.shape,
            additive=getattr(profile, "additive", False),
        )
        if self.missing_rate > 0:
            values = values.astype("float64", copy=True)
            values[rng.random(values.shape) < self.missing_rate] = np.nan

        n_regions = len(self.regions)
        frame = pd.DataFrame(
            {
                "ts": np.tile(self._index.to_numpy(), n_regions),
                "region": np.repeat(np.asarray(self.regions, dtype=object), len(self._index)),
                "value": values.reshape(-1),
                "unit": profile.unit,
            }
        )
        self._cache[cache_key] = frame
        return frame

    # ---------------------------------------------------------------- 查询
    def fetch(
        self,
        category: Category | str,
        region: Sequence[str] | str | None = None,
        start: object = None,
        end: object = None,
        granularity: str = "raw",
        param: str | None = None,
    ) -> pd.DataFrame:
        """按类别 / 地区 / 时间窗取数，返回 ``[ts, region, value, unit]`` 长表。"""
        cat = resolve_category(category)
        frame = self._frame(cat.value, param)

        mask = pd.Series(True, index=frame.index)
        names = self._region_names(region)
        if names is not None:
            mask &= frame["region"].isin(names)
        if start is not None:
            mask &= frame["ts"] >= _to_start(start)
        if end is not None:
            mask &= frame["ts"] <= _to_end(end)

        result = frame.loc[mask.to_numpy(), list(CANONICAL_COLUMNS)].copy()
        return result.sort_values(["region", "ts"], kind="stable").reset_index(drop=True)

    def _region_names(self, region: Sequence[str] | str | None) -> tuple[str, ...] | None:
        if region is None:
            return None
        queries = [region] if isinstance(region, str) else list(region)
        if not queries:
            return None
        return tuple(self.region_index.resolve(item).name for item in queries)


class MySQLTimeSeriesSource:
    """直连真实库的时序数据源（``wx_*`` 24 张宽表 + ``market_prices`` 长表）。

    三条设计约束
    ------------
    1. **库是宽表，SDK 输出长表**：``wx_{大区拼音}_{产品}`` 一行是「某站某时刻的
       10 个要素」，SDK 的 ``[ts, region, value, unit]`` 需要 ①按 ``param`` 选列
       ②把同省多站压成一条省序列。压缩口径见 :data:`PROVINCE_AGGREGATION`。
    2. **没有的类别就说没有**：负荷/风光水/用电量/缺口/预测当前无采集器在写，
       取数直接抛 :class:`~powersdk.errors.UnsupportedCategoryError`。
       返回合成曲线会让调用方以为拿到了真的负荷。
    3. **省份→大区的归属不硬编码**：查 ``wx_stations`` 得到 ``province_code → region``
       （唯一真源在库里），SDK 只保留 8 个大区的表名片段。采集侧调整归属时不用改 SDK。

    离线安全
    --------
    只有真正取数时才导入 PyMySQL；未安装或连不上库时抛
    :class:`~powersdk.errors.DataSourceError`，消息里说明原因，
    CI / 无网环境可直接换用 :class:`InMemoryTimeSeriesSource`。
    """

    def __init__(
        self,
        settings: object | None = None,
        *,
        dsn: str | None = None,
        product: str = "hist_rt",
        weather_prefix: str = "wx",
        station_table: str = "wx_stations",
        market_table: str = "market_prices",
        region_index: RegionIndex | None = None,
        connection: object | None = None,
    ) -> None:
        if product not in WEATHER_PRODUCTS:
            raise ValueError(
                f"未知气象产品 {product!r}，可选：{', '.join(WEATHER_PRODUCTS)}"
            )
        self.settings = settings
        self.dsn = dsn
        #: 气象产品（hist_rt / hist_fc / daily_fc）——一个源实例读一张产品表族
        self.product = product
        self.weather_prefix = weather_prefix
        self.station_table = station_table
        self.market_table = market_table
        self.region_index = region_index or default_region_index()
        #: 已建立的连接（可注入 fake，便于离线单测 execute/fetch）
        self._connection = connection
        self._province_regions: dict[str, str] | None = None

    # ---------------------------------------------------------------- 表定位
    def weather_table(self, region: str) -> str:
        """大区中文名 → 气象宽表名（如 ``华北`` → ``wx_huabei_hist_rt``）。"""
        segment = WEATHER_REGION_CODES.get(str(region).strip())
        if segment is None:
            raise DataSourceError(
                f"未知大区 {region!r}，可选：{', '.join(WEATHER_REGION_CODES)}"
            )
        return f"{self.weather_prefix}_{segment}_{self.product}"

    def province_region_map(self) -> dict[str, str]:
        """``province_code → 大区中文名``，来自 ``wx_stations``（惰性缓存）。"""
        if self._province_regions is None:
            rows = self.execute(
                f"SELECT province_code, MAX(region) AS region FROM `{self.station_table}` "
                "GROUP BY province_code"
            )
            self._province_regions = {str(r[0]): str(r[1]) for r in rows}
        return self._province_regions

    def group_provinces_by_region(
        self, codes: Sequence[str]
    ) -> dict[str, tuple[str, ...]]:
        """省份代码按大区分组 → ``{大区: (省份代码, ...)}``。

        库里没有该省站点时报错列出缺哪些省，而不是悄悄少返一个省——
        「查 5 个省只回 4 个」是极难被发现的失效方式。
        """
        mapping = self.province_region_map()
        grouped: dict[str, list[str]] = {}
        missing: list[str] = []
        for code in codes:
            region = mapping.get(str(code))
            if region is None:
                missing.append(str(code))
                continue
            grouped.setdefault(region, []).append(str(code))
        if missing:
            raise DataSourceError(
                f"`{self.station_table}` 中没有这些省份的站点记录，无法定位气象分表："
                f"{', '.join(missing)}"
            )
        return {region: tuple(items) for region, items in grouped.items()}

    # ---------------------------------------------------------------- SQL 拼装
    def build_weather_sql(
        self,
        table: str,
        column: str,
        *,
        province_codes: Sequence[str] | None = None,
        start: object = None,
        end: object = None,
        granularity: str = "hourly",
    ) -> tuple[str, tuple]:
        """气象宽表 → 省级长表的参数化查询（纯函数，可单测）。

        ``WHERE`` 子句与 ``params`` 在同一个循环里成对追加，
        占位符顺序就是参数顺序——这两者错位时 MySQL 不报错，只会返回错值。
        """
        if column not in WEATHER_COLUMN_UNIT:
            raise UnavailableParamError(column, sorted(WEATHER_COLUMN_UNIT))
        db_granularity = "daily" if str(granularity) == "daily" else "hourly"

        where = ["granularity = %s"]
        params: list[object] = [db_granularity]
        if start is not None:
            where.append("ts >= %s")
            params.append(_db_time(start))
        if end is not None:
            where.append("ts <= %s")
            params.append(_db_time(end, end_of_day=True))
        codes = tuple(str(c) for c in (province_codes or ()))
        if codes:
            where.append(f"province_code IN ({', '.join(['%s'] * len(codes))})")
            params.extend(codes)

        sql = (
            f"SELECT ts, province_code, {province_projection(column)} "
            f"FROM `{table}` WHERE {' AND '.join(where)} "
            "GROUP BY ts, province_code ORDER BY province_code, ts"
        )
        return sql, tuple(params)

    def build_market_sql(
        self,
        dataset: str,
        *,
        regions: Sequence[str] | None = None,
        grades: Sequence[str] | None = None,
        start: object = None,
        end: object = None,
    ) -> tuple[str, tuple]:
        """行情长表的参数化查询（纯函数，可单测）。"""
        where = ["dataset = %s"]
        params: list[object] = [dataset]
        names = tuple(str(r) for r in (regions or ()))
        if names:
            where.append(f"region IN ({', '.join(['%s'] * len(names))})")
            params.extend(names)
        grade_values = tuple(str(g) for g in (grades or ()))
        if grade_values:
            where.append(f"grade IN ({', '.join(['%s'] * len(grade_values))})")
            params.extend(grade_values)
        if start is not None:
            where.append("price_date >= %s")
            params.append(_db_time(start).date())
        if end is not None:
            where.append("price_date <= %s")
            params.append(_db_time(end, end_of_day=True).date())

        sql = (
            f"SELECT price_date AS ts, region, value, unit FROM `{self.market_table}` "
            f"WHERE {' AND '.join(where)} ORDER BY region, price_date"
        )
        return sql, tuple(params)

    @staticmethod
    def rows_to_frame(rows: Iterable[Sequence], columns: Sequence[str] | None = None) -> pd.DataFrame:
        """游标行 → 标准长表（列名可来自 ``cursor.description``）。"""
        cols = list(columns) if columns else list(CANONICAL_COLUMNS)
        frame = pd.DataFrame(list(rows), columns=cols)
        for column in CANONICAL_COLUMNS:
            if column not in frame.columns:
                frame[column] = None
        return frame[list(CANONICAL_COLUMNS)]

    def connect(self):
        """建立 PyMySQL 连接（惰性导入；不可用时抛出 :class:`DataSourceError`）。"""
        try:
            import pymysql  # noqa: PLC0415 - 故意惰性导入，保证无依赖环境可用
        except ImportError as exc:  # pragma: no cover - 取决于运行环境
            raise DataSourceError(
                "MySQL 数据源需要 PyMySQL：pip install PyMySQL；"
                "离线/测试环境请使用 InMemoryTimeSeriesSource"
            ) from exc
        dsn = self.dsn or getattr(self.settings, "dsn", None)
        if not dsn:
            raise DataSourceError(
                "缺少 MySQL DSN：请传入 dsn、含 dsn 的 settings，或直接注入 connection"
            )
        try:
            self._connection = pymysql.connect(
                host=_dsn_part(dsn, "host") or "127.0.0.1",
                port=int(_dsn_part(dsn, "port") or 3306),
                user=_dsn_part(dsn, "user") or "root",
                password=_dsn_part(dsn, "password") or "",
                database=_dsn_part(dsn, "database") or "",
                charset="utf8mb4",
            )
        except Exception as exc:  # pragma: no cover - 需要真实数据库才会触发
            raise DataSourceError(f"无法连接 MySQL：{exc}") from exc
        return self._connection

    def execute(self, sql: str, params: Sequence = ()) -> list[tuple]:
        """执行查询，按 **SELECT 列序** 返回元组行（无连接时自动建连）。

        为什么要归一化：调用方常注入 ``pymysql.cursors.DictCursor`` 的连接
        （采集侧的 ``newsagg.db`` 就是），而 dict 迭代出来的是**列名不是值**——
        ``for ts, code, value in rows`` 会把字符串 ``"province_code"`` 当省份码去解析。
        换一种游标就换一份结果，这是取数层最不该有的行为。
        """
        connection = self._connection or self.connect()
        with connection.cursor() as cursor:
            cursor.execute(sql, tuple(params))
            rows = list(cursor.fetchall())
            if not rows or not isinstance(rows[0], dict):
                return rows
            names = [d[0] for d in cursor.description]
            return [tuple(row[name] for name in names) for row in rows]

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> MySQLTimeSeriesSource:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def fetch(
        self,
        category: Category | str,
        region: Sequence[str] | str | None = None,
        start: object = None,
        end: object = None,
        granularity: str = "raw",
        param: str | None = None,
    ) -> pd.DataFrame:
        """从真实库取数，返回 ``[ts, region, value, unit]`` 长表。

        ``param`` 的语义随类别切换：气象是**要素列**（``temperature_2m``…），
        行情是**口径/规格**（``grade``，如「日前」「实时」「到岸标煤单价」）。
        两种都拼错时抛错，不做「猜一个」的兜底。
        """
        cat = resolve_category(category)
        binding = CATEGORY_BINDINGS[cat.value]
        if binding is None:
            raise UnsupportedCategoryError(
                cat.value, source="MySQL", available=backed_categories()
            )
        if binding.kind == "weather":
            return self._fetch_weather(binding, region, start, end, granularity, param)
        return self._fetch_market(binding, region, start, end, param)

    # ---------------------------------------------------------------- 内部分派
    def _fetch_weather(
        self,
        binding: SourceBinding,
        region: Sequence[str] | str | None,
        start: object,
        end: object,
        granularity: str,
        param: str | None,
    ) -> pd.DataFrame:
        column = weather_column(param)
        unit = weather_unit(column, granularity)
        codes = self._province_codes(region)
        if codes is None:
            groups: dict[str, tuple[str, ...]] = {r: () for r in WEATHER_REGION_CODES}
        else:
            groups = self.group_provinces_by_region(codes)

        frames: list[pd.DataFrame] = []
        for region_name, province_codes in sorted(groups.items()):
            sql, params = self.build_weather_sql(
                self.weather_table(region_name),
                column,
                province_codes=province_codes or None,
                start=start,
                end=end,
                granularity=granularity,
            )
            rows = self.execute(sql, params)
            if rows:
                frames.append(self._weather_frame(rows, unit))
        if not frames:
            return self.rows_to_frame([])
        out = pd.concat(frames, ignore_index=True)
        return out.sort_values(["region", "ts"], kind="stable").reset_index(drop=True)

    def _fetch_market(
        self,
        binding: SourceBinding,
        region: Sequence[str] | str | None,
        start: object,
        end: object,
        param: str | None,
    ) -> pd.DataFrame:
        if not binding.dataset:
            raise DataSourceError(f"绑定 {binding.kind}/{binding.dataset} 缺少数据集")
        grade = param or binding.default_grade
        sql, params = self.build_market_sql(
            binding.dataset,
            regions=self._market_regions(region),
            grades=None if grade in (None, "") else [grade],
            start=start,
            end=end,
        )
        return self.rows_to_frame(self.execute(sql, params))

    def _weather_frame(self, rows: Sequence[Sequence], unit: str) -> pd.DataFrame:
        """``(ts, province_code, value)`` → 长表（省份码换成 SDK 统一地区名）。"""
        records = [(ts, self._province_name(code), value, unit) for ts, code, value in rows]
        return self.rows_to_frame(records)

    def _province_name(self, code: object) -> str:
        record = self.region_index.lookup(str(code))
        if record is None:
            raise DataSourceError(f"行政区划码 {code!r} 不在 SDK 地区索引里")
        return record.name

    def _province_codes(self, region: Sequence[str] | str | None) -> tuple[str, ...] | None:
        """地区写法 → 行政区划码；``None`` 表示「不限省份」（不加省过滤）。"""
        records = self.region_index.resolve_many(region)
        return None if records is None else tuple(record.code for record in records)

    def _market_regions(self, region: Sequence[str] | str | None) -> tuple[str, ...] | None:
        """行情侧的地区写法。

        行情表的 ``region`` 混着三种口径：省名（``广东``）、全国（``CN``）、
        区域（``南方区域``）。省名走索引归一，其余原样透传——
        「华北」在气象侧是大区、在行情侧是 region 值，不能强行按一种解释处理。
        """
        queries = [region] if isinstance(region, str) else list(region or [])
        if not queries:
            return None
        names: list[str] = []
        unknown: list[str] = []
        for item in queries:
            record = self.region_index.lookup(item)
            if record is not None:
                names.append(record.name)
                continue
            text = str(item).strip()
            alias = MARKET_REGION_ALIASES.get(text.lower())
            if alias is None and text in MARKET_REGION_LABELS:
                alias = text
            if alias is None:
                unknown.append(text)
                continue
            names.append(alias)
        if unknown:
            raise UnknownRegionError(
                ", ".join(unknown),
                self.region_index.names + tuple(sorted(MARKET_REGION_LABELS)),
            )
        return tuple(dict.fromkeys(names))

    # ---------------------------------------------------------------- 元信息
    def available_regions(self) -> tuple[str, ...]:
        """库里真有站点的省份（需要连库）。"""
        codes = tuple(self.province_region_map())
        return tuple(sorted(self._province_name(code) for code in codes))

    def products(self) -> tuple[str, ...]:
        return tuple(WEATHER_PRODUCTS)


def _dsn_part(dsn: str, part: str) -> str | None:
    """从 ``mysql+pymysql://user:pass@host:port/db?charset=utf8mb4`` 中取片段。"""
    from urllib.parse import unquote, urlsplit

    try:
        parsed = urlsplit(dsn)
    except ValueError:
        return None
    if part == "host":
        return parsed.hostname
    if part == "port":
        return str(parsed.port) if parsed.port else None
    if part == "user":
        return unquote(parsed.username) if parsed.username else None
    if part == "password":
        return unquote(parsed.password) if parsed.password else None
    if part == "database":
        return parsed.path.lstrip("/") or None
    return None
