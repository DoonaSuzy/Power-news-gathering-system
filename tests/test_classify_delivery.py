"""投递决策 `classify_delivery()` 的回归测试。

这个函数此前**零覆盖**——`tests/test_job_decide.py` 只测 `CrawlSession.decide()`
（任务**该以什么状态结束**），而这里测的是 worker 拿到一条队列消息后
**该执行、该等待、还是该直接确认**。两者是两个不同的决策点，混在一起会漏。

用例分两组：
1. 终态与租约（`partial` 终态化时补的）；
2. 重试到期判定（`retry_at_utc` 从「只写不读」改成真读时补的）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from newsagg.classify import (
    ACK,
    RUN,
    TERMINAL_STATUSES,
    WAIT,
    classify_delivery,
)

_NOW = datetime(2026, 10, 28, 0, 0)


def _call(**overrides):
    """用一组合理默认值调 `classify_delivery`，只覆盖本用例关心的参数。"""
    kwargs = {
        "job_id": 1,
        "job_exists": True,
        "job_status": "queued",
        "lease_owner": None,
        "lease_until_utc": None,
        "now_utc": _NOW,
        "owner": "worker-a",
    }
    kwargs.update(overrides)
    return classify_delivery(**kwargs)


# ----------------------------------------------------------------------
# 1. 终态：到了终态就必须 ACK，否则消息会在 PEL 里被反复认领
# ----------------------------------------------------------------------
@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_terminal_status_is_acked(status: str):
    delivery = _call(job_status=status)
    assert delivery.action == ACK
    assert delivery.reason == f"terminal:{status}"


def test_partial_counts_as_terminal():
    """`partial` 必须算终态。

    它表示「抓到了，但被预算/阻断提前收手」。不算终态时：CLI 退出码 1 →
    消息不 ACK → 重跑（`consider()` 会跳过已登记候选，补不回漏抓的）→
    三次后被 `mark_job_failed` 覆盖成 failed，连「抓到过」这个事实都丢了。
    """
    assert "partial" in TERMINAL_STATUSES
    assert _call(job_status="partial").action == ACK


def test_missing_job_is_acked():
    """任务已被清理：消息没有意义，直接丢弃。"""
    delivery = _call(job_exists=False)
    assert delivery.action == ACK
    assert delivery.reason == "job_missing"


# ----------------------------------------------------------------------
# 2. 租约
# ----------------------------------------------------------------------
def test_lease_held_by_other_waits():
    delivery = _call(
        job_status="running",
        lease_owner="worker-b",
        lease_until_utc=_NOW + timedelta(minutes=5),
    )
    assert delivery.action == WAIT
    assert delivery.reason == "lease_held_by:worker-b"


def test_expired_lease_can_run():
    """租约过期说明持有者已死，本 worker 可以接手。"""
    delivery = _call(
        job_status="running",
        lease_owner="worker-b",
        lease_until_utc=_NOW - timedelta(minutes=1),
    )
    assert delivery.action == RUN


def test_own_lease_runs():
    delivery = _call(
        job_status="running",
        lease_owner="worker-a",
        lease_until_utc=_NOW + timedelta(minutes=5),
    )
    assert delivery.action == RUN


def test_ready_job_runs():
    delivery = _call(job_status="queued")
    assert delivery.action == RUN
    assert delivery.reason == "ready"


# ----------------------------------------------------------------------
# 3. 重试到期（retry_at_utc 真正生效）
# ----------------------------------------------------------------------
def test_retry_wait_before_due_waits():
    """退避未到：不能跑，且要告诉调用方还剩多少秒。"""
    delivery = _call(
        job_status="retry_wait",
        retry_at_utc=_NOW + timedelta(minutes=30),
    )
    assert delivery.action == WAIT
    assert delivery.reason == "retry_not_due"
    assert delivery.retry_in_seconds == pytest.approx(1800, abs=1)


def test_retry_wait_due_runs():
    delivery = _call(
        job_status="retry_wait",
        retry_at_utc=_NOW - timedelta(minutes=1),
    )
    assert delivery.action == RUN
    assert delivery.reason == "retry_due"


def test_retry_wait_exactly_due_runs():
    """到点即放行——比较符号写成 ``<`` 就会多等一轮。"""
    assert _call(job_status="retry_wait", retry_at_utc=_NOW).action == RUN


def test_retry_wait_without_deadline_runs():
    """历史脏数据（列为 NULL）按到期处理，不能永久卡住。"""
    delivery = _call(job_status="retry_wait", retry_at_utc=None)
    assert delivery.action == RUN
    assert delivery.reason == "retry_due"
