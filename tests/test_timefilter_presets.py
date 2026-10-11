"""时间范围筛选（今日 / 近1周 / 近1个月 / 自定义）的回归测试。

全部离线：窗口数学、两个查询面（API 依赖与 CLI 参数）的语义对齐、
SQL 条件编译。真实数据上的「命中数对不对」由 ``scripts/news_time_filter_audit.py``
对着 MySQL 核验（本文件的断言只保证「代码算出的窗口」与「界面标签」一致）。

钉住的坑：
1. 中台容器与 MySQL 服务器的时区都是 ``+00:00``，只有 Windows 开发机是 +08:00。
   用宿主机 ``datetime.now()`` 算「今日」，在生产上会整体漂 8 小时。
2. 只给日期却截到 ``00:00:00``，会静默漏掉当天全部新闻（旧项目踩过）。
3. 同一个「近1周」在 API 与 CLI 算出不同窗口 → 看板数与导出 CSV 对不上。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
from datetime import datetime, timedelta
from typing import get_args, get_origin, get_type_hints

import pytest

from newsagg.cleaning import CST
from newsagg.query import ArticleFilter, build_where
from newsagg.timefilter import RANGE_DAYS, RANGE_KEYS, describe_range, resolve_range
from newsplatform.core.deps import (
    CATEGORY_CHOICES,
    Pagination,
    TimeRange,
    get_article_query,
    parse_day,
)
from newsplatform.core.exceptions import BadRequest
from newsplatform.repositories.articles import ArticleQuery, ArticleRepository

# 2026-10-12 02:09:44 是覆盖审计时的真实北京时间（跨零点样本：UTC 侧还停在 10-11）
_NOW = datetime(2026, 10, 12, 2, 9, 44)


def _api_query(**kw) -> ArticleQuery:
    """跑一次真实的依赖函数。

    ``resolve_province_codes`` 在 ``province=None`` 时不碰会话，所以会话传个
    占位对象即可——本用例不需要数据库。
    """
    args: dict = {
        "pagination": Pagination(page=1, page_size=20),
        "session": object(),
        "range_key": None,
        "since": None,
        "until": None,
    }
    args.update(kw)
    return asyncio.run(get_article_query(**args))


# ======================================================================
# 1. 窗口数学
# ======================================================================
def test_each_preset_is_inclusive_natural_days_ending_today():
    """档位覆盖的自然日数含今天，且上界是当天 23:59:59。"""
    expect = {"today": 1, "week": 7, "month": 30}
    assert RANGE_DAYS == expect
    for key, days in expect.items():
        date_from, date_to = resolve_range(key, now=_NOW)
        assert date_from == datetime(2026, 10, 12) - timedelta(days=days - 1)
        assert date_to == datetime(2026, 10, 12, 23, 59, 59)
        # 闭区间天数自证：含头含尾正好 days 天
        assert (date_to.date() - date_from.date()).days + 1 == days


def test_presets_roll_over_midnight_not_off_by_eight_hours():
    """北京 00:30 时「今日」必须是新的一天。

    这正是生产环境的形状：MySQL 容器 ``time_zone=+00:00``，北京 00:30 时它的
    ``CURDATE()`` 还是昨天。谁把 ``now_cst()`` 换成 ``datetime.now()`` 就会红。
    """
    just_after_midnight = datetime(2026, 10, 12, 0, 30, 0)
    date_from, date_to = resolve_range("today", now=just_after_midnight)
    assert date_from.date() == date_to.date() == just_after_midnight.date()

    # 默认取北京时间，与宿主机时区无关
    assert resolve_range("today")[0].date() == datetime.now(CST).date()
    assert "datetime.now()" not in inspect.getsource(resolve_range)


def test_custom_and_absent_preset_defer_to_explicit_bounds():
    """``custom`` / 不传 → 返回 None，由调用方用 since/until；不能当成「今日」。"""
    assert resolve_range("custom", now=_NOW) is None
    assert resolve_range(None) is None
    assert resolve_range("  Custom ", now=_NOW) is None


def test_unknown_preset_fails_loudly_instead_of_dropping_the_filter():
    """错档位必须报错。静默退化成「不加时间过滤」会把三个月数据当成今日返回。"""
    with pytest.raises(ValueError) as caught:
        resolve_range("last-year", now=_NOW)
    for key in RANGE_KEYS:
        assert key in str(caught.value)


def test_describe_range_shows_resolved_window():
    assert describe_range("week", now=_NOW) == "近1周（2026-10-06 ~ 2026-10-12）"
    assert describe_range("custom", now=_NOW).startswith("自定义")


# ======================================================================
# 2. API 侧：query string → ArticleQuery
# ======================================================================
def test_api_range_param_is_a_swagger_dropdown():
    """``?range=`` 必须是枚举（Swagger 才渲染成下拉控件），且与 timefilter 同源。"""
    annotated = get_type_hints(get_article_query, include_extras=True)["range_key"]
    assert annotated.__metadata__[0].alias == "range"
    assert set(get_args(TimeRange)) == set(RANGE_KEYS)
    assert get_origin(TimeRange) is not None  # 是 Literal，不是裸 str


def test_api_preset_and_custom_bounds_are_mutually_exclusive():
    """两者同时给 → 400，而不是「按某一个静默优先」。"""
    with pytest.raises(BadRequest, match="二选一"):
        _api_query(range_key="week", since="2026-01-01")
    with pytest.raises(BadRequest, match="二选一"):
        _api_query(range_key="month", until="2026-01-01")


def test_api_custom_still_includes_whole_last_day():
    """自定义区间保留 ``until=日期`` 补齐 23:59:59 的老行为。"""
    q = _api_query(range_key="custom", since="2026-09-12", until="2026-10-12")
    assert (q.date_from, q.date_to) == (
        datetime(2026, 9, 12, 0, 0),
        datetime(2026, 10, 12, 23, 59, 59),
    )


def test_api_range_resolves_same_window_as_timefilter():
    today = datetime.now(CST).date()
    q = _api_query(range_key="today")
    assert (q.date_from, q.date_to) == (
        datetime.combine(today, datetime.min.time()),
        datetime.combine(today, datetime.max.time()).replace(microsecond=0),
    )


def test_gov_category_is_filterable():
    """``category`` 合法值取自种子清单；漏了 gov 会让那批文章查不出来。"""
    assert "gov" in CATEGORY_CHOICES
    q = _api_query(category="gov")
    assert (q.date_from, q.date_to) == (None, None)  # 没套时间窗，也没报错


# ======================================================================
# 3. 两个查询面不得漂移
# ======================================================================
def _cli_filter(**kw) -> ArticleFilter:
    from newsagg.cli import _filters_from_args

    ns = argparse.Namespace(
        province=None, category=None, source=None, keyword=None,
        range=None, since=None, until=None,
        body_only=False, exclude_duplicates=False, page=1, limit=20,
    )
    for key, value in kw.items():
        setattr(ns, key, value)
    return _filters_from_args(ns)


def test_cli_and_api_resolve_the_same_window():
    """同一个档位，CLI 导出与 API 列表必须命中同一批行。"""
    for key in ("today", "week", "month"):
        cli = _cli_filter(range=key)
        api = _api_query(range_key=key)
        assert (cli.date_from, cli.date_to) == (api.date_from, api.date_to)


def test_cli_rejects_mixing_preset_with_custom_bounds():
    from newsagg.errors import PowerNewsError

    with pytest.raises(PowerNewsError, match="二选一"):
        _cli_filter(range="week", since="2026-01-01")


def test_sql_layers_emit_identical_time_predicates():
    """SQLAlchemy 仓储与原生 SQL 两处 WHERE 的时间字段/边界一致。"""
    date_from, date_to = resolve_range("month", now=_NOW)

    conds = [str(c) for c in ArticleRepository.conditions(
        ArticleQuery(date_from=date_from, date_to=date_to)
    )]
    assert len(conds) == 2
    assert any("published_at_cst >=" in c.replace("\n", " ") for c in conds)
    assert any("published_at_cst <=" in c.replace("\n", " ") for c in conds)

    clause, params = build_where(ArticleFilter(date_from=date_from, date_to=date_to))
    assert clause.count("published_at_cst") == 2
    assert ">=" in clause and "<=" in clause
    assert list(params) == [date_from, date_to]


def test_parse_day_keeps_end_of_day_semantics():
    """老坑的最后一道闸：日期型 until 补齐到 23:59:59。"""
    assert parse_day("2026-10-12", end_of_day=True) == datetime(2026, 10, 12, 23, 59, 59)
    assert parse_day("2026-10-12") == datetime(2026, 10, 12, 0, 0, 0)
    assert parse_day("2026-10-12 08:30") == datetime(2026, 10, 12, 8, 30)
