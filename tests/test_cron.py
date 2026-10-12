"""cron 表达式解析与匹配的回归测试。

覆盖：5 字段语法各形态、日/星期 OR 语义、星期 7=周日、越界与语法错误。
这些是「定时任务不重复、不遗漏」的地基 —— 解析错一位就是整批漏触发。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from newsagg.cron import CronError, cron_matches, parse_cron


def dt(minute=0, hour=0, day=1, month=1, weekday_mon0=0):
    """按 cron 语义构造时间。weekday_mon0: 0=周一..6=周日（datetime 惯例）。"""
    # 2026-01-01 是周四；用基期 + 偏移构造，避免手算星期
    base = datetime(2026, 1, 1, 0, 0)  # 周四
    d = base.replace(day=1, month=month, hour=hour, minute=minute)
    d = d.fromordinal(base.toordinal() + weekday_mon0)
    return d.replace(hour=hour, minute=minute, day=day, month=month)


class TestParse:
    def test_valid_expressions(self):
        for expr in ("0 8 * * *", "0 */2 * * *", "30 8 * * 1",
                     "0 9 1 * *", "*/15 * * * *", "0 0 * * 7", "1-30/10 * * * *"):
            c = parse_cron(expr)
            assert c.raw == expr
            assert len(c.fields) == 5

    @pytest.mark.parametrize("expr", [
        "",               # 空
        "0 8 * *",        # 4 字段
        "0 8 * * * *",    # 6 字段
        "60 8 * * *",     # 分钟越界
        "0 24 * * *",     # 小时越界
        "0 8 0 * *",      # 日越界（cron 日是 1-31）
        "0 8 * 13 *",     # 月越界
        "0 8 * * 8",      # 星期越界（只有 0-7）
        "a 8 * * *",      # 非数字
        "0 8 * * 1-",     # 残缺区间
        "0 8 * * */0",    # 步长为 0
        "0 8 * * 5-1",    # 起点大于终点
        "0 8 , * *",      # 空片段
        "0 8 * * 1/2/3",  # 多个斜杠
    ])
    def test_invalid_raises(self, expr):
        with pytest.raises(CronError):
            parse_cron(expr)


class TestDailyFixedTime:
    """每日固定时间 —— 需求 1 的核心形态。"""

    def test_daily_8am(self):
        expr = parse_cron("0 8 * * *")
        assert expr.matches(dt(minute=0, hour=8))
        assert not expr.matches(dt(minute=0, hour=9))
        assert not expr.matches(dt(minute=1, hour=8)), "分钟必须精确为 0"

    def test_daily_multiple_times(self):
        expr = parse_cron("0 8,12,18 * * *")
        for h in (8, 12, 18):
            assert expr.matches(dt(hour=h))
        assert not expr.matches(dt(hour=9))


class TestHourlyAndInterval:
    """每小时 / 每两小时。"""

    def test_every_hour(self):
        expr = parse_cron("0 * * * *")
        for h in range(24):
            assert expr.matches(dt(hour=h))
        assert not expr.matches(dt(hour=5, minute=30))

    def test_every_two_hours(self):
        expr = parse_cron("0 */2 * * *")
        for h in (0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22):
            assert expr.matches(dt(hour=h)), f"{h} 点应触发"
        for h in (1, 3, 5, 7, 9, 11):
            assert not expr.matches(dt(hour=h)), f"{h} 点不应触发"

    def test_step_minutes(self):
        expr = parse_cron("*/15 * * * *")
        for m in (0, 15, 30, 45):
            assert expr.matches(dt(minute=m))
        for m in (1, 14, 16, 59):
            assert not expr.matches(dt(minute=m))


class TestWeekdayAndMonth:
    def test_weekly_monday(self):
        # 2026-01-05 是周一
        expr = parse_cron("0 8 * * 1")
        assert expr.matches(dt(day=5, hour=8, weekday_mon0=0))   # 周一
        assert not expr.matches(dt(day=6, hour=8, weekday_mon0=1))  # 周二

    def test_sunday_is_both_0_and_7(self):
        """星期 7 与 0 都表示周日 —— crontab(5) 明规则。"""
        for expr_s in ("0 8 * * 0", "0 8 * * 7"):
            expr = parse_cron(expr_s)
            assert expr.matches(dt(day=4, hour=8, weekday_mon0=6)), expr_s  # 周日
            assert not expr.matches(dt(day=5, hour=8, weekday_mon0=0)), expr_s

    def test_monthly_first_day(self):
        expr = parse_cron("0 9 1 * *")
        assert expr.matches(dt(day=1, hour=9))
        assert not expr.matches(dt(day=2, hour=9))

    def test_specific_month(self):
        expr = parse_cron("0 9 1 6 *")
        assert expr.matches(dt(day=1, month=6, hour=9))
        assert not expr.matches(dt(day=1, month=7, hour=9))


class TestDayWeekdayOrSemantics:
    """日 与 星期 是 OR —— 这是最容易实现错的地方。"""

    def test_both_set_is_or(self):
        """「每月 1 号」+「周一」→ 两者任一中都触发。"""
        expr = parse_cron("0 8 1 * 1")
        assert expr.matches(dt(day=1, hour=8, weekday_mon0=2)), "1 号（非周一）应触发"
        assert expr.matches(dt(day=5, hour=8, weekday_mon0=0)), "周一（非 1 号）应触发"
        assert not expr.matches(dt(day=6, hour=8, weekday_mon0=1)), "都不中的不触发"

    def test_day_star_uses_weekday_only(self):
        expr = parse_cron("0 8 * * 1")
        assert expr.matches(dt(day=5, hour=8, weekday_mon0=0))
        assert not expr.matches(dt(day=6, hour=8, weekday_mon0=1))

    def test_weekday_star_uses_day_only(self):
        expr = parse_cron("0 8 15 * *")
        assert expr.matches(dt(day=15, hour=8, weekday_mon0=0))
        assert not expr.matches(dt(day=16, hour=8, weekday_mon0=0))


class TestHelper:
    def test_cron_matches_one_shot(self):
        assert cron_matches("0 8 * * *", dt(hour=8))
        assert not cron_matches("0 8 * * *", dt(hour=9))

    def test_weekday_range_with_sunday(self):
        """"0-7" 这类含 7 的区间应归一，不越界。"""
        expr = parse_cron("0 8 * * 0-7")
        assert expr.matches(dt(hour=8, weekday_mon0=0)), "周一到周日每天都中"
