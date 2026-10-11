"""时间范围快捷档 → 北京时间闭区间窗口（今日 / 近1周 / 近1个月 / 自定义）。

为什么单独一个模块：同一个「近1周」在 API（``?range=week``）与 CLI
（``--range week``）必须算出**同一个**窗口，否则界面数字和导出对不上。
本模块是唯一实现，两端都调 :func:`resolve_range`。

口径（写死在这里，改动请连同 ``RANGE_LABELS`` 一起改）：
    * 档位按**自然日**算，且**含今天**——``week`` 是「今天 + 之前 6 天」共 7 天，
      不是「过去 168 小时」。滚动小时窗对日粒度归档的新闻没有意义。
    * 起止都是 **naive 北京时间**，与 ``articles.published_at_cst`` 同类型比较；
      绝不使用 ``datetime.now()``——宿主机时区不是 +08:00 时（MySQL 服务器就是
      ``+00:00``），"今日"会整体漂 8 小时。
    * 结束点补到 ``23:59:59``：只给日期就截到 ``00:00:00`` 会静默漏掉当天全部数据。
    * 闭区间两端都含（``>=`` 起、``<=`` 止）。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Final

from newsagg.cleaning import now_cst

__all__ = [
    "RANGE_KEYS",
    "RANGE_LABELS",
    "RANGE_DAYS",
    "start_of_day",
    "end_of_day",
    "resolve_range",
    "describe_range",
]

#: 每档覆盖的自然日数（含今天）
RANGE_DAYS: Final[dict[str, int]] = {"today": 1, "week": 7, "month": 30}

#: 界面标签（Swagger / CLI help 复用，避免文案漂移）。
#: 「自定义」在不同查询面上的参数写法不同（HTTP 是 ``since``、CLI 是 ``--since``），
#: 所以这里只给标签本体，尾巴上的参数名由各自的面自己拼。
RANGE_LABELS: Final[dict[str, str]] = {
    "today": "今日",
    "week": "近1周",
    "month": "近1个月",
    "custom": "自定义",
}

#: 合法取值顺序：界面下拉框按这个顺序渲染
RANGE_KEYS: Final[tuple[str, ...]] = ("today", "week", "month", "custom")


def start_of_day(day: date) -> datetime:
    """某日 00:00:00（naive 北京时间）。"""
    return datetime.combine(day, time.min)


def end_of_day(day: date) -> datetime:
    """某日 23:59:59（naive 北京时间）——闭区间上界。"""
    return datetime.combine(day, time(23, 59, 59))


def resolve_range(
    key: str | None, *, now: datetime | None = None
) -> tuple[datetime, datetime] | None:
    """把快捷档翻译成 ``(date_from, date_to)`` 闭区间。

    ``now`` 只为测试留的注入口，默认取真实的北京时间。

    ``None`` / ``"custom"`` 返回 ``None``，表示「不套用快捷档」，由调用方使用
    自己给的 ``since`` / ``until``；其余取值必须命中 :data:`RANGE_DAYS`，
    否则抛 :class:`ValueError`（消息里列出全部合法值）——传错档位不该被当成
    「不加时间过滤」静默放过。
    """
    if key is None:
        return None
    normalized = str(key).strip().lower()
    if not normalized or normalized == "custom":
        return None
    days = RANGE_DAYS.get(normalized)
    if days is None:
        raise ValueError(
            f"未知的时间范围档位: {key}（可选 {', '.join(RANGE_KEYS)}）"
        )
    today = (now or now_cst()).date()
    return start_of_day(today - timedelta(days=days - 1)), end_of_day(today)


def describe_range(key: str | None, *, now: datetime | None = None) -> str:
    """人类可读的一行说明，如 ``近1周（2026-10-06 ~ 2026-10-12）``。"""
    window = resolve_range(key, now=now)
    label = RANGE_LABELS.get(str(key or "custom").strip().lower(), str(key))
    if window is None:
        return label
    return f"{label}（{window[0]:%Y-%m-%d} ~ {window[1]:%Y-%m-%d}）"
