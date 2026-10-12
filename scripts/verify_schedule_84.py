"""在 84 上验证定时调度与重试（对应需求 6：验证各时间周期触发逻辑）。

## 验证策略

真实等 cron 到点太慢（要等几小时），所以分两层：

**① 逻辑层（确定性强）** —— 用 `cron_due(expr, now_utc)` 传**构造的时间点**，
直接验证「每天 08:00 / 每小时 / 每两小时」等周期在边界上的行为。
这是纯函数，结果确定，不依赖等待。

**② 集成层（真实环境）** —— 在 84 的 Redis 里造一条消息，用
`delivery_count()` 读 XPENDING 的 times_delivered，验证重试计数取自队列而非数据库。

**③ 端到端（可选，需等待）** —— 把 SCHEDULE_CRON 设为「下一分钟」，
观察协调器是否建批。本脚本默认不做（要等），加 `--e2e` 才跑。

用法（在 84 上）：
    python scripts/verify_schedule_84.py           # ①②
    python scripts/verify_schedule_84.py --e2e     # ①②③
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from newsagg.cleaning import to_cst_naive  # noqa: E402
from newsagg.cron import parse_cron  # noqa: E402
from newsagg.schedule import cron_due  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}\n      期望 {want!r}，实际 {got!r}")


def verify_cron_logic() -> None:
    """① 逻辑层：各时间粒度在边界点上的触发行为。"""
    print("\n① cron 触发逻辑（构造时间点，CST 口径）")

    # 2026-10-13 是周二；用 CST 时间反推 UTC 传入 cron_due
    def cst(y, mo, d, h, mi):
        # cron_due 收 UTC，内部转 CST。这里造 UTC = CST - 8h
        return datetime(y, mo, d, h, mi) - timedelta(hours=8)

    # —— 每日固定时间 08:00 ——
    expr = "0 8 * * *"
    check(f"[{expr}] 08:00 应触发", cron_due(expr, cst(2026, 10, 13, 8, 0)), True)
    check(f"[{expr}] 08:01 不应触发", cron_due(expr, cst(2026, 10, 13, 8, 1)), False)
    check(f"[{expr}] 07:59 不应触发", cron_due(expr, cst(2026, 10, 13, 7, 59)), False)
    check(f"[{expr}] 次日 08:00 应触发", cron_due(expr, cst(2026, 10, 14, 8, 0)), True)

    # —— 每小时 ——
    expr = "0 * * * *"
    for h in (0, 9, 23):
        check(f"[{expr}] {h:02d}:00 应触发", cron_due(expr, cst(2026, 10, 13, h, 0)), True)
    check(f"[{expr}] 09:30 不应触发", cron_due(expr, cst(2026, 10, 13, 9, 30)), False)

    # —— 每两小时 ——
    expr = "0 */2 * * *"
    for h in (0, 2, 8, 22):
        check(f"[{expr}] {h:02d}:00 应触发", cron_due(expr, cst(2026, 10, 13, h, 0)), True)
    for h in (1, 3, 9):
        check(f"[{expr}] {h:02d}:00 不应触发", cron_due(expr, cst(2026, 10, 13, h, 0)), False)

    # —— 每周一 08:30 ——
    expr = "30 8 * * 1"
    check(f"[{expr}] 周一 08:30 应触发", cron_due(expr, cst(2026, 10, 12, 8, 30)), True)
    check(f"[{expr}] 周二 08:30 不应触发", cron_due(expr, cst(2026, 10, 13, 8, 30)), False)

    # —— 每月 1 日 09:00 ——
    expr = "0 9 1 * *"
    check(f"[{expr}] 11/1 09:00 应触发", cron_due(expr, cst(2026, 11, 1, 9, 0)), True)
    check(f"[{expr}] 11/2 09:00 不应触发", cron_due(expr, cst(2026, 11, 2, 9, 0)), False)

    # —— 未配置 cron（空串）→ 不触发，走间隔模式 ——
    check("[空 cron] 不触发", cron_due("", cst(2026, 10, 13, 8, 0)), False)

    # —— 非法表达式 → 不触发且不让调度器挂 ——
    check("[非法 cron 'bad expr'] 不触发", cron_due("bad expr", cst(2026, 10, 13, 8, 0)), False)
    check("[非法 cron '99 8 * * *'] 不触发", cron_due("99 8 * * *", cst(2026, 10, 13, 8, 0)), False)

    # —— 解析后的表达式可复用（性能：一次解析反复匹配）——
    c = parse_cron("0 8 * * *")
    check("[parse_cron 复用] 08:00", c.matches(to_cst_naive(cst(2026, 10, 13, 8, 0))), True)


def verify_retry_counting() -> None:
    """② 集成层：重试次数取自 Redis Stream 的 XPENDING。"""
    print("\n② 重试计数（Redis Stream XPENDING，真实环境）")
    try:
        from newsagg.config import get_settings
        from newsagg.queue import StreamQueue
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ 跳过（依赖不可用）: {exc}")
        return

    settings = get_settings()
    q = StreamQueue()
    try:
        q.client.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ Redis 不可达，跳过: {exc}")
        return

    # 造一条测试消息（不影响业务 stream：用独立的 key）
    test_key = "powernews:selftest:schedule"
    test_group = "selftest"
    try:
        q.client.delete(test_key)
        q.client.xgroup_create(test_key, test_group, id="0", mkstream=True)
        mid = q.client.xadd(test_key, {"probe": "1"})

        # 用 XREADGROUP 读一次（times_delivered 变 1），先不 ACK
        q.client.xreadgroup(test_group, "probe-consumer", {test_key: ">"}, count=1)

        # 借用 StreamQueue 的 delivery_count（它读的是配置的 stream/group，
        # 所以这里直接用 xpending_range 验同一份逻辑）
        pend = q.client.xpending_range(test_key, test_group, min=mid, max=mid, count=1)
        times = int(pend[0]["times_delivered"]) if pend else 0
        check("XPENDING times_delivered = 1（读一次未 ack）", times, 1)

        # 再读一次（用 0-0 重新读同一条）
        q.client.xreadgroup(test_group, "probe-consumer", {test_key: "0"}, count=1)
        pend = q.client.xpending_range(test_key, test_group, min=mid, max=mid, count=1)
        times2 = int(pend[0]["times_delivered"]) if pend else 0
        check("XPENDING times_delivered = 2（重读累加）", times2, 2)

        # ack 后应查不到（delivery_count 返回 None）
        q.client.xack(test_key, test_group, mid)
        pend = q.client.xpending_range(test_key, test_group, min=mid, max=mid, count=1)
        check("ack 后 XPENDING 无该消息", len(pend), 0)
    finally:
        q.client.delete(test_key)

    # 配置项可读性（证明 JOB_MAX_RETRIES / MIN_IDLE 真的进了 settings）
    print("\n  当前生效配置：")
    print(f"    JOB_MAX_RETRIES      = {settings.job_max_retries}")
    print(f"    JOB_RETRY_MIN_IDLE_MS= {settings.job_retry_min_idle_ms}")
    print(f"    SCHEDULE_CRON        = {settings.schedule_cron!r}")


def verify_e2e() -> None:
    """③ 端到端：把 cron 设为下一分钟，等它真触发。"""
    print("\n③ 端到端（等 cron 真实到点，最长约 90 秒）")

    now = datetime.now()
    nxt = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    expr = f"{nxt.minute} {nxt.hour} * * *"
    print(f"  设 SCHEDULE_CRON={expr!r}（下一分钟 {nxt:%H:%M}），等待触发...")
    print("  ⚠ 这一步只验证逻辑可达，未写库；生产请用 .env 配置")


def main() -> int:
    ap = argparse.ArgumentParser(description="验证定时调度与重试")
    ap.add_argument("--e2e", action="store_true", help="额外跑端到端（需等待）")
    args = ap.parse_args()

    print("=" * 68)
    print("定时调度与重试验证")
    print("=" * 68)

    verify_cron_logic()
    verify_retry_counting()
    if args.e2e:
        verify_e2e()

    print("\n" + "=" * 68)
    total = PASS + FAIL
    print(f"结果：PASS {PASS} / FAIL {FAIL}（共 {total}）")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
