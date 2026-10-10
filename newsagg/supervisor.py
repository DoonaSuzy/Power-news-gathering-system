"""常驻监督：保持 N 个 worker 一直在跑。

与 ``run-worker`` 的区别：``run-worker`` 用线程池在**同一进程**里跑多个循环；
本模块用**多进程**拉起 ``python -m newsagg worker --once``，任一 worker 被
站点拖垮（内存暴涨 / 卡死）时只有它自己退出，监督器会在 5 秒后补一个新的。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from newsagg.config import get_settings
from newsagg.logutil import get_logger

log = get_logger("supervisor")

__all__ = ["run_supervisor"]

#: 子进程退出后的重启间隔（秒）
_RESTART_DELAY = 5


def run_supervisor(worker_count: int | None = None) -> None:
    """拉起并监督 ``worker_count`` 个 worker 子进程。"""
    settings = get_settings()
    count = worker_count or settings.worker_count
    python = sys.executable or "python"

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")

    procs: list[subprocess.Popen] = []

    def _spawn(index: int) -> subprocess.Popen:
        child_env = dict(env)
        child_env["POWERNEWS_OWNER"] = f"{env.get('POWERNEWS_OWNER', 'supervisor')}-w{index}"
        cmd = [python, "-m", "newsagg", "worker"]
        log.info("拉起 worker#%d: %s", index, " ".join(cmd[1:]))
        return subprocess.Popen(cmd, env=child_env)

    stopping = False

    def _handle_signal(signum, _frame):  # noqa: ANN001
        nonlocal stopping
        log.info("收到信号 %s，准备停止全部 worker", signum)
        stopping = True

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    log.info("监督器启动，目标 worker 数 = %d", count)
    for i in range(count):
        procs.append(_spawn(i))

    try:
        while not stopping:
            time.sleep(2)
            for i, proc in enumerate(procs):
                if proc.poll() is not None:
                    if stopping:
                        continue
                    log.warning("worker#%d 退出（code=%s），%.0f 秒后重启",
                                i, proc.returncode, _RESTART_DELAY)
                    time.sleep(_RESTART_DELAY)
                    procs[i] = _spawn(i)
    finally:
        log.info("监督器停止中，等待 %d 个子进程退出", len(procs))
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        deadline = time.time() + 20
        for proc in procs:
            remaining = max(0.0, deadline - time.time())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.info("监督器已退出")
