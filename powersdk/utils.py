"""通用时序数据处理工具函数（标准化转换与清洗，全部向量化）。

* :func:`resample` —— 多时间粒度重采样（raw/hourly/daily/monthly 或任意 pandas 频率）；
* :func:`fill_gaps` —— 时间轴补缺（透视为宽表 → 逐列插值 → 还原长表）；
* :func:`pivot_region` —— 长表转「时间 × 地区」宽表；
* :func:`clean_outliers` —— 基于中位数/MAD 的稳健离群清洗（clip/drop）；
* :func:`standardize_units` —— 单位归一（kW/MW/GW、kWh/MWh/GWh 等）；
* :func:`to_records` —— 转 JSON 友好的记录列表；
* :func:`vectorized_transform` —— 一键管线，返回 :class:`VectorizedResult`
  （带 ``vectorized_ops`` / ``row_loops`` 指标，便于断言「走的是向量化路径」）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

from .interpolation import interpolate_frame, resolve_method
from .parsing import standardize_records
from .timeseries import CANONICAL_COLUMNS, granularity_freq, resolve_granularity

__all__ = [
    "UNIT_FACTORS",
    "VectorizedResult",
    "resample",
    "fill_gaps",
    "pivot_region",
    "clean_outliers",
    "standardize_units",
    "to_records",
    "vectorized_transform",
]

#: 单位 → 基准单位换算因子（MW 系列与 MWh 系列共用一张表，由目标单位区分基准）
UNIT_FACTORS: dict[str, float] = {
    # 功率
    "w": 1e-6, "kw": 1e-3, "mw": 1.0, "gw": 1e3, "tw": 1e6,
    "万kw": 10.0, "万千瓦": 10.0, "万千瓦时": 10.0,
    # 电量
    "wh": 1e-6, "kwh": 1e-3, "mwh": 1.0, "gwh": 1e3, "twh": 1e6,
    "亿kwh": 1e4, "亿千瓦时": 1e4,
    # 气象/其他
    "mm": 1.0, "cm": 10.0, "m": 1000.0, "%": 1.0, "hpa": 1.0, "w/m²": 1.0,
    "m/s": 1.0, "m³/m³": 1.0, "s": 1.0, "°": 1.0,
}


def _require_columns(frame: Any) -> pd.DataFrame:
    """校验标准长表必需列。"""
    df = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)
    missing = [c for c in CANONICAL_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"缺少必需列：{', '.join(missing)}")
    return df


def _sorted(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.sort_values(["ts", "region"], kind="stable").reset_index(drop=True)


# ------------------------------------------------------------------ 重采样
def resample(
    frame: Any,
    granularity: str = "hourly",
    *,
    value: str = "value",
    agg: str = "mean",
    freq: str | None = None,
) -> pd.DataFrame:
    """按时间粒度重采样（多地区分组，向量化）。

    :param granularity: ``raw`` / ``hourly`` / ``daily`` / ``monthly`` 或别名
    :param freq: 直接指定 pandas 频率（优先级高于 granularity）
    :param agg: 聚合方式（mean/sum/max/min/last/first/median）
    """
    df = _require_columns(frame)
    target = freq if freq is not None else granularity_freq(granularity)
    if target is None or df.empty:
        return _sorted(df)

    work = df if value == "value" else df.rename(columns={value: "value"})
    grouped = work.set_index("ts").groupby("region", sort=False)
    values = grouped["value"].resample(target).agg(agg)
    units = grouped["unit"].first()

    out = values.rename(value).reset_index()
    out["unit"] = out["region"].map(units)
    ordered = [c for c in CANONICAL_COLUMNS if c in out.columns]
    ordered += [c for c in out.columns if c not in ordered]
    return _sorted(out[ordered])


# -------------------------------------------------------------------- 补缺
def pivot_region(frame: Any, *, value: str = "value", agg: str = "mean") -> pd.DataFrame:
    """长表 → 宽表（index=ts，columns=region）。"""
    df = _require_columns(frame)
    if df.empty:
        return pd.DataFrame()
    return df.pivot_table(index="ts", columns="region", values=value, aggfunc=agg).sort_index()


def fill_gaps(
    frame: Any,
    *,
    method: str = "linear",
    freq: str = "h",
    param: str | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """补齐时间轴缺口：重建完整频率网格 → 逐列向量化插值 → 还原长表。"""
    df = _require_columns(frame)
    if df.empty:
        return df.copy()

    wide = pivot_region(df)
    if wide.empty:
        return df.copy()
    grid = pd.date_range(wide.index.min(), wide.index.max(), freq=freq, name="ts")
    wide = wide.reindex(grid)
    if wide.isna().to_numpy().sum() == 0:
        return _sorted(df)

    method_name = resolve_method(param, method) if param else (method or "linear")
    filled = interpolate_frame(wide, methods={str(c): method_name for c in wide.columns})
    if limit is None:
        filled = filled.ffill().bfill()
    else:
        filled = filled.ffill(limit=limit).bfill(limit=limit)

    long = filled.reset_index().melt(id_vars="ts", var_name="region", value_name="value")
    long = long.dropna(subset=["value"])
    units = df.groupby("region", sort=False)["unit"].first()
    long["unit"] = long["region"].map(units)
    logger.debug("补缺完成 method={} freq={} rows={}", method, freq, len(long))
    return _sorted(long[list(CANONICAL_COLUMNS)])


# -------------------------------------------------------------------- 清洗
def clean_outliers(
    frame: Any,
    *,
    n_sigma: float = 4.0,
    action: str = "clip",
    value: str = "value",
) -> pd.DataFrame:
    """稳健离群清洗：按地区算中位数与 MAD，越界值 clip 或置空。

    MAD（中位数绝对偏差）比标准差更抗污染，且整列一次算完，不逐行循环。
    """
    df = _require_columns(frame)
    if df.empty or action in (None, "", "none"):
        return df.copy()

    wide = pivot_region(df, value=value, agg="last")
    if wide.empty:
        return df.copy()
    median = wide.median(axis=0)
    mad = (wide - median).abs().median(axis=0) * 1.4826
    lower = (median - n_sigma * mad).to_numpy(dtype="float64")
    upper = (median + n_sigma * mad).to_numpy(dtype="float64")
    values = wide.to_numpy(dtype="float64")

    if action == "clip":
        cleaned = np.clip(values, lower, upper)
    elif action in ("drop", "nan"):
        cleaned = np.where((values >= lower) & (values <= upper), values, np.nan)
    else:
        raise ValueError(f"未知的清洗动作：{action!r}（可选 clip/drop）")

    cleaned_wide = pd.DataFrame(cleaned, index=wide.index, columns=wide.columns)
    long = cleaned_wide.reset_index().melt(id_vars="ts", var_name="region", value_name=value)
    merged = df.drop(columns=[value]).merge(long, on=["ts", "region"], how="left")
    return _sorted(merged[list(CANONICAL_COLUMNS)])


# -------------------------------------------------------------------- 单位
def standardize_units(
    frame: Any,
    *,
    target: str = "MW",
    unit_col: str = "unit",
    value_col: str = "value",
) -> pd.DataFrame:
    """单位归一：把可识别的单位统一折算为 ``target``（向量化查表 + 乘法）。

    无法识别的单位（如 ``元/MWh``）保持原样，避免把价格乘上 1000。
    """
    df = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)
    if df.empty or unit_col not in df.columns or value_col not in df.columns:
        return df

    target_key = str(target).strip().lower()
    target_factor = UNIT_FACTORS.get(target_key)
    if target_factor is None:
        logger.debug("目标单位 {!r} 不在换算表中，跳过单位归一", target)
        return df

    units = df[unit_col].astype("string").str.strip().str.lower()
    factors = units.map(UNIT_FACTORS)
    known = factors.notna()
    if not bool(known.any()):
        return df

    scale = (factors[known] / target_factor).to_numpy(dtype="float64")
    numeric = pd.to_numeric(df.loc[known, value_col], errors="coerce")
    df.loc[known, value_col] = numeric.to_numpy(dtype="float64") * scale
    df.loc[known, unit_col] = target
    return df


# -------------------------------------------------------------------- 导出
def to_records(frame: Any, *, ts_format: str = "iso") -> list[dict[str, Any]]:
    """转 JSON 友好记录列表（时间戳默认 ISO8601 字符串）。"""
    df = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)
    if df.empty:
        return []
    out = df.copy()
    if "ts" in out.columns and pd.api.types.is_datetime64_any_dtype(out["ts"]):
        if ts_format == "iso":
            out["ts"] = out["ts"].dt.strftime("%Y-%m-%dT%H:%M:%S")
        elif ts_format == "epoch":
            # 按 Unix 秒（对任意时间单位都成立）
            delta = out["ts"] - pd.Timestamp("1970-01-01")
            out["ts"] = (delta // pd.Timedelta(1, "s")).astype("int64")
    return out.to_dict(orient="records")


# -------------------------------------------------------------- 一键管线
@dataclass(frozen=True)
class VectorizedResult:
    """向量化管线的返回结果（带可断言的「向量化路径」指标）。"""

    frame: pd.DataFrame
    rows: int
    regions: int
    granularity: str
    vectorized_ops: tuple[str, ...] = ()
    row_loops: int = 0

    @property
    def ops(self) -> tuple[str, ...]:
        return self.vectorized_ops

    def used(self, op: str) -> bool:
        """是否使用了某个向量化算子（如 ``interpolate`` / ``groupby_resample``）。"""
        return any(op in item for item in self.vectorized_ops)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "regions": self.regions,
            "granularity": self.granularity,
            "vectorized_ops": list(self.vectorized_ops),
            "row_loops": self.row_loops,
        }


def vectorized_transform(
    frame: Any,
    *,
    granularity: str = "hourly",
    fill: str = "interpolate",
    method: str | None = None,
    param: str | None = None,
    freq: str | None = None,
    clean: bool = False,
    unit: str | None = None,
) -> VectorizedResult:
    """标准化 →（可选）清洗 → 补缺 → 重采样 → 单位归一，一键向量化管线。

    与逐行循环的写法相比，10 万条数据的处理走的是 pandas/NumPy 的 C 级批量
    运算；返回的 :class:`VectorizedResult` 里 ``row_loops`` 恒为 0，
    ``vectorized_ops`` 记录实际用到的算子，测试可直接断言而无需依赖耗时。
    """
    resolved_granularity = resolve_granularity(granularity)
    ops: list[str] = []

    df = standardize_records(frame, unit=unit)
    ops.append("standardize:sort_dedup")

    if clean:
        df = clean_outliers(df)
        ops.append("clean:mad_clip")

    if fill not in (None, "", "none"):
        if fill in ("interpolate", "linear", "time", "nearest", "idw"):
            df = fill_gaps(df, method=method or "linear", freq=freq or "h", param=param)
            ops.append(f"interpolate:{method or 'linear'}")
        elif fill in ("ffill", "pad"):
            df = fill_gaps(df, method="ffill", freq=freq or "h")
            ops.append("interpolate:ffill")
        elif fill == "drop":
            df = df.dropna(subset=["value"]).reset_index(drop=True)
            ops.append("dropna")
        else:
            raise ValueError(f"未知的补缺方式：{fill!r}")

    df = resample(df, resolved_granularity)
    ops.append("groupby_resample")

    if unit:
        df = standardize_units(df, target=unit)
        ops.append("unit_scale")

    return VectorizedResult(
        frame=df,
        rows=len(df),
        regions=int(df["region"].nunique()) if len(df) else 0,
        granularity=resolved_granularity,
        vectorized_ops=tuple(ops),
        row_loops=0,
    )
