"""JSON 数据解析 + 时间戳自动补全（全流程 Pandas 向量化）。

两个入口
--------
* :func:`parse_json_records` —— 兼容多种上游返回结构：裸列表、``{"data": [...]}``、
  ``{"code":0,"result":{"rows":[...]}}``、DataFrame；并把中英文列别名
  （``时间/日期/time/datetime`` → ``ts`` 等）归一到 ``[ts, region, value, unit]``。
* :func:`complete_timestamps` —— 时间戳自动补全：
  ① 全角转半角、``时``/``点`` → ``:``；
  ② 严格 ISO8601 批量解析（不猜格式，避免把 ``01-02 06:00`` 猜成公元 1 年）；
  ③ 对解析失败的行按**上下文日期**推断缺失部分（只有 ``HH:MM`` → 补日期；
  只有 ``MM-DD`` → 补年份；只有 ``HH`` → 补日期和分秒），少数非 ISO 写法再走
  宽松 ``format="mixed"`` 兜底；
  ④ 时区统一转 Asia/Shanghai 后转 naive（东八区无夏令时，naive 即本地墙钟）；
  ⑤ 数值型时间戳按秒/毫秒自动判定。

:func:`standardize_records` 把上述步骤串起来并排序去重，是 SDK 内部管线的第一步。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from .config import DEFAULT_TZ
from .errors import PowerSDKError
from .pinyin import to_halfwidth
from .timeseries import CANONICAL_COLUMNS

__all__ = [
    "COLUMN_ALIASES",
    "DATA_KEYS",
    "parse_json_records",
    "load_json_text",
    "complete_timestamps",
    "standardize_records",
]

#: 列名别名 → 规范列名（键为小写形式，匹配时忽略大小写与首尾空白）
COLUMN_ALIASES: dict[str, str] = {
    # 时间
    "ts": "ts", "time": "ts", "datetime": "ts", "datatime": "ts", "date": "ts",
    "timestamp": "ts", "sj": "ts", "riqi": "ts", "时间": "ts", "日期": "ts",
    "时间戳": "ts", "采集时间": "ts", "统计时间": "ts", "数据时间": "ts",
    # 地区
    "region": "region", "province": "region", "prov": "region", "provincecode": "region",
    "area": "region", "dq": "region", "shengfen": "region", "省市": "region",
    "地区": "region", "省份": "region", "省": "region", "区域": "region", "地区名称": "region",
    # 数值
    "value": "value", "val": "value", "pv": "value", "power": "value",
    "load": "value", "y": "value", "shuzhi": "value", "数值": "value", "值": "value",
    "功率": "value", "负荷": "value", "电量": "value", "出力": "value",
    # 单位
    "unit": "unit", "units": "unit", "uom": "unit", "danwei": "unit",
    "dw": "unit", "单位": "unit",
}

#: JSON 中承载记录列表的常见键（按优先级）
DATA_KEYS: tuple[str, ...] = ("data", "result", "rows", "records", "list", "items", "body")


def _alias_target(column: object) -> str | None:
    """列名 → 规范列名（忽略大小写/空白）。"""
    key = str(column).strip()
    return COLUMN_ALIASES.get(key.lower()) or COLUMN_ALIASES.get(key)


def _canonical_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """列名归一化：同一规范列的多个别名源按「先到先得」合并（combine_first）。

    上游 JSON 的记录常常字段不齐（有的行用 ``时间``，有的行用 ``time``），
    ``from_records`` 会并成多列，这里把它们合并回同一列而不是互相覆盖。
    """
    sources: dict[str, list[object]] = {column: [] for column in CANONICAL_COLUMNS}
    for column in frame.columns:
        target = _alias_target(column)
        if target is not None:
            sources[target].append(column)

    out = pd.DataFrame(index=frame.index)
    for target, columns in sources.items():
        series: pd.Series | None = None
        for column in columns:
            series = frame[column] if series is None else series.combine_first(frame[column])
        out[target] = series if series is not None else pd.NA
    return out[list(CANONICAL_COLUMNS)]


def _extract_records(payload: Any) -> tuple[list[Mapping[str, Any]] | None, pd.DataFrame | None]:
    """把各种上游结构解析为记录列表；DataFrame 直接返回。"""
    if payload is None:
        return [], None
    if isinstance(payload, pd.DataFrame):
        return None, payload
    if isinstance(payload, (str, bytes, bytearray)):
        text = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PowerSDKError(f"JSON 解析失败：{exc}") from exc
    if isinstance(payload, Mapping):
        for key in DATA_KEYS:
            if key in payload:
                nested, frame = _extract_records(payload[key])
                if nested is not None or frame is not None:
                    return nested, frame
        raise PowerSDKError(f"JSON 中找不到记录列表（已尝试键：{', '.join(DATA_KEYS)}）")
    if isinstance(payload, Iterable):
        records = [item for item in payload]
        if not all(isinstance(item, Mapping) for item in records):
            raise PowerSDKError("JSON 记录必须是对象（dict）列表")
        return records, None
    raise PowerSDKError(f"不支持的 JSON 载荷类型：{type(payload).__name__}")


def parse_json_records(
    payload: Any,
    *,
    category: str | None = None,
    region: str | None = None,
    unit: str | None = None,
    context_date: object = None,
    tz: str = DEFAULT_TZ,
    complete: bool = True,
) -> pd.DataFrame:
    """解析上游 JSON 为标准长表 ``[ts, region, value, unit]``。

    :param payload: JSON 文本 / bytes / dict / list / DataFrame
    :param category: 类别名（仅用于默认单位与日志）
    :param region: 记录缺失地区列时的默认地区
    :param unit: 记录缺失单位列时的默认单位
    :param context_date: 时间戳补全的上下文日期（缺省取该列众数日期）
    :param complete: 是否执行时间戳补全 + 排序去重
    """
    records, frame = _extract_records(payload)
    if frame is not None:
        raw = _canonical_columns(frame)
    elif records:
        raw = _canonical_columns(pd.DataFrame.from_records(records))
    else:
        raw = pd.DataFrame({column: [] for column in CANONICAL_COLUMNS})

    if not complete:
        return raw
    return standardize_records(
        raw, category=category, region=region, unit=unit, context_date=context_date, tz=tz
    )


def load_json_text(text: str | bytes, **kwargs: Any) -> pd.DataFrame:
    """从 JSON 文本加载标准长表（:func:`parse_json_records` 的语法糖）。"""
    return parse_json_records(text, **kwargs)


# ------------------------------------------------------------- 时间戳补全
_TIME_ONLY = r"^\d{1,2}:\d{2}(:\d{2})?$"
_MONTH_DAY = r"^\d{1,2}[-/]\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?$"
_HOUR_ONLY = r"^\d{1,2}$"
_DATE_HOUR = r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2})$"

#: 显式时区后缀（+08:00 / Z / UTC / GMT / CST）
_TZ_SUFFIX = re.compile(r"(?:[+-]\d{2}:?\d{2}|Z|UTC|GMT|CST)\s*$", re.IGNORECASE)


def _parse_iso(raw: pd.Series, tz: str = DEFAULT_TZ) -> pd.Series:
    """严格 ISO8601 批量解析（不猜格式，因此不会把 ``01-02 06:00`` 猜成公元 1 年）。

    按「是否带显式时区后缀」拆两批：带时区的按 UTC 解析后转 ``tz``，
    不带时区的按本地墙钟（东八区）解析，最后统一成 naive。
    """
    parsed = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")
    aware = raw.str.contains(_TZ_SUFFIX, regex=True, na=False).fillna(False).to_numpy()
    if aware.any():
        part = pd.to_datetime(raw[aware], format="ISO8601", errors="coerce", utc=True)
        parsed.loc[aware] = part.dt.tz_convert(tz).dt.tz_localize(None)
    if (~aware).any():
        parsed.loc[~aware] = pd.to_datetime(raw[~aware], format="ISO8601", errors="coerce")
    return parsed


def _parse_one(value: object, tz: str) -> pd.Timestamp:
    """单值宽松解析（兜底路径）：越界/异常一律记 NaT。"""
    try:
        stamp = pd.Timestamp(pd.to_datetime(value, errors="coerce"))
    except (ValueError, TypeError, OverflowError):
        return pd.NaT
    if pd.isna(stamp):
        return pd.NaT
    if stamp.tz is not None:
        stamp = stamp.tz_convert(tz).tz_localize(None)
    if stamp < pd.Timestamp.min or stamp > pd.Timestamp.max:
        return pd.NaT
    return stamp


def _parse_loose(text: pd.Series, tz: str = DEFAULT_TZ) -> pd.Series:
    """宽松混合格式解析（仅在少数非 ISO 写法上触发，逐值兜底）。"""
    try:
        parsed = pd.to_datetime(text, format="mixed", errors="coerce")
        if isinstance(parsed.dtype, pd.DatetimeTZDtype):
            parsed = parsed.dt.tz_convert(tz).dt.tz_localize(None)
        return parsed
    except Exception:  # pragma: no cover - 极端脏数据才走到这里
        return text.map(lambda value: _parse_one(value, tz)).astype("datetime64[ns]")


def _parse_stage(raw: pd.Series, tz: str) -> pd.Series:
    """补齐后再解析：先严格 ISO，失败的行走宽松兜底。"""
    parsed = _parse_iso(raw, tz)
    still = parsed.isna()
    if bool(still.any()):
        parsed.loc[still.to_numpy()] = _parse_loose(raw[still], tz)
    return parsed


def _context_date(context_date: object, parsed: pd.Series) -> pd.Timestamp:
    """补全所用的上下文日期：显式参数 > 该列众数日期 > 2000-01-01。"""
    if context_date is not None:
        return pd.Timestamp(context_date).normalize()
    valid = parsed.dropna()
    if len(valid):
        return pd.Timestamp(valid.dt.normalize().mode().iloc[0])
    return pd.Timestamp("2000-01-01")


def _infer_missing(text: pd.Series, context: pd.Timestamp) -> pd.Series:
    """按上下文日期推断缺失的日期/年份/分秒（纯向量化 str 操作）。"""
    out = text.str.replace(_DATE_HOUR, r"\1-\2-\3 \4:00:00", regex=True)
    year = str(context.year)
    date_part = context.date().isoformat()

    mask = out.str.match(_TIME_ONLY).fillna(False).to_numpy()
    out.loc[mask] = f"{date_part} " + text.loc[mask].astype("string")

    mask = out.str.match(_MONTH_DAY).fillna(False).to_numpy()
    out.loc[mask] = f"{year}-" + text.loc[mask].astype("string").str.replace("/", "-", regex=False)

    mask = out.str.match(_HOUR_ONLY).fillna(False).to_numpy()
    out.loc[mask] = f"{date_part} " + text.loc[mask].astype("string") + ":00:00"
    return out


def _normalize_tz(parsed: pd.Series, tz: str) -> pd.Series:
    """时区统一：转 ``tz`` 后去掉时区信息（naive 视为该时区墙钟）。"""
    if isinstance(parsed.dtype, pd.DatetimeTZDtype):
        parsed = parsed.dt.tz_convert(tz).dt.tz_localize(None)
    return parsed


def complete_timestamps(
    values: Any,
    *,
    context_date: object = None,
    tz: str = DEFAULT_TZ,
) -> pd.Series:
    """时间戳列自动补全 + 时区归一（向量化，返回 naive ``datetime64[ns]``）。

    支持：``2025-01-01 05:00`` / ``2025-01-01 05`` / ``2025/1/1 5时`` /
    ``05:00``（补上下文日期）/ ``01-02 06:00``（补年份）/ ``5``（补日期分秒）/
    带时区字符串 / epoch 秒或毫秒 / 已是 datetime64 的时间列。
    """
    series = values if isinstance(values, pd.Series) else pd.Series(values)
    if series.empty:
        return pd.Series(pd.DatetimeIndex([]), dtype="datetime64[ns]", index=series.index)
    if pd.api.types.is_datetime64_any_dtype(series):
        return _normalize_tz(series.astype("datetime64[ns]"), tz)

    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    present = series.notna()
    numbers = pd.to_numeric(series, errors="coerce")

    # 1) 数值型（epoch 秒/毫秒）：按量级自动判定单位。
    #    门槛 1e8（1973 年之后）避免把「7」这种补全用的小时数当成 epoch。
    numeric = present & numbers.notna() & (numbers.abs() >= 1e8)
    if bool(numeric.any()):
        finite = numbers[numeric]
        # 逐元素判定秒/毫秒（>1e11 视为毫秒），统一折算成秒后再批量解析
        seconds = finite.to_numpy(dtype="float64")
        seconds = np.where(np.abs(seconds) > 1e11, seconds / 1000.0, seconds)
        epoch = pd.to_datetime(pd.Series(seconds, index=finite.index), unit="s", errors="coerce", utc=True)
        parsed.loc[numeric] = epoch.dt.tz_convert(tz).dt.tz_localize(None)

    # 2) 文本型：全角/中文时间词归一 → 严格 ISO 解析 → 上下文补全 → 宽松兜底
    text_mask = present & ~numeric
    if bool(text_mask.any()):
        raw = series[text_mask].astype("string").str.strip().map(to_halfwidth)
        raw = raw.str.replace("时", ":", regex=False).str.replace("点", ":", regex=False)
        raw = raw.str.replace(r":\s*$", "", regex=True).str.replace(r"\s+", " ", regex=True)

        part = _parse_iso(raw, tz)
        missing = part.isna()
        if bool(missing.any()):
            context = _context_date(context_date, part)
            repaired = _infer_missing(raw[missing], context)
            part.loc[missing] = _parse_stage(repaired, tz)
        parsed.loc[text_mask] = part
    return parsed


def _text_column(series: pd.Series | None, default: str | None) -> pd.Series:
    """地区/单位列清洗：去空白、缺失填默认值。"""
    fallback = default if default else ""
    if series is None:
        return pd.Series([fallback] * 0, dtype="str")
    text = series.astype("string").str.strip()
    return text.fillna(fallback).astype("str")


def standardize_records(
    frame: Any,
    *,
    category: str | None = None,
    region: str | None = None,
    unit: str | None = None,
    context_date: object = None,
    tz: str = DEFAULT_TZ,
    sort: bool = True,
    dedup: bool = True,
) -> pd.DataFrame:
    """标准化转换：列名归一 → 时间戳补全 → 数值化 → 排序 → 去重。"""
    source = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)
    out = _canonical_columns(source).copy()
    out["ts"] = complete_timestamps(out["ts"], context_date=context_date, tz=tz)
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out["region"] = _text_column(out.get("region"), region)
    out["unit"] = _text_column(out.get("unit"), unit)
    if sort:
        out = out.sort_values(["ts", "region"], kind="stable")
    if dedup:
        out = out.drop_duplicates(subset=["ts", "region"], keep="last")
    out = out.sort_values(["ts", "region"], kind="stable").reset_index(drop=True)
    return out[list(CANONICAL_COLUMNS)]
