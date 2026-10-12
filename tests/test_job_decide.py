"""任务终态判定（``CrawlSession.decide``）的回归测试。

## 测什么

核心是一条**真假 bug 的边界**：``listed``（列表页解析出的条目数，
无论新旧）与 ``discovered``（其中本次新发现的候选数）必须分开用。

早期 ``decide()`` 用 ``discovered == 0`` 判断「列表页没拿到候选」，
导致增量采集时**站点没更新也被判成异常** —— 45 个来源里 39 个卡在
``retry_wait``，调度器反复重试。所以这里把每种组合都钉住。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from newsagg.crawl.session import CrawlSession
from newsagg.db import Database
from newsagg.timewindow import build_window


def _session(**counters) -> CrawlSession:
    """构造不连库的会话，并按需预置计数器。"""
    now = datetime(2026, 10, 13, 8, 0)
    window = build_window(
        job_type="incremental",
        now_utc_naive=now - timedelta(hours=8),
        last_discovery_success_at_utc=None,
        window_days=7,
        incremental_lookback_hours=48,
    )
    s = CrawlSession(
        job_id=1, source_id=1, window=window, adapter_name="ctg", db=Database(),
    )
    for k, v in counters.items():
        setattr(s, k, v)
    return s


class TestNormalCases:
    """正常路径。"""

    def test_new_articles_saved_is_succeeded(self):
        s = _session(listed=26, discovered=26, saved=26)
        status, retry = s.decide()
        assert status == "succeeded"
        assert retry == 0

    def test_no_new_articles_is_succeeded_not_retry(self):
        """★ 核心回归：站点本轮没更新 → listed>0 但 discovered=0。

        早期这里返回 retry_wait（误判「列表页没拿到候选」），
        导致 39/45 个来源卡死。正确行为是 **succeeded**。
        """
        s = _session(listed=26, discovered=0, saved=0)
        status, retry = s.decide()
        assert status == "succeeded", "站点无更新是正常情况，不应重试"
        assert retry == 0

    def test_all_candidates_already_known(self):
        """列表有条目但全是旧候选（布隆/库里已见）。"""
        s = _session(listed=40, discovered=0, saved=0)
        assert s.decide()[0] == "succeeded"


class TestAbnormalCases:
    """真异常路径。"""

    def test_listed_zero_is_retry(self):
        """列表页一条都没解析出来 → 站点改版/路径失效，应重试。"""
        s = _session(listed=0, discovered=0, saved=0)
        status, retry = s.decide()
        assert status == "retry_wait"
        assert retry == 120

    def test_listed_zero_even_with_old_articles_in_db(self):
        """库里有很多文章但本轮列表页为空 → 仍是异常。"""
        s = _session(listed=0, discovered=0, saved=0)
        assert s.decide()[0] == "retry_wait"

    def test_discovered_but_nothing_saved_with_failures(self):
        s = _session(listed=26, discovered=26, saved=0, failures=3)
        status, retry = s.decide()
        assert status == "retry_wait"
        assert retry == 30


class TestBudgetAndBlock:
    def test_blocked_triggers_retry(self):
        s = _session(listed=26, discovered=26, saved=26, blocked=5)
        assert s.decide() == ("retry_wait", 60)

    def test_budget_stop_reason(self):
        s = _session(listed=26, discovered=10, saved=5)
        s.stop_reason = "request_budget"
        assert s.decide() == ("retry_wait", 30)

    def test_failure_ratio_stop_reason(self):
        s = _session(listed=26, discovered=10, saved=5)
        s.stop_reason = "failure_ratio"
        assert s.decide() == ("retry_wait", 30)


class TestPartial:
    def test_should_stop_makes_partial(self):
        s = _session(listed=26, discovered=26, saved=10)
        s.request_stop("budget")
        status, _ = s.decide()
        assert status == "partial"

    def test_paging_stop_alone_is_not_partial(self):
        """只停翻页（不是停机）→ 仍算 succeeded。

        这是「翻页终止 ≠ 任务停机」语义在终态判定上的体现。
        """
        s = _session(listed=26, discovered=26, saved=26)
        s.stop_paging_only("window_start_reached")
        assert s.decide()[0] == "succeeded"
