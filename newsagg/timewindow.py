"""采集时间窗口（改造方案 §3.3「差异化采集策略」的基础）。

两类任务：

- ``JOB_FULL_RESCAN``：全量回看，窗口 = 今天 00:00 往前数 ``window_days`` 个自然日。
- ``JOB_INCREMENTAL``：增量，窗口 = 上次发现成功时间往前推
  ``incremental_lookback_hours`` 小时（默认 48h），保证边界文章不漏。

窗口内的时间一律为**北京时间 naive**，与 ``cleaning.parse_published`` 输出一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from newsagg.cleaning import now_cst, to_cst_naive, to_utc_naive

__all__ = [
    "JOB_FULL_RESCAN",
    "JOB_INCREMENTAL",
    "Window",
    "build_window",
    "window_key",
]

JOB_FULL_RESCAN = "full_rescan"
JOB_INCREMENTAL = "incremental"


@dataclass(frozen=True, slots=True)
class Window:
    """一次任务的时间窗口（北京时间 naive）。"""

    start_cst: datetime
    end_cst: datetime
    job_type: str = JOB_FULL_RESCAN

    def contains(self, when_cst: datetime | None) -> bool:
        """时间是否落在窗口内；``None`` 视为不在窗口内（未知时间不参与判定）。"""
        if when_cst is None:
            return False
        return self.start_cst <= when_cst <= self.end_cst

    def is_above(self, when_cst: datetime | None) -> bool:
        """是否晚于窗口上界（倒序列表可据此提前停止）。"""
        return when_cst is not None and when_cst > self.end_cst

    @property
    def start_utc(self) -> datetime:
        return to_utc_naive(self.start_cst)

    @property
    def end_utc(self) -> datetime:
        return to_utc_naive(self.end_cst)

    @property
    def days(self) -> int:
        return max(1, (self.end_cst - self.start_cst).days + 1)

    def describe(self) -> str:
        return (
            f"[{self.job_type}] {self.start_cst:%Y-%m-%d %H:%M} ~ "
            f"{self.end_cst:%Y-%m-%d %H:%M} (CST)"
        )


def build_window(
    *,
    job_type: str = JOB_FULL_RESCAN,
    now_utc_naive: datetime | None = None,
    last_discovery_success_at_utc: datetime | None = None,
    window_days: int = 7,
    incremental_lookback_hours: int = 48,
) -> Window:
    """构造时间窗口。

    :param job_type: ``full_rescan`` 或 ``incremental``
    :param now_utc_naive: 建批时刻（UTC naive），默认取当前时间
    :param last_discovery_success_at_utc: 该来源上次列表发现成功时间（UTC naive）
    :param window_days: 全量任务回看天数
    :param incremental_lookback_hours: 增量任务回看小时数
    """
    now_utc = now_utc_naive or to_utc_naive(now_cst())
    now_cst_value = to_cst_naive(now_utc)

    if job_type == JOB_INCREMENTAL and last_discovery_success_at_utc is not None:
        start = to_cst_naive(last_discovery_success_at_utc) - timedelta(
            hours=incremental_lookback_hours
        )
        return Window(start, now_cst_value, JOB_INCREMENTAL)

    days = max(1, window_days)
    start = (now_cst_value - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return Window(start, now_cst_value, JOB_FULL_RESCAN)


def window_key(window: Window) -> str:
    """窗口的稳定字符串键（用于建批幂等 trigger_key）。"""
    return f"{window.job_type}:{window.start_cst:%Y%m%d%H%M}-{window.end_cst:%Y%m%d%H%M}"
