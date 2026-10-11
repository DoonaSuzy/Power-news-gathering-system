"""11 类时序数据的类别注册表与数据源抽象。

模块内容
--------
* :class:`Category` —— 11 类时序数据枚举（天气、电价、负荷、新能源出力、风电、
  光伏、水电、煤价、用电量、电力缺口、预测）；
* :data:`CATEGORY_REGISTRY` —— 每类的单位、量级、取值范围、日周期形状、默认
  插值参数、中文别名（**配置化**，新增类别只改这张表）；
* :data:`WEATHER_PARAMS` —— 18 个气象参数的取值画像；
* :class:`TimeSeriesSource` —— 数据源协议（Protocol）；
  :class:`InMemoryTimeSeriesSource` 为**确定性**内存源（固定随机种子，保证测试
  可复现），:class:`MySQLTimeSeriesSource` 为 MySQL 源（离线安全：仅在真正取数
  时才导入 PyMySQL）。
"""

from __future__ import annotations

import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from .errors import DataSourceError, UnknownCategoryError, UnknownParamError
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
    """MySQL 时序数据源（依赖注入 DSN，离线安全）。

    * **不在模块导入时** import PyMySQL，只有真正 :meth:`connect` 时才导入；
      未安装 PyMySQL 或连不上库时抛出 :class:`~powersdk.errors.DataSourceError`，
      消息里说明原因，方便在 CI / 无网环境直接换用 :class:`InMemoryTimeSeriesSource`。
    * :meth:`build_select_sql` 与 :meth:`rows_to_frame` 是纯函数，可单测：
      SQL 拼装、游标行 → DataFrame 的转换都不需要真实数据库。
    """

    def __init__(
        self,
        settings: object | None = None,
        *,
        dsn: str | None = None,
        table_prefix: str = "power_timeseries",
        region_index: RegionIndex | None = None,
        connection: object | None = None,
    ) -> None:
        self.settings = settings
        self.dsn = dsn
        self.table_prefix = table_prefix
        self.region_index = region_index or default_region_index()
        #: 已建立的连接（可注入 fake，便于离线单测 execute/fetch）
        self._connection = connection

    def table_name(self, category: Category | str) -> str:
        """类别 → 物理表名（如 ``power_timeseries_load``）。"""
        return f"{self.table_prefix}_{resolve_category(category).value}"

    def build_select_sql(
        self,
        category: Category | str,
        region: Sequence[str] | str | None = None,
        start: object = None,
        end: object = None,
    ) -> tuple[str, tuple]:
        """构造参数化查询 SQL 与参数元组（纯函数，便于单测）。"""
        table = self.table_name(category)
        where: list[str] = []
        params: list[object] = []
        names = self._region_names(region)
        if names:
            placeholders = ", ".join(["%s"] * len(names))
            where.append(f"region IN ({placeholders})")
            params.extend(names)
        if start is not None:
            where.append("ts >= %s")
            params.append(_to_start(start))
        if end is not None:
            where.append("ts <= %s")
            params.append(_to_end(end))
        sql = f"SELECT ts, region, value, unit FROM {table}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sql + " ORDER BY region, ts", tuple(params)

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
        """执行查询并返回原始行（无连接时自动建连）。"""
        connection = self._connection or self.connect()
        with connection.cursor() as cursor:
            cursor.execute(sql, tuple(params))
            return list(cursor.fetchall())

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
        """从 MySQL 取数（离线环境会抛出 :class:`DataSourceError`）。"""
        cat = resolve_category(category)
        if cat != Category.WEATHER and param is not None:
            raise UnknownCategoryError(param, categories())
        sql, params = self.build_select_sql(cat, region=region, start=start, end=end)
        return self.rows_to_frame(self.execute(sql, params))

    def _region_names(self, region: Sequence[str] | str | None) -> tuple[str, ...] | None:
        if region is None:
            return None
        queries = [region] if isinstance(region, str) else list(region)
        if not queries:
            return None
        return tuple(self.region_index.resolve(item).name for item in queries)


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
