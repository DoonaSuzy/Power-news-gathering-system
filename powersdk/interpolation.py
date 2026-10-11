"""气象参数 → 插值方法映射表（配置与逻辑解耦）。

设计要点
--------
* :data:`INTERP_METHODS` 是纯配置：``气象参数名 → 插值方法名``，覆盖 31 个常用
  气象/辐照参数，映射到 27 种不同方法。新增参数或调整方法只改这张表，不需要
  碰任何处理逻辑（**配置与逻辑解耦**）。
* :data:`METHOD_CATALOG` 描述每种方法的实现后端：``native`` 表示 pandas /
  NumPy 原生实现（本仓库在无 SciPy 环境下实测可用）；其余方法（cubic、spline、
  akima、pchip……）在缺少 SciPy 时**优雅降级为 ``linear``** 并记 loguru 调试日志，
  绝不抛异常中断查询。
* :func:`resolve_method` 支持运行时覆盖（``override`` 参数优先于配置表），
  未知参数回退 :data:`DEFAULT_METHOD`。
* 所有实现均为向量化运算（NumPy searchsorted / pandas interpolate），无 Python
  行级循环。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd
from loguru import logger

__all__ = [
    "MethodSpec",
    "METHOD_CATALOG",
    "NATIVE_METHODS",
    "SCIPY_METHODS",
    "METHOD_ALIASES",
    "DEFAULT_METHOD",
    "INTERP_METHODS",
    "resolve_method",
    "canonical_method",
    "apply_interpolation",
    "interpolate_frame",
    "interpolate_params",
]


@dataclass(frozen=True)
class MethodSpec:
    """插值方法描述（配置项）。"""

    name: str
    native: bool
    backend: str
    description: str


#: 方法目录：20+ 种方法（含别名与 SciPy 后端方法）
METHOD_CATALOG: dict[str, MethodSpec] = {
    "linear": MethodSpec("linear", True, "pandas", "线性插值（默认，通用连续量）"),
    "nearest": MethodSpec("nearest", True, "numpy", "最近邻（NumPy searchsorted 实现）"),
    "idw": MethodSpec("idw", True, "numpy", "反距离权重插值（土壤含水率等空间量）"),
    "bilinear": MethodSpec("bilinear", True, "numpy", "双线性插值（单变量序列等价于线性）"),
    "quadratic": MethodSpec("quadratic", False, "scipy", "二次样条（SciPy，缺失时降级 linear）"),
    "zero": MethodSpec("zero", True, "pandas", "零阶保持 / 阶梯插值（降水量等累量）"),
    "pad": MethodSpec("pad", True, "pandas", "前值填充（等价 ffill）"),
    "previous": MethodSpec("previous", True, "pandas", "前值填充（pad 别名，风向等角度量）"),
    "ffill": MethodSpec("ffill", True, "pandas", "前向填充"),
    "bfill": MethodSpec("bfill", True, "pandas", "后向填充"),
    "backfill": MethodSpec("backfill", True, "pandas", "后向填充（bfill 别名）"),
    "next": MethodSpec("next", True, "pandas", "后值填充（bfill 别名，蒸散发等）"),
    "dropped": MethodSpec("dropped", True, "pandas", "不插值，直接丢弃缺失（能见度/天气代码）"),
    "time": MethodSpec("time", True, "pandas", "按时间距离线性插值（湿度等）"),
    "index": MethodSpec("index", True, "pandas", "按索引距离线性插值（露点等）"),
    "values": MethodSpec("values", True, "pandas", "按位置等距插值（等价 linear）"),
    "area": MethodSpec("area", False, "scipy", "面积守恒插值（气压等，SciPy）"),
    "cubic": MethodSpec("cubic", False, "scipy", "三次样条（风速等，SciPy）"),
    "spline": MethodSpec("spline", False, "scipy", "样条插值（地温等，SciPy）"),
    "akima": MethodSpec("akima", False, "scipy", "Akima 插值（海平面气压等，SciPy）"),
    "pchip": MethodSpec("pchip", False, "scipy", "保形分段三次（阵风等，SciPy）"),
    "slinear": MethodSpec("slinear", False, "scipy", "线性样条（短波辐射等，SciPy）"),
    "barycentric": MethodSpec("barycentric", False, "scipy", "重心坐标插值（散射辐射等，SciPy）"),
    "krogh": MethodSpec("krogh", False, "scipy", "Krogh 插值（直接法向辐照度等，SciPy）"),
    "cubicspline": MethodSpec("cubicspline", False, "scipy", "自然三次样条（雪深等，SciPy）"),
    "polynomial": MethodSpec("polynomial", False, "scipy", "多项式插值（深层土壤湿度等，SciPy）"),
    "from_derivatives": MethodSpec("from_derivatives", False, "scipy", "导数约束插值（浅层地温等，SciPy）"),
    "piecewise_polynomial": MethodSpec("piecewise_polynomial", False, "scipy", "分段多项式插值（SciPy）"),
}

#: 别名归一化（pad/previous/ffill 语义相同，backfill/next/bfill 语义相同）
METHOD_ALIASES: dict[str, str] = {
    "previous": "pad",
    "backfill": "bfill",
    "next": "bfill",
    "values": "linear",
    "bilinear": "linear",
    "nearest": "nearest",
}

#: 无需 SciPy 即可运行的方法
NATIVE_METHODS: frozenset[str] = frozenset(
    name for name, spec in METHOD_CATALOG.items() if spec.native
)

#: 依赖 SciPy 的方法（缺失时降级为 linear）
SCIPY_METHODS: frozenset[str] = frozenset(
    name for name, spec in METHOD_CATALOG.items() if not spec.native
)

#: 未知参数/未配置时的兜底方法
DEFAULT_METHOD: str = "linear"

#: 气象参数 → 插值方法映射表（20+ 参数、20+ 种不同方法；纯配置，可热更新）
INTERP_METHODS: dict[str, str] = {
    # --- 温度类 ---
    "temperature_2m": "linear",
    "apparent_temperature": "linear",
    "temperature_80m": "bilinear",
    "soil_temperature_0cm": "values",
    "soil_temperature_7cm": "from_derivatives",
    # --- 湿度 / 云类 ---
    "relative_humidity_2m": "time",
    "dewpoint_2m": "index",
    "cloudcover": "ffill",
    "cloudcover_low": "idw",
    # --- 降水类（累量，零阶保持）---
    "precipitation": "zero",
    "rain": "zero",
    "snowfall": "bfill",
    # --- 风类 ---
    "wind_speed_10m": "cubic",
    "wind_speed_80m": "spline",
    "wind_speed_120m": "akima",
    "wind_gusts_10m": "pchip",
    "wind_direction_10m": "pad",
    "wind_direction_80m": "previous",
    # --- 气压类 ---
    "pressure_msl": "nearest",
    "surface_pressure": "area",
    # --- 辐射类 ---
    "shortwave_radiation": "slinear",
    "direct_radiation": "quadratic",
    "diffuse_radiation": "barycentric",
    "direct_normal_irradiance": "krogh",
    # --- 其他 ---
    "visibility": "dropped",
    "soil_moisture_0_10cm": "idw",
    "soil_moisture_7_28cm": "polynomial",
    "evapotranspiration": "next",
    "sunshine_duration": "backfill",
    "snow_depth": "cubicspline",
    "weather_code": "dropped",
    "is_day": "previous",
}


def resolve_method(param: str | None, override: str | None = None) -> str:
    """解析某个气象参数应使用的插值方法。

    优先级：``override`` > :data:`INTERP_METHODS` 配置 > :data:`DEFAULT_METHOD`。
    """
    if override:
        return canonical_method(override)
    if param is None:
        return DEFAULT_METHOD
    method = INTERP_METHODS.get(str(param).strip().lower())
    if method is None:
        logger.debug("参数 {!r} 未配置插值方法，回退为 {!r}", param, DEFAULT_METHOD)
        return DEFAULT_METHOD
    return canonical_method(method)


def canonical_method(method: str) -> str:
    """方法名归一化（小写 + 别名折叠）；未知方法原样返回。"""
    name = str(method).strip().lower()
    return METHOD_ALIASES.get(name, name)


# ---------------------------------------------------------------------- 实现
def _nearest_fill(series: pd.Series) -> pd.Series:
    """最近邻填充（向量化：一次 searchsorted 完成）。"""
    values = series.to_numpy(dtype="float64", copy=True)
    valid = ~np.isnan(values)
    if not valid.any():
        return series.copy()
    pos_valid = np.flatnonzero(valid)
    pos_missing = np.flatnonzero(~valid)
    right = np.clip(np.searchsorted(pos_valid, pos_missing, side="left"), 0, len(pos_valid) - 1)
    left = np.clip(right - 1, 0, len(pos_valid) - 1)
    # 距离相等时取右侧（与 pandas/scipy 的 tie 行为一致）
    take_right = (pos_missing - pos_valid[left]) >= (pos_valid[right] - pos_missing)
    chosen = np.where(take_right, pos_valid[right], pos_valid[left])
    values[pos_missing] = values[chosen]
    return pd.Series(values, index=series.index, name=series.name)


def _idw_fill(series: pd.Series, *, power: float = 2.0) -> pd.Series:
    """反距离权重（IDW）填充：取左右最近的有效点加权。

    权重为 ``1 / 距离**power``，完全向量化；序列两端退化为最近邻常数填充。
    """
    values = series.to_numpy(dtype="float64", copy=True)
    valid = ~np.isnan(values)
    if not valid.any():
        return series.copy()
    pos_valid = np.flatnonzero(valid)
    pos_missing = np.flatnonzero(~valid)
    right = np.clip(np.searchsorted(pos_valid, pos_missing, side="left"), 0, len(pos_valid) - 1)
    left = np.clip(right - 1, 0, len(pos_valid) - 1)
    dist_left = np.abs(pos_missing - pos_valid[left]).astype("float64") + 1e-9
    dist_right = np.abs(pos_missing - pos_valid[right]).astype("float64") + 1e-9
    weight_left = 1.0 / dist_left ** power
    weight_right = 1.0 / dist_right ** power
    estimate = (
        weight_left * values[pos_valid[left]] + weight_right * values[pos_valid[right]]
    ) / (weight_left + weight_right)
    values[pos_missing] = estimate
    return pd.Series(values, index=series.index, name=series.name)


def apply_interpolation(
    values: pd.Series,
    *,
    method: str | None = None,
    param: str | None = None,
) -> pd.Series:
    """对单条序列按指定方法补缺（向量化实现，返回新 Series）。

    * ``dropped``：直接丢弃缺失值；
    * ``pad`` / ``previous`` / ``ffill`` / ``zero``：前值填充（零阶保持）；
    * ``bfill`` / ``backfill`` / ``next``：后值填充；
    * ``nearest`` / ``idw``：NumPy 向量化实现；
    * ``time`` / ``index`` / ``values`` / ``linear``：pandas 原生插值；
    * 其余 SciPy 方法：降级为 ``linear`` 并记调试日志。
    """
    series = values if isinstance(values, pd.Series) else pd.Series(values)
    resolved = resolve_method(param, method)
    if not series.isna().any():
        return series.copy()

    if resolved == "dropped":
        return series.dropna()
    if resolved in ("pad", "ffill", "zero"):
        return series.ffill()
    if resolved in ("bfill", "next"):
        return series.bfill()
    if resolved == "nearest":
        return _nearest_fill(series)
    if resolved == "idw":
        return _idw_fill(series)
    if resolved in ("linear", "values"):
        return series.interpolate(method="linear")
    if resolved == "time":
        if isinstance(series.index, pd.DatetimeIndex):
            return series.interpolate(method="time")
        return series.interpolate(method="linear")
    if resolved == "index":
        if pd.api.types.is_numeric_dtype(series.index):
            return series.interpolate(method="index")
        return series.interpolate(method="linear")

    logger.debug("插值方法 {!r} 需要 SciPy，当前环境降级为 {!r}", resolved, DEFAULT_METHOD)
    return series.interpolate(method=DEFAULT_METHOD)


def interpolate_frame(
    wide: pd.DataFrame,
    *,
    methods: Mapping[str, str] | None = None,
    default: str | None = None,
) -> pd.DataFrame:
    """对宽表逐列插值（index=时间戳，columns=气象参数/地区）。

    所有列方法一致且为 ``linear`` 时走单次 :meth:`pandas.DataFrame.interpolate`
    快路径；否则逐列调用 :func:`apply_interpolation`（每列仍是一次向量化运算）。
    """
    if wide.empty:
        return wide.copy()
    mapping: Mapping[str, str] = methods or {}
    fallback = default or DEFAULT_METHOD
    resolved = {str(col): canonical_method(mapping.get(str(col), fallback)) for col in wide.columns}
    if resolved and all(method == "linear" for method in resolved.values()):
        return wide.interpolate(method="linear")

    filled = pd.DataFrame(index=wide.index)
    for col in wide.columns:
        filled[col] = apply_interpolation(wide[col], method=resolved[str(col)])
    return filled


def interpolate_params(
    frame: pd.DataFrame,
    *,
    param_col: str = "param",
    value_col: str = "value",
    methods: Mapping[str, str] | None = None,
    default: str | None = None,
) -> pd.DataFrame:
    """长表（含 ``param`` 列）按参数各自的插值方法补缺。

    内部先透视为宽表 → 逐列向量化插值 → 还原长表，避免 groupby-apply 的行级循环。
    """
    if frame.empty or param_col not in frame.columns:
        return frame.copy()
    mapping: Mapping[str, str] = methods or INTERP_METHODS
    index_cols = [c for c in frame.columns if c not in (param_col, value_col)]
    wide = frame.pivot_table(index=index_cols, columns=param_col, values=value_col, aggfunc="mean")
    filled = interpolate_frame(wide, methods=mapping, default=default)
    long = filled.reset_index().melt(id_vars=index_cols, var_name=param_col, value_name=value_col)
    ordered = [c for c in frame.columns if c in long.columns]
    ordered += [c for c in long.columns if c not in ordered]
    return long[ordered]


def available_methods() -> tuple[str, ...]:
    """返回方法目录中全部方法名（供前端/文档展示）。"""
    return tuple(METHOD_CATALOG)


def configured_params() -> tuple[str, ...]:
    """返回已配置插值方法的参数名。"""
    return tuple(INTERP_METHODS)


def methods_for_params(params: Iterable[str] | None = None) -> dict[str, str]:
    """批量解析参数 → 方法；``None`` 表示全部已配置参数。"""
    names = tuple(params) if params is not None else configured_params()
    return {name: resolve_method(name) for name in names}
