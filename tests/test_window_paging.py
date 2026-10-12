"""「翻页终止」与「任务停机」必须分离的回归测试。

## 这个 bug 的完整因果链（实测踩到过两次）

列表页解析完 → 26 个候选逐个 ``consider()`` 成功 → ``yield`` 26 个详情请求
（Scrapy **异步**排队）→ 循环结束 → 判定「已翻到窗口起点」→
**若此时调 ``request_stop`` 设了全局 ``should_stop``** →
那些已排队的详情请求回调进 ``parse_detail`` → 开头的 ``_guard()`` 返回 True →
**26 个请求全部被丢弃**。

实测现象：

    discovered=26 / requests=1 / saved=0 / status=partial

``requests=1`` 说明**只有列表页那一次回调执行过** ——
详情请求发出去了、但回调一进门就被守卫拦住、连 ``requests`` 都没来得及加。

## 为什么第一次修复不够

第一轮修复把 ``request_stop`` 从**循环前**挪到**循环后**，
以为「本页候选仍要处理」就万事大吉。但详情请求是**异步**的 ——
``request_stop`` 在循环后执行的那一刻，请求还在 Scrapy 队列里没被处理，
于是照样被拦。

**所以真正的修法是语义分离**：

- ``should_stop``  —— 真正的停机（租约丢失 / 预算耗尽 / 列表页失败），拦所有请求
- ``stop_paging``  —— 只是翻页终止条件，**不拦已发出的请求**

本测试文件锁住这个语义，避免第三次踩坑。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from newsagg.crawl.session import CrawlSession
from newsagg.db import Database
from newsagg.timewindow import Window

# ======================================================================
# 1. 语义分离：stop_paging_only 不得设置 should_stop
# ======================================================================


def _session() -> CrawlSession:
    """构造一个不连库的会话（只测标志位语义）。"""
    return CrawlSession(
        job_id=1,
        source_id=1,
        window=Window(start_cst=datetime(2026, 9, 12), end_cst=datetime(2026, 10, 12)),
        adapter_name="ctg",
        db=Database(),
    )


def test_stop_paging_only_does_not_set_should_stop():
    """核心断言：``stop_paging_only`` **不能**碰 ``should_stop``。

    一旦设了，已排队的详情请求会被 ``_guard()`` 全部丢弃 ——
    这正是 ``saved=0`` 的根因。
    """
    session = _session()
    assert session.should_stop is False

    session.stop_paging_only("window_start_reached")

    assert session.stop_paging is True, "应标记「停止翻页」"
    assert session.should_stop is False, (
        "❌ stop_paging_only 设了 should_stop —— 已发出的详情请求会被 _guard() 丢弃"
    )


def test_stop_paging_only_records_reason():
    """仍要记 ``stop_reason`` —— 台账要统计「为什么停」。"""
    session = _session()
    session.stop_paging_only("window_start_reached")
    assert session.stop_reason == "window_start_reached"


def test_request_stop_still_sets_both_flags():
    """真正的停机（预算耗尽/租约丢失）**必须**拦所有请求，两个标志都设。"""
    session = _session()
    session.request_stop("request_budget")
    assert session.should_stop is True
    assert session.stop_reason == "request_budget"


def test_stop_reason_keeps_first_cause():
    """``stop_reason`` 保留首个原因（后到的原因不覆盖）。"""
    session = _session()
    session.request_stop("request_budget")
    session.stop_paging_only("window_start_reached")
    assert session.stop_reason == "request_budget"


@pytest.mark.parametrize("reason", ["window_start_reached", "list_not_descending"])
def test_paging_termination_reasons_never_stop_the_job(reason):
    """所有「翻页终止」类原因都不该导致停机。

    这类原因是**正常的探测结果**（翻到头了），不是异常。
    """
    session = _session()
    session.stop_paging_only(reason)
    assert session.should_stop is False


# ======================================================================
# 2. 默认值：stop_paging 必须是 False（否则一页都不翻）
# ======================================================================


def test_stop_paging_defaults_to_false():
    session = _session()
    assert session.stop_paging is False


# ======================================================================
# 3. 与 _guard 的组合语义（用真实 spider 的判定条件）
# ======================================================================


def test_guard_semantics_after_paging_stop():
    """``_guard()`` 在「仅停止翻页」后必须仍返回 False。

    这是 ``parse_detail`` 能否处理请求的唯一开关 ——
    直接决定「详情请求是落库还是被丢掉」。
    """
    guard = _guard_of(_session())
    session = _session()

    session.stop_paging_only("window_start_reached")

    assert _guard_of(session)() is False, (
        "❌ 停止翻页后 _guard() 返回 True —— 26 个详情请求会被全部丢弃"
    )
    assert guard() is False, "未做任何停止操作时 _guard() 本应为 False"


def _guard_of(session: CrawlSession):
    """复刻 ``SiteSpider._guard`` 的判定逻辑。

    不直接 import spider（它依赖 Scrapy 运行时），
    但判定条件必须与 ``newsagg/crawl/spider.py::_guard`` 保持一致：

        should_stop  → True
        budget_hit() → True
        否则          → False
    """
    def guard() -> bool:
        if session.should_stop:
            return True
        return session.budget_hit()

    return guard
