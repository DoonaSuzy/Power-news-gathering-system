"""工作进程主循环。

每个 worker：

1. 从 Redis Stream 读一条任务消息；
2. 用 :func:`classify.classify_delivery` 判定「执行 / 等待 / 直接确认」；
3. 需要执行时**拉起独立子进程**跑 ``python -m newsagg run-job <job_id>``
   （Twisted 反应器不可在同一进程内重启，故必须隔离进程）；
4. 子进程写终态成功后，父进程才 XACK。

子进程隔离同时带来一个好处：单个站点把内存跑爆或卡死，不会带走整个 worker 池。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from newsagg.classify import ACK, WAIT, classify_delivery
from newsagg.config import get_settings
from newsagg.db import Database
from newsagg.errors import PowerNewsError
from newsagg.logutil import bind_context, get_logger
from newsagg.queue import StreamQueue
from newsagg.repo import Repository

log = get_logger("worker")

__all__ = ["worker_identity", "take_assignment", "run_worker"]

#: 等待他人租约时，消息重投的延迟（秒）
_WAIT_RESUBMIT_SECONDS = 30


def worker_identity() -> str:
    """当前 worker 身份：主机名+PID（可用 ``POWERNEWS_OWNER`` 覆盖）。"""
    override = os.environ.get("POWERNEWS_OWNER")
    if override:
        return override
    import socket

    return f"{socket.gethostname()}-{os.getpid()}"


def _utc_now():
    from newsagg.cleaning import now_utc_naive

    return now_utc_naive()


def take_assignment(queue: StreamQueue, db: Database, owner: str) -> bool:
    """取一条任务并处理。返回 True 表示本次确实干了活。"""
    repo = Repository(db)
    # 取新消息失败时**不能直接 return** —— 那会跳过下面的 reclaim_stale，
    # 让 PEL 里的积压消息永远得不到认领。所以这里吞掉异常转去认领。
    messages: list = []
    try:
        messages = queue.take(consumer=owner, block_ms=5000, count=1)
    except PowerNewsError as exc:
        log.warning("取新消息失败，转去认领积压: %s", exc)

    if not messages:
        # 没有新消息时，认领其他 worker 崩溃后遗留的超时消息。
        # 间隔取配置（默认 10 分钟）—— 立即重试会对着同一个失败原因连打，
        # 既浪费请求又刷日志；太长则故障恢复慢。10 分钟是两者折中。
        min_idle_ms = get_settings().job_retry_min_idle_ms
        messages = queue.reclaim_stale(consumer=owner, min_idle_ms=min_idle_ms, count=1)
        if not messages:
            return False

    message_id, job_id = messages[0]
    log_ctx = bind_context(job_id=job_id, run_id=message_id)

    conn = db.connect()
    try:
        job = repo.find_open_job(conn, job_id)
    finally:
        conn.close()

    delivery = classify_delivery(
        job_id=job_id,
        job_exists=job is not None,
        job_status=job.status if job else None,
        lease_owner=job.lease_owner if job else None,
        lease_until_utc=job.lease_until_utc if job else None,
        now_utc=_utc_now(),
        owner=owner,
        retry_at_utc=job.retry_at_utc if job else None,
    )

    if delivery.action == ACK:
        log.info("job %s 直接确认：%s", job_id, delivery.reason)
        queue.ack(message_id)
        return True

    if delivery.action == WAIT:
        log.info("job %s 稍后重试：%s", job_id, delivery.reason)
        if delivery.reason == "retry_not_due":
            # 退避未到点：这条消息本身已经没有价值了，直接确认丢弃。
            # 不能像租约等待那样 publish 一条新的——那会变成「每 5 秒重投一次」
            # 的空转（生产实测 155 个任务堆出 1,955 条积压，把真实抓取饿死）；
            # 也不能留在 PEL 等 XAUTOCLAIM——认领会累加 times_delivered，
            # 把「等待」算成失败尝试。
            # 到点重投由调度器每轮统一做（repo.list_due_retry_jobs）。
            log.info("job %s 退避中（剩余 %ds），交给调度器到点重投",
                     job_id, int(delivery.retry_in_seconds or 0))
            queue.ack(message_id)
            return True
        # 租约被他人持有：重新投递一条新消息，延后处理；原消息确认掉避免堆积
        queue.publish([job_id])
        queue.ack(message_id)
        time.sleep(min(_WAIT_RESUBMIT_SECONDS, 5))
        return True

    # action == RUN
    if _run_job_subprocess(job_id):
        queue.ack(message_id)
    else:
        # 子进程失败：先看是否还有重试额度。
        # 次数来自 Redis Stream 的 XPENDING（times_delivered）——
        # **不依赖给 crawl_jobs 加 attempts 列**：重试次数本是队列的属性，
        # 为它改 DDL 会把队列关注点泄漏到 schema。
        max_retries = get_settings().job_max_retries
        delivered = queue.delivery_count(message_id)
        if delivered is not None and delivered >= max_retries:
            log.warning(
                "job %s 已重试 %s 次（上限 %s），标记失败不再重认领",
                job_id, delivered, max_retries,
            )
            # 不标记的话，这条消息会永远留在 PEL 里被反复认领 ——
            # 项目早期踩过：无上限重试导致同一条失败任务每 10 分钟被拉起来一次，
            # 日志里全是重复错误，反而掩盖真正的问题。
            repo = Repository(db)
            conn = db.connect()
            try:
                repo.mark_job_failed(conn, job_id=job_id,
                                     reason=f"retry_exhausted_{delivered}")
                conn.commit()
            finally:
                conn.close()
            queue.ack(message_id)
        else:
            log.warning(
                "job %s 子进程未成功，保留待重认领（已投递 %s / 上限 %s）",
                job_id, "?" if delivered is None else delivered, max_retries,
            )
    # 释放日志上下文
    from newsagg.logutil import _LOG_CONTEXT

    _LOG_CONTEXT.reset(log_ctx)
    return True


def _run_job_subprocess(job_id: int) -> bool:
    """拉起子进程跑单个任务。返回是否成功（退出码 0）。"""
    python = sys.executable or "python"
    cmd = [python, "-m", "newsagg", "run-job", str(job_id)]
    # 修复模式：强制重抓已发现候选以补齐元数据（由 supervisor 环境注入）
    if os.environ.get("POWERNEWS_REFRESH") == "1":
        cmd.append("--refresh")

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")

    log.info("启动子进程: %s", " ".join(cmd[1:]))
    try:
        proc = subprocess.run(cmd, env=env, timeout=1800, check=False)
    except subprocess.TimeoutExpired:
        log.error("job %s 子进程超时（>30min）", job_id)
        return False
    except OSError as exc:
        log.error("job %s 子进程无法启动: %s", job_id, exc)
        return False

    if proc.returncode != 0:
        log.warning("job %s 子进程退出码 %s", job_id, proc.returncode)
    return proc.returncode == 0


def run_worker(queue: StreamQueue | None = None, db: Database | None = None,
               owner: str | None = None, *, once: bool = False) -> None:
    """worker 常驻主循环。``once=True`` 时只处理一轮（便于测试与 cron）。"""
    settings = get_settings()
    queue = queue or StreamQueue(settings)
    db = db or Database(settings)
    owner = owner or worker_identity()

    log.info("worker %s 启动（stream=%s）", owner, queue.stream)
    try:
        while True:
            try:
                worked = take_assignment(queue, db, owner)
            except PowerNewsError as exc:
                log.warning("worker %s 业务异常，继续: %s", owner, exc)
                worked = False
            except Exception as exc:  # noqa: BLE001
                # Redis 阻塞读偶发 "Timeout reading from socket"，
                # 不能让 worker 因此退出：重建连接后继续消费。
                log.warning("worker %s 消费异常，重连队列: %s", owner, exc)
                time.sleep(2)
                try:
                    queue = StreamQueue(settings)
                    queue.ensure_group()
                except Exception:  # noqa: BLE001
                    pass
                continue

            if once:
                return
            if not worked:
                time.sleep(1)
    except KeyboardInterrupt:
        log.info("worker %s 停止", owner)
