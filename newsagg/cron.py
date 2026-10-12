"""Cron 表达式解析与匹配（支持「每日固定时间」等周期触发）。

## 为什么自己写而不用 croniter / APScheduler

本项目调度已有成熟链路（``schedule.py --loop`` 协调器 + Redis Stream），
缺的只是「每日固定时间」这一种触发形态。为此引入 APScheduler（几十个传递依赖）
或 croniter（额外依赖）性价比极低 —— 一个 5 字段 cron 匹配器约 60 行，
无依赖、可单测、行为完全可控。

## 支持的语法（标准 5 字段 cron）

    ┌────────── 分钟        (0-59)
    │ ┌──────── 小时        (0-23)
    │ │ ┌────── 日          (1-31)
    │ │ │ ┌──── 月          (1-12)
    │ │ │ │ ┌── 星期        (0-6，0=周日；7 也视为周日)
    │ │ │ │ │
    * * * * *

每个字段支持：

* ``*``        任意值
* ``5``        单值
* ``1,3,5``    枚举
* ``1-5``      区间
* ``*/15``     步长（从 0 起）
* ``1-30/10``  区间内步长

## 语义边界（**刻意与 cron 标准一致，别"优化"**）

* **日 与 星期 是 OR 关系**：两者都非 ``*`` 时，任一方匹配即触发
  （Vixie cron 语义）。若改成 AND，「每月 1 号」和「每周一」无法同时表达。
* 星期 ``7`` 规范为周日（与 ``0`` 等价）—— 这是 crontab(5) 的明规则。
* 不支持 ``L``/``W``/``?``/名称（``JAN``/``MON``）—— 本项目用不到，
  加了反而多一堆要测的分支。

## 示例

    "0 8 * * *"     每天 08:00
    "0 */2 * * *"   每两小时（偶数整点）
    "30 8 * * 1"    每周一 08:30
    "0 9 1 * *"     每月 1 日 09:00
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["CronExpr", "parse_cron", "cron_matches"]

#: 各字段的取值范围（分钟, 小时, 日, 月, 星期）
_FIELD_RANGES: tuple[tuple[int, int], ...] = (
    (0, 59), (0, 23), (1, 31), (1, 12), (0, 7),
)

_FIELD_NAMES = ("minute", "hour", "day", "month", "weekday")

_TOKEN = re.compile(r"^(?:\*|\d+)(?:-\d+)?(?:/\d+)?$")


class CronError(ValueError):
    """cron 表达式非法。"""


@dataclass(frozen=True, slots=True)
class _Field:
    """单个字段的匹配集合。"""

    values: frozenset[int]
    is_star: bool  # 原表达式是否为 ``*``（用于日/星期的 OR 判定）


def _parse_field(raw: str, index: int) -> _Field:
    """解析单个字段。

    Raises:
        CronError: 语法错误或取值越界。
    """
    name = _FIELD_NAMES[index]
    lo, hi = _FIELD_RANGES[index]
    raw = raw.strip()
    if not raw:
        raise CronError(f"字段 {name} 为空")

    # 星期 7 规范为周日：先归一到 0-6，避免 7 越界
    if index == 4 and raw == "7":
        raw = "0"

    if raw == "*":
        return _Field(frozenset(range(lo, hi + 1)), is_star=True)

    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"字段 {name} 存在空片段: {raw!r}")
        if not _TOKEN.match(part):
            raise CronError(f"字段 {name} 语法错误: {part!r}")

        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) == 0:
                raise CronError(f"字段 {name} 步长非法: {part!r}")
            step = int(step_s)
            part = base

        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            start_s, _, end_s = part.partition("-")
            if not (start_s.isdigit() and end_s.isdigit()):
                raise CronError(f"字段 {name} 区间非法: {part!r}")
            start, end = int(start_s), int(end_s)
        else:
            if not part.isdigit():
                raise CronError(f"字段 {name} 取值非法: {part!r}")
            start = end = int(part)

        # 单值 + 步长（如 5/10）语义上等于「从 5 起每 10」，与 cron 一致
        if start > end:
            raise CronError(f"字段 {name} 区间起点大于终点: {part!r}")
        if not (lo <= start <= hi and lo <= end <= hi):
            raise CronError(
                f"字段 {name} 取值越界: {part!r}（允许 {lo}-{hi}）"
            )
        values.update(range(start, end + 1, step))

    if not values:
        raise CronError(f"字段 {name} 解析结果为空: {raw!r}")

    # 星期归一并集后若含 7，转成 0（处理 "0-7" 这类区间）
    if index == 4 and 7 in values:
        values.discard(7)
        values.add(0)
    return _Field(frozenset(values), is_star=False)


@dataclass(frozen=True, slots=True)
class CronExpr:
    """解析后的 cron 表达式。

    Attributes:
        raw: 原始表达式（用于日志与错误信息）。
        fields: 5 个字段的匹配集合，顺序为 分/时/日/月/星期。
    """

    raw: str
    fields: tuple[_Field, ...]

    def matches(self, dt) -> bool:
        """判断某个时间点是否命中。

        Args:
            dt: 带 ``minute/hour/day/month/weekday`` 属性的时间对象
                （``datetime`` 即可；注意 ``weekday()`` 是 0=周一，
                 而 cron 是 0=周日，这里做了换算）。

        Returns:
            True 表示该分钟的 :00 秒应触发。
        """
        minute, hour, day, month, weekday = self.fields
        # datetime.weekday(): 0=周一..6=周日 → cron: 0=周日..6=周六
        cron_wd = (dt.weekday() + 1) % 7
        if dt.minute not in minute.values or dt.hour not in hour.values:
            return False
        if dt.month not in month.values:
            return False

        # 日 / 星期 的 OR 语义：两者都非 * 时任一方匹配即触发
        day_ok = dt.day in day.values
        wd_ok = cron_wd in weekday.values
        if day.is_star and weekday.is_star:
            return True
        if day.is_star:
            return wd_ok
        if weekday.is_star:
            return day_ok
        return day_ok or wd_ok


def parse_cron(expr: str) -> CronExpr:
    """解析 cron 表达式。

    Args:
        expr: 5 字段 cron 表达式，如 ``"0 8 * * *"``。

    Returns:
        解析后的 :class:`CronExpr`。

    Raises:
        CronError: 字段数不符或任一字段非法。
    """
    if not isinstance(expr, str):
        raise CronError(f"cron 表达式必须是字符串，得到 {type(expr).__name__}")
    parts = expr.split()
    if len(parts) != 5:
        raise CronError(
            f"cron 表达式应为 5 个字段（分 时 日 月 星期），得到 {len(parts)} 个: {expr!r}"
        )
    fields = tuple(_parse_field(p, i) for i, p in enumerate(parts))
    return CronExpr(raw=expr.strip(), fields=fields)


def cron_matches(expr: str, dt) -> bool:
    """一步到位的匹配（解析 + 判断）。

    适合调用方只关心「这一分钟要不要触发」的场景；
    若同一表达式要反复匹配，应 :func:`parse_cron` 一次后复用 :meth:`CronExpr.matches`。
    """
    return parse_cron(expr).matches(dt)
