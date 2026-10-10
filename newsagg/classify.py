"""投递决策：worker 拿到一条队列消息后，决定「执行 / 等待 / 直接确认」。

这一步把「该不该真正启动一个抓取子进程」的判断从 worker 主循环里剥出来，
使其可以脱离 Redis 与 Scrapy 单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

__all__ = ["Delivery", "classify_delivery"]

#: 决策动作
RUN = "run"        # 执行任务
WAIT = "wait"      # 他人持有租约，稍后重投
ACK = "ack"        # 已终态，直接确认丢弃

#: 视为终态的任务状态
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "skipped", "cancelled"})


@dataclass(frozen=True, slots=True)
class Delivery:
    """一次投递的处置结论。"""

    action: str
    reason: str
    job_id: int

    @property
    def should_run(self) -> bool:
        return self.action == RUN


def classify_delivery(
    *,
    job_id: int,
    job_exists: bool,
    job_status: str | None,
    lease_owner: str | None,
    lease_until_utc: datetime | None,
    now_utc: datetime,
    owner: str,
) -> Delivery:
    """判定一条队列消息该如何处置。

    :param job_exists: 任务是否存在于库中（已被清理则为 False）
    :param job_status: 任务当前状态
    :param lease_owner: 当前租约持有者
    :param lease_until_utc: 租约到期时间
    :param now_utc: 当前 UTC 时间
    :param owner: 本 worker 身份
    """
    if not job_exists:
        return Delivery(ACK, "job_missing", job_id)

    if job_status in TERMINAL_STATUSES:
        return Delivery(ACK, f"terminal:{job_status}", job_id)

    if job_status == "retry_wait":
        # 未到重试时刻则等待，否则放行
        return Delivery(RUN, "retry_due", job_id)

    holding = bool(lease_owner) and lease_owner != owner
    expired = lease_until_utc is not None and lease_until_utc <= now_utc
    if holding and not expired:
        return Delivery(WAIT, f"lease_held_by:{lease_owner}", job_id)

    return Delivery(RUN, "ready", job_id)
