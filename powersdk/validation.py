"""内置数据校验与告警机制。

* :func:`validate` 对标准长表做**全向量化**检查：必填列、空值、取值范围、重复
  ``(ts, region)``、时间倒退、缺失率、单位一致性、时间轴缺口；
  返回 :class:`ValidationReport`（**准确率 = 1 - 问题行数 / 总行数**，按行去重
  统计，不做「 violations 计数相加」的高估）。
* 告警通过 :class:`AlertSink` 协议外发，SDK 自带 :class:`CollectingAlertSink`
  （测试断言用）与 :class:`LoguruAlertSink`（生产日志用），业务侧也可注入自己的
  Webhook / 短信实现。
* 干净的种子数据准确率为 1.0（≥ 0.998），脏数据会显著掉分并触发告警。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import pandas as pd
from loguru import logger

from .timeseries import (
    CATEGORY_REGISTRY,
    WEATHER_PARAMS,
    Category,
    CategorySpec,
    WeatherParamSpec,
    resolve_category,
)

__all__ = [
    "ValidationSpec",
    "Violation",
    "ValidationReport",
    "Alert",
    "AlertSink",
    "CollectingAlertSink",
    "LoguruAlertSink",
    "validate",
    "validate_and_alert",
    "alerts_from_report",
    "spec_for_category",
    "DEFAULT_THRESHOLD",
]

#: 默认准确率阈值（简历口径：99.8%）
DEFAULT_THRESHOLD: float = 0.998


@dataclass(frozen=True)
class ValidationSpec:
    """校验规则（可由类别画像自动派生，也可手写）。"""

    value_min: float | None = None
    value_max: float | None = None
    unit: str | None = None
    allow_null: bool = False
    max_null_ratio: float = 0.0
    max_duplicate_ratio: float = 0.0
    check_monotonic: bool = True
    expected_freq: str | None = None
    max_gap_ratio: float = 0.05
    name: str = "default"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value_min": self.value_min,
            "value_max": self.value_max,
            "unit": self.unit,
            "allow_null": self.allow_null,
            "expected_freq": self.expected_freq,
        }


@dataclass(frozen=True)
class Violation:
    """一类违规问题。"""

    code: str
    kind: str  # "error" | "warning"
    count: int
    ratio: float
    message: str
    examples: tuple[Any, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "kind": self.kind,
            "count": self.count,
            "ratio": round(self.ratio, 6),
            "message": self.message,
            "examples": list(self.examples),
        }


@dataclass(frozen=True)
class ValidationReport:
    """校验结果：准确率 + 违规明细。"""

    spec: ValidationSpec
    total_rows: int
    valid_rows: int
    violations: tuple[Violation, ...] = ()

    @property
    def accuracy(self) -> float:
        """准确率（无数据时定义为 1.0，不制造假告警）。"""
        if self.total_rows <= 0:
            return 1.0
        return self.valid_rows / self.total_rows

    @property
    def errors(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.kind == "error")

    @property
    def warnings(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.kind != "error")

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(v.code for v in self.violations)

    def passed(self, threshold: float = DEFAULT_THRESHOLD) -> bool:
        return self.accuracy >= threshold

    def as_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.describe(),
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
            "accuracy": round(self.accuracy, 6),
            "violations": [v.as_dict() for v in self.violations],
        }


def _samples(values: Iterable[Any], limit: int = 5) -> tuple[Any, ...]:
    """违规样例（最多 limit 个，便于日志/告警里直接看到脏数据长相）。"""
    out: list[Any] = []
    for value in values:
        if len(out) >= limit:
            break
        if isinstance(value, float):
            out.append(round(value, 4))
        else:
            out.append(str(value))
    return tuple(out)


def validate(frame: Any, spec: ValidationSpec) -> ValidationReport:
    """对标准长表做校验，返回 :class:`ValidationReport`。

    所有检查均为向量化布尔运算；准确率按「行」去重统计（一行同时触发多个问题
    只扣一次），因此 :attr:`ValidationReport.accuracy` 与业务口径一致。
    """
    df = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)
    total = len(df)
    if total == 0:
        return ValidationReport(spec, 0, 0, ())

    missing_cols = [c for c in ("ts", "value") if c not in df.columns]
    if missing_cols:
        return ValidationReport(
            spec,
            total,
            0,
            (
                Violation(
                    "missing_columns", "error", total, 1.0,
                    f"缺少必需列：{', '.join(missing_cols)}", (),
                ),
            ),
        )

    bad = pd.Series(False, index=df.index)
    violations: list[Violation] = []

    # 1) 时间戳缺失
    null_ts = df["ts"].isna()
    # 2) 数值缺失 / 非数值
    value = pd.to_numeric(df["value"], errors="coerce")
    null_value = value.isna()
    if not spec.allow_null and (bool(null_ts.any()) or bool(null_value.any())):
        count = int((null_ts | null_value).sum())
        ratio = count / total
        bad |= null_ts | null_value
        violations.append(
            Violation("null_value", "error", count, ratio, f"{count} 行存在空值/非数值", _samples(df.index[null_value].tolist()))
        )
    if spec.max_null_ratio and float(null_value.mean()) > spec.max_null_ratio:
        violations.append(
            Violation("null_ratio_exceeded", "error", int(null_value.sum()), float(null_value.mean()),
                      f"空值率 {null_value.mean():.4f} 超过上限 {spec.max_null_ratio}", ())
        )

    # 3) 取值范围
    if spec.value_min is not None or spec.value_max is not None:
        low = value < spec.value_min if spec.value_min is not None else pd.Series(False, index=df.index)
        high = value > spec.value_max if spec.value_max is not None else pd.Series(False, index=df.index)
        out_of_range = (low | high).fillna(False)
        if bool(out_of_range.any()):
            count = int(out_of_range.sum())
            bad |= out_of_range
            violations.append(
                Violation(
                    "out_of_range", "error", count, count / total,
                    f"{count} 行数值越界（合法区间 [{spec.value_min}, {spec.value_max}]）",
                    _samples(value[out_of_range].tolist()),
                )
            )

    # 4) 重复 (ts, region)
    if "region" in df.columns:
        duplicated = df.duplicated(subset=["ts", "region"], keep="first").fillna(False)
        duplicate_count = int(duplicated.sum())
        if duplicate_count:
            bad |= duplicated
            violations.append(
                Violation("duplicate_key", "error", duplicate_count, duplicate_count / total,
                          f"{duplicate_count} 行 (ts, region) 重复", ())
            )
        if spec.max_duplicate_ratio and duplicate_count / total > spec.max_duplicate_ratio:
            violations.append(
                Violation("duplicate_ratio_exceeded", "error", duplicate_count, duplicate_count / total,
                          f"重复率 {duplicate_count / total:.4f} 超过上限 {spec.max_duplicate_ratio}", ())
            )

        # 5) 时间倒退（按地区分组判断单调性）
        if spec.check_monotonic and total > 1:
            ordered = df.sort_values(["region", "ts"], kind="stable")
            regress = ordered.groupby("region", sort=False)["ts"].diff() < pd.Timedelta(0)
            regress = regress.fillna(False)
            if bool(regress.any()):
                count = int(regress.sum())
                bad |= regress.reindex(df.index, fill_value=False)
                violations.append(
                    Violation("non_monotonic", "error", count, count / total, f"{count} 行时间戳倒退", ())
                )

    # 6) 单位一致性（warning，不扣准确率）
    if spec.unit and "unit" in df.columns:
        mismatch = df["unit"].astype("string").str.strip().ne(spec.unit).fillna(False)
        if bool(mismatch.any()):
            count = int(mismatch.sum())
            violations.append(
                Violation("unit_mismatch", "warning", count, count / total,
                          f"{count} 行单位不是 {spec.unit}", _samples(df["unit"][mismatch].tolist()))
            )

    # 7) 时间轴缺口（warning）
    if spec.expected_freq and "region" in df.columns and total > 1:
        wide = df.pivot_table(index="ts", columns="region", values="value", aggfunc="count")
        grid = wide.reindex(pd.date_range(df["ts"].min(), df["ts"].max(), freq=spec.expected_freq))
        gap_ratio = float(grid.isna().to_numpy().mean()) if grid.size else 0.0
        if gap_ratio > spec.max_gap_ratio:
            violations.append(
                Violation("time_gap", "warning", int(grid.isna().to_numpy().sum()), gap_ratio,
                          f"时间轴缺口率 {gap_ratio:.4f} 超过上限 {spec.max_gap_ratio}", ())
            )

    valid_rows = total - int(bad.sum())
    return ValidationReport(spec, total, valid_rows, tuple(violations))


# -------------------------------------------------------------------- 告警
@dataclass(frozen=True)
class Alert:
    """一条告警（由校验违规转换而来，也可由业务直接构造）。"""

    code: str
    level: str  # "info" | "warning" | "error" | "critical"
    message: str
    rows: int = 0
    ratio: float = 0.0
    context: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "level": self.level,
            "message": self.message,
            "rows": self.rows,
            "ratio": round(self.ratio, 6),
            "context": dict(self.context),
        }


@runtime_checkable
class AlertSink(Protocol):
    """告警出口协议：任何实现 ``emit(alert)`` 的对象都可接入。"""

    def emit(self, alert: Alert) -> None:  # pragma: no cover - 协议定义
        ...


class CollectingAlertSink:
    """把告警收集到内存列表（测试 / 单机调试用）。"""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def emit(self, alert: Alert) -> None:
        self.alerts.append(alert)

    def by_level(self, level: str) -> tuple[Alert, ...]:
        return tuple(a for a in self.alerts if a.level == level)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(a.code for a in self.alerts)

    def clear(self) -> None:
        self.alerts.clear()

    def __len__(self) -> int:
        return len(self.alerts)

    def __iter__(self):
        return iter(self.alerts)


class LoguruAlertSink:
    """把告警写进 loguru（生产默认出口，可按级别路由到不同 sink）。"""

    _LEVELS = {"debug": "DEBUG", "info": "INFO", "warning": "WARNING", "error": "ERROR", "critical": "CRITICAL"}

    def __init__(self, channel: str = "powersdk.alert") -> None:
        self.channel = channel
        self._log = logger.bind(channel=channel)

    def emit(self, alert: Alert) -> None:
        level = self._LEVELS.get(str(alert.level).lower(), "WARNING")
        self._log.log(level, "[{}] {}（rows={}, ratio={:.4f}）", alert.code, alert.message, alert.rows, alert.ratio)


def alerts_from_report(report: ValidationReport, *, threshold: float = DEFAULT_THRESHOLD) -> list[Alert]:
    """把校验报告转换为告警列表（每条违规一条 + 准确率不足一条汇总）。"""
    alerts: list[Alert] = [
        Alert(
            code=violation.code,
            level="critical" if violation.kind == "error" and violation.ratio > 0.05 else violation.kind,
            message=violation.message,
            rows=violation.count,
            ratio=violation.ratio,
            context={"spec": report.spec.name, "examples": list(violation.examples)},
        )
        for violation in report.violations
    ]
    if not report.passed(threshold):
        alerts.append(
            Alert(
                code="accuracy_below_threshold",
                level="critical",
                message=f"数据准确率 {report.accuracy:.4f} 低于阈值 {threshold:.4f}",
                rows=report.total_rows - report.valid_rows,
                ratio=1.0 - report.accuracy,
                context={"threshold": threshold, "total_rows": report.total_rows},
            )
        )
    return alerts


def validate_and_alert(
    frame: Any,
    spec: ValidationSpec,
    sink: AlertSink | None = None,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    strict: bool = False,
) -> ValidationReport:
    """校验 + 发告警；``strict=True`` 时准确率不足直接抛
    :class:`~powersdk.errors.ValidationError`。"""
    report = validate(frame, spec)
    alerts = alerts_from_report(report, threshold=threshold)
    if alerts:
        if sink is None:
            sink = LoguruAlertSink()
        for alert in alerts:
            sink.emit(alert)
    if strict and not report.passed(threshold):
        from .errors import ValidationError

        raise ValidationError(
            f"{spec.name} 数据校验未达标",
            accuracy=report.accuracy,
            threshold=threshold,
        )
    return report


def spec_for_category(
    category: Category | str,
    param: str | None = None,
    *,
    expected_freq: str | None = None,
    allow_null: bool = False,
) -> ValidationSpec:
    """按类别（或气象参数）画像自动派生校验规则。"""
    resolved = resolve_category(category)
    if resolved == Category.WEATHER:
        name = param or "temperature_2m"
        profile: CategorySpec | WeatherParamSpec = WEATHER_PARAMS.get(
            name, WEATHER_PARAMS["temperature_2m"]
        )
        return ValidationSpec(
            value_min=profile.minimum,
            value_max=profile.maximum,
            unit=profile.unit,
            allow_null=allow_null,
            expected_freq=expected_freq,
            name=f"weather:{name}",
        )
    spec = CATEGORY_REGISTRY[resolved.value]
    return ValidationSpec(
        value_min=spec.minimum,
        value_max=spec.maximum,
        unit=spec.unit,
        allow_null=allow_null,
        expected_freq=expected_freq,
        name=spec.value,
    )
