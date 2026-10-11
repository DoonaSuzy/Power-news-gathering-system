"""气象数据采集：站点、变量、24 张表命名的**单一事实来源**。

数据产品（对应需求里的三类）
----------------------------
====================  ==========================================  ==================
需求里的说法          真实 Open-Meteo 产品                        Host + 路径
====================  ==========================================  ==================
历史实时              Historical Weather（Archive / ERA5 再分析） ``archive-api.open-meteo.com/v1/archive``
历史预测              Historical Forecast（归档的历史预报）        ``historical-forecast-api.open-meteo.com/v1/forecast``
每日预测              Weather Forecast（滚动预报）                ``api.open-meteo.com/v1/forecast``
====================  ==========================================  ==================

**为什么「历史实时」不叫「历史观测」**：ERA5 是再分析产品（模式 + 同化观测），
不是台站原始观测；叫「实况」会让人误以为等价于地面站读数。口径差异见
``docs/气象数据采集说明.md``。

表命名
------
``wx_{大区}_{产品}``，例如 ``wx_huabei_hist_rt``。8 大区 × 3 产品 = **24 张表**。
大区口径**复用** :mod:`newsagg.provinces`，不在此处另建一份映射。

时间类别
--------
同一张表内用 ``granularity`` 列区分 **逐时**（``hourly``）与 **逐日**（``daily``）：

- 逐时行的 ``ts`` 是整点（``2026-01-01 13:00``）；
- 逐日行的 ``ts`` 是当日零点（``2026-01-01 00:00``）。

两类行的 10 个数值列同名，但**口径不同**——逐日行的聚合方式见
:data:`DAILY_FROM_API` 与 :data:`DAILY_DERIVED`。这是刻意的：列名一致让
「同站点同时刻跨粒度对比」不必改 SQL，而口径由本模块显式声明而不是靠猜。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

__all__ = [
    "REGIONS",
    "REGION_CODES",
    "PRODUCTS",
    "ENDPOINTS",
    "HOURLY_VARS",
    "VALUE_COLUMNS",
    "VARIABLE_CN",
    "DAILY_FROM_API",
    "DAILY_DERIVED",
    "DAILY_AGG_LABEL",
    "GRANULARITIES",
    "table_name",
    "all_tables",
    "region_of_province",
    "chunk_periods",
    "estimate_call_cost",
    "WeatherPoint",
]

# --------------------------------------------------------------------------- 大区

#: 8 大区，顺序与 :mod:`newsagg.provinces` 一致（华北→港澳台）。
REGIONS: tuple[str, ...] = (
    "华北", "东北", "华东", "华中", "华南", "西南", "西北", "港澳台",
)

#: 大区 → 表名用的 ASCII 段。**必须小写且只含 [a-z0-9_]**：
#: MySQL 在部分平台把表名当大小写敏感，中文表名则会让命令行与备份工具频繁踩坑。
REGION_CODES: dict[str, str] = {
    "华北": "huabei",
    "东北": "dongbei",
    "华东": "huadong",
    "华中": "huazhong",
    "华南": "huanan",
    "西南": "xinan",
    "西北": "xibei",
    "港澳台": "gangat",
}

# --------------------------------------------------------------------------- 产品

#: 产品标识 → 中文名。
PRODUCTS: dict[str, str] = {
    "hist_rt": "历史实时（ERA5 再分析实况）",
    "hist_fc": "历史预测（归档的历史预报）",
    "daily_fc": "每日预测（滚动预报）",
}

#: 产品 → ``(接口基址, 该接口的日期参数风格)``。
#:
#: ``date_range`` 用 ``start_date``/``end_date``（适合历史回填）；
#: ``rolling`` 用 ``past_days``/``forecast_days``（适合每日滚动预报——
#: 预报接口的 ``start_date`` 只能落在 ``past_days``+``forecast_days`` 窗口内）。
ENDPOINTS: dict[str, tuple[str, str]] = {
    "hist_rt": ("https://archive-api.open-meteo.com/v1/archive", "date_range"),
    "hist_fc": (
        "https://historical-forecast-api.open-meteo.com/v1/forecast",
        "date_range",
    ),
    "daily_fc": ("https://api.open-meteo.com/v1/forecast", "rolling"),
}

#: 归档接口的发布滞后（天）。ERA5 官方口径是「每日更新、滞后 5 天」，
#: 所以「历史实时」只能覆盖到 ``今天 − 5 天``；**最近这 5 天的缺口由
#: 「历史预测」（无滞后）补**，这就是同时保留两个产品的实际理由。
ARCHIVE_LAG_DAYS = 5

# --------------------------------------------------------------------------- 变量

#: 逐时变量：``(接口参数名, 单位, 中文名)``。**恰好 10 个**——
#: Open-Meteo 的计费口径是「超过 10 个变量按倍数计」，卡在 10 可以让
#: 单请求权重保持 1.0（见 :func:`estimate_call_cost`）。
HOURLY_VARS: tuple[tuple[str, str, str], ...] = (
    ("temperature_2m", "°C", "2 米气温"),
    ("relative_humidity_2m", "%", "2 米相对湿度"),
    ("precipitation", "mm", "降水量"),
    ("wind_speed_10m", "km/h", "10 米风速"),
    ("wind_direction_10m", "°", "10 米风向"),
    ("shortwave_radiation", "W/m²", "短波辐照度"),
    ("cloud_cover", "%", "总云量"),
    ("pressure_msl", "hPa", "海平面气压"),
    ("et0_fao_evapotranspiration", "mm", "参考蒸散量 ET₀"),
    ("weather_code", "WMO", "天气代码"),
)

#: 10 个数值列名（逐时行与逐日行共用）。
VALUE_COLUMNS: tuple[str, ...] = tuple(v[0] for v in HOURLY_VARS)

#: 列名 → 中文名，用于 DDL 注释与 CLI 输出。
VARIABLE_CN: dict[str, str] = {v[0]: v[2] for v in HOURLY_VARS}

#: 列名 → 单位。
VARIABLE_UNIT: dict[str, str] = {v[0]: v[1] for v in HOURLY_VARS}

#: 逐日行里**由接口日聚合直接给出**的列：``接口 daily 参数 → 目标列``。
DAILY_FROM_API: dict[str, str] = {
    "temperature_2m_mean": "temperature_2m",
    "precipitation_sum": "precipitation",
    "wind_speed_10m_max": "wind_speed_10m",
    "wind_direction_10m_dominant": "wind_direction_10m",
    "shortwave_radiation_sum": "shortwave_radiation",
    "et0_fao_evapotranspiration": "et0_fao_evapotranspiration",
    "weather_code": "weather_code",
}

#: 逐日行里**接口没有对应日聚合、改由逐时值本地取均值**的列。
#:
#: 这三个变量在 Open-Meteo 的 ``daily`` 参数里确实不存在（不是漏写）。
#: 之所以要自己算而不是留 NULL：相对湿度与云量对光伏/负荷预测都是有效特征，
#: 静默留空会让下游拿到「有列但永远没数」的假列。
DAILY_DERIVED: tuple[str, ...] = (
    "relative_humidity_2m",
    "cloud_cover",
    "pressure_msl",
)

#: 逐日行各列的口径说明，写进 DDL 注释并由 ``docs`` 引用。
DAILY_AGG_LABEL: dict[str, str] = {
    "temperature_2m": "日平均",
    "relative_humidity_2m": "逐时均值（本地聚合）",
    "precipitation": "日累计",
    "wind_speed_10m": "日最大",
    "wind_direction_10m": "主导风向",
    "shortwave_radiation": "日累计",
    "cloud_cover": "逐时均值（本地聚合）",
    "pressure_msl": "逐时均值（本地聚合）",
    "et0_fao_evapotranspiration": "日累计",
    "weather_code": "日主导天气代码",
}

#: 允许的粒度值，与建表时的 ``ENUM`` 必须逐字一致。
GRANULARITIES: tuple[str, ...] = ("hourly", "daily")


def table_name(region: str, product: str) -> str:
    """``(大区, 产品)`` → 表名。未知取值直接报错，不做静默兜底。

    静默兜底（比如未知大区就写进 ``wx_other_*``）会让数据悄悄落到错误的表里，
    而错误的分区表在后续按表聚合时**看不出来**——这是本项目最不想要的一类 bug。
    """
    if region not in REGION_CODES:
        raise ValueError(f"未知大区 {region!r}，可选：{sorted(REGION_CODES)}")
    if product not in PRODUCTS:
        raise ValueError(f"未知产品 {product!r}，可选：{sorted(PRODUCTS)}")
    return f"wx_{REGION_CODES[region]}_{product}"


def all_tables() -> list[str]:
    """全部 24 张数据表名，顺序固定（大区 × 产品）。"""
    return [table_name(r, p) for r in REGIONS for p in PRODUCTS]


def region_of_province(province_code: str) -> str:
    """行政区划代码 → 大区。**唯一来源**是 :mod:`newsagg.provinces`。"""
    from newsagg.provinces import get as get_province  # 局部导入避免循环

    prov = get_province(str(province_code))
    if prov is None:
        raise ValueError(f"未知省级行政区划代码 {province_code!r}")
    return prov.region


# --------------------------------------------------------------------------- 切片


def chunk_periods(start, end, *, days: int = 14) -> list[tuple[Any, Any]]:
    """把 ``[start, end]`` 切成若干闭区间，每段不超过 ``days`` 天。

    为什么是 14 天：Open-Meteo 对「单请求跨度 > 2 周」按 ``周数 / 2`` 倍计费，
    2 周正好卡在 1.0 倍。切得更碎只会增加请求数，切得更长则直接翻倍。

    末段不足 ``days`` 也照常返回，不做补齐——补齐会让请求带上未来日期，
    归档接口对超出可发布范围的日期会报错。
    """
    if end < start:
        raise ValueError(f"结束日期 {end} 早于开始日期 {start}")
    periods: list[tuple[Any, Any]] = []
    cursor = start
    step = timedelta(days=days)
    while cursor <= end:
        stop = min(cursor + step - timedelta(days=1), end)
        periods.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return periods


#: 单地点单次请求的基准权重。
#:
#: **实测校准（2026-10-11）**：连续发「100 站 × 14 天 × 17 变量」的请求，
#: 第 8 个触发 HTTP 429；同样条件下「1 站」连发 12 个全部通过。
#: 若按官方说明「倍数只看变量与跨度」，100 站与 1 站权重应相同 →
#: 1 站也会在第 8 个左右被限，实测不符。**地点数是按 1:1 计入权重的**。
#: 取 ``locations`` 为乘数属偏保守（实测反推的单次权重略低于此值），
#: 宁可低估吞吐也不能把账号打进 429。
COST_UNIT_PER_LOCATION = 1.0


def estimate_call_cost(
    *,
    locations: int,
    days: int,
    variables: int,
) -> float:
    """估算一次请求折算成多少次 API 调用。

    计费口径（官方文档 + 本项目实测校准）：

    - 基准：一个地点一次请求 = 1.0 次；
    - 变量 > 10：乘 ``变量数 / 10``；
    - 跨度 > 2 周：乘 ``周数 / 2``；
    - **多地点：乘地点数**（实测校准，见 :data:`COST_UNIT_PER_LOCATION`）。

    Args:
        locations: 单请求内的站点数。
        days: 时间跨度（闭区间天数）。
        variables: 变量个数。

    Returns:
        折算调用次数（保留 3 位小数）。这是**本地估算**，不是权威账单；
        它只用来在本地限速与工期预估里保守地控制节奏。
    """
    if locations <= 0:
        raise ValueError("locations 必须为正")
    if days <= 0:
        raise ValueError("days 必须为正")
    if variables <= 0:
        raise ValueError("variables 必须为正")

    weeks = days / 7.0
    var_factor = max(1.0, variables / 10.0)
    time_factor = max(1.0, weeks / 2.0)
    return round(
        locations * COST_UNIT_PER_LOCATION * var_factor * time_factor, 3
    )


# --------------------------------------------------------------------------- 数据点

#: 产品 → 入库时的 ``source`` 标识，便于回溯是哪个接口写进来的。
PRODUCT_SOURCE: dict[str, str] = {
    "hist_rt": "open-meteo/archive",
    "hist_fc": "open-meteo/historical-forecast",
    "daily_fc": "open-meteo/forecast",
}


@dataclass(frozen=True, slots=True)
class WeatherPoint:
    """一行气象数据（逐时或逐日）。

    Attributes:
        station_id: 站点主键，形如 ``CN110101``（``CN`` + 行政区划代码）。
        adcode: 行政区划代码（整数）。
        province_code: 省级行政区划代码，如 ``110000``。
        product: 数据产品标识，见 :data:`PRODUCTS`。
        granularity: ``hourly`` 或 ``daily``。
        ts: 逐时为整点、逐日为当日零点。
        values: 10 个数值列的取值，缺测用 ``None``。
        source: 数据来源标识。
    """

    station_id: str
    adcode: int
    province_code: str
    product: str
    granularity: str
    ts: datetime
    values: dict[str, float | None] = field(default_factory=dict)
    source: str = ""

    def __post_init__(self) -> None:
        if self.product not in PRODUCTS:
            raise ValueError(f"未知产品 {self.product!r}")
        if self.granularity not in GRANULARITIES:
            raise ValueError(f"未知粒度 {self.granularity!r}，可选：{GRANULARITIES}")
        unknown = set(self.values) - set(VALUE_COLUMNS)
        if unknown:
            raise ValueError(f"含未知变量列：{sorted(unknown)}")

    @property
    def region(self) -> str:
        """所属大区（由省份推导，保证与建表口径一致）。"""
        return region_of_province(self.province_code)

    @property
    def table(self) -> str:
        """该行应写进哪张表。"""
        return table_name(self.region, self.product)

    def as_row(self) -> tuple[Any, ...]:
        """拍平成与 DDL 列顺序一致的元组，供 ``executemany`` 使用。"""
        return (
            self.station_id,
            self.adcode,
            self.province_code,
            self.granularity,
            self.ts,
            *(self.values.get(col) for col in VALUE_COLUMNS),
            self.source,
        )


def iter_regions(items: Iterable[WeatherPoint]) -> set[str]:
    """取一批数据点涉及的大区集合（用于日志与路由断言）。"""
    return {p.region for p in items}
