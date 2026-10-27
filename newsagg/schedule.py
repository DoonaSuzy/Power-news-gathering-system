"""定时协调器：按采集频率与时间窗口建批 → 发布任务到 Redis Stream。

差异化采集策略（改造方案 §3.3 / 来源清单 §七）由 ``sources.crawl_interval_hours``
与 ``sources.priority`` 两个字段驱动：

- ``priority=0``（国家电网/南方电网/蒙东，每小时）——每轮都建批；
- ``priority=1``（港澳台、中国电力新闻网，2–4 小时）——按 ``crawl_interval_hours`` 间隔；
- ``priority=2``（北极星/国际能源网、省级党报专栏，4–6 小时）——同上，更长间隔。

建批幂等靠 ``crawl_jobs`` 的唯一键 ``(source_id, trigger_key)``，
多实例同时跑也只会建出一份任务。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from newsagg.cleaning import now_cst, now_utc_naive
from newsagg.config import get_settings
from newsagg.cron import CronError, parse_cron
from newsagg.db import Database
from newsagg.logutil import bind_context, get_logger
from newsagg.queue import StreamQueue
from newsagg.repo import Repository
from newsagg.timewindow import JOB_FULL_RESCAN, JOB_INCREMENTAL, build_window

log = get_logger("schedule")

__all__ = ["run_coordinator", "build_triggers", "cron_due", "publish_jobs", "main"]


def build_triggers(now_utc: datetime | None = None) -> str:
    """生成本轮批次的 trigger_key（按小时粒度，天然幂等）。"""
    now_utc = now_utc or now_utc_naive()
    cst = now_cst() if now_utc is None else None
    if cst is None:
        from newsagg.cleaning import to_cst_naive

        cst = to_cst_naive(now_utc)
    return f"hourly:{cst:%Y%m%d%H}"


def cron_due(expr: str, now_utc: datetime | None = None) -> bool:
    """判断当前时刻是否命中 cron 表达式。

    Args:
        expr: 5 字段 cron 表达式；空串表示未启用 cron 模式。
        now_utc: 当前 UTC 时间（默认取系统时间）。

    Returns:
        True 表示应触发。表达式非法时**返回 False 并记 error** ——
        调度器不能因为一条配置写错就整体停摆，但必须留下可见的错误。

    Note:
        按 **CST（北京时间）** 匹配，与项目其它时间口径一致
        （用户配「每天 08:00」指的是北京时间 08:00，不是 UTC）。
    """
    if not expr or not expr.strip():
        return False
    now_utc = now_utc or now_utc_naive()
    try:
        from newsagg.cleaning import to_cst_naive

        cst = to_cst_naive(now_utc)
        return parse_cron(expr).matches(cst)
    except CronError as exc:
        log.error("SCHEDULE_CRON 配置非法，本轮按间隔模式处理: %s", exc)
        return False


def publish_jobs(job_ids: list[int], meta: dict | None = None,
                 queue: StreamQueue | None = None) -> int:
    """把任务 id 投递到 Redis Stream。"""
    if not job_ids:
        return 0
    queue = queue or StreamQueue()
    return queue.publish(job_ids)


def run_coordinator(*, force: bool = False, dry_run: bool = False,
                    now_utc: datetime | None = None,
                    db: Database | None = None,
                    queue: StreamQueue | None = None) -> dict[str, int]:
    """跑一轮协调：筛选到期来源 → 建批 → 投递。

    :param force: 忽略采集间隔，全部来源强制建批
    :returns: ``{"considered": n, "created": n, "published": n}``
    """
    settings = get_settings()
    db = db or Database(settings)
    repo = Repository(db)
    now_utc = now_utc or now_utc_naive()
    trigger = build_triggers(now_utc)

    # cron 命中 → 本轮忽略「间隔小时」，全部来源强制建批。
    # 两种模式是**叠加**关系：cron 命中 或 间隔到期，任一满足即建批。
    cron_hit = cron_due(settings.schedule_cron, now_utc)
    if cron_hit:
        log.info("cron %s 命中，本轮强制建批", settings.schedule_cron)
        force = True
        # trigger_key 加 cron 前缀，避免与同小时的 hourly 批次**撞唯一键**
        # （crawl_jobs 的 trigger_key 有唯一约束，撞了会静默建不出批）。
        trigger = f"cron:{settings.schedule_cron}:{trigger}"

    conn = db.connect()
    job_ids: list[int] = []
    considered = 0
    try:
        # 先回收僵死任务 —— 必须在建批**之前**做。
        # 否则那些被卡住的源会被「正在跑」挡住，永远建不出新批
        # （84 生产实测：bjx 卡 14h、浙江/湖北各卡 2h，期间完全停采且无告警）。
        # 阈值取 2 倍租约，避免误杀跑得慢的正常任务。
        try:
            reaped = repo.reap_stale_jobs(conn, older_than_seconds=settings.lease_seconds * 2)
            if reaped:
                conn.commit()
                log.warning("回收僵死任务 %d 个（进程已死但状态停在 running）", reaped)
        except Exception as exc:  # noqa: BLE001
            log.error("回收僵死任务失败（不影响本轮协调）: %s", exc)

        # 到点的重试任务在这里重投。worker 只负责「丢弃未到点的消息」，
        # 不 sleep、不反复 publish —— 否则退避期间会一直空转（生产踩过）。
        due_ids: list[int] = []
        try:
            due_ids = repo.list_due_retry_jobs(conn, limit=200)
            if due_ids:
                log.info("到点重投 %d 个 retry_wait 任务", len(due_ids))
        except Exception as exc:  # noqa: BLE001
            log.error("查询到点重试任务失败（不影响本轮协调）: %s", exc)

        run_id = repo.create_run(conn, trigger)
        sources = repo.list_sources(conn, enabled_only=True)
        considered = len(sources)

        for src in sources:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cooldown_until_utc, last_discovery_success_at_utc "
                    "FROM sources WHERE id=%s",
                    (src.id,),
                )
                state = cur.fetchone() or {}

            cooldown = state.get("cooldown_until_utc")
            if cooldown and cooldown > now_utc:
                log.info("来源 %s 处于冷却期，跳过", src.adapter_name)
                continue

            # 按 crawl_interval_hours 判断是否到期
            if not force:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT finished_at_utc FROM crawl_jobs WHERE source_id=%s "
                        "AND status='succeeded' ORDER BY id DESC LIMIT 1",
                        (src.id,),
                    )
                    last = (cur.fetchone() or {}).get("finished_at_utc")
                if last and last > now_utc - timedelta(hours=max(1, src.crawl_interval_hours)):
                    continue

            last_success = state.get("last_discovery_success_at_utc")
            job_type = JOB_INCREMENTAL if last_success else JOB_FULL_RESCAN
            window = build_window(
                job_type=job_type,
                now_utc_naive=now_utc,
                last_discovery_success_at_utc=last_success,
                window_days=settings.window_days,
                incremental_lookback_hours=settings.incremental_lookback_hours,
            )
            job_id = repo.create_job(
                conn,
                source_id=src.id,
                trigger_key=f"{trigger}:{src.adapter_name}",
                job_type=window.job_type,
                window_start_cst=window.start_cst,
                window_end_cst=window.end_cst,
                priority=src.priority,
            )
            if job_id:
                job_ids.append(job_id)

        if run_id:
            repo.finish_run(conn, run_id, len(job_ids))
        conn.commit()
    finally:
        conn.close()

    published = 0
    to_publish = job_ids + due_ids
    if to_publish and not dry_run:
        try:
            published = publish_jobs(to_publish, {"trigger": trigger}, queue)
        except Exception as exc:  # noqa: BLE001
            log.error("投递失败（任务已入库，可稍后重投）: %s", exc)
    elif dry_run:
        log.info("[dry-run] 将建批 %d 条，不投递", len(job_ids))

    summary = {"considered": considered, "created": len(job_ids),
               "retried": len(due_ids), "published": published}
    log.info("协调完成 %s", summary)
    return summary


def run_image_fetch(*, batch: int = 200) -> dict[str, int]:
    """跑一轮配图下载（`pending` → RustFS）。

    为什么挂在调度器而不是独立定时器：
    - 图片的「生产者」就是采集（spider 只收集 URL），挂在同一个循环里
      天然有先后关系 —— 先采文章、再取配图；
    - 少一个常驻进程（84 只有 3.8G 内存，能省就省）。

    为什么不用每轮都跑：图片是**重 IO**（每张几百 KB），
    10 分钟一轮太频繁 —— 新文章没那么多，白跑只是浪费带宽。
    默认每 6 轮（约 1 小时）跑一次。

    Args:
        batch: 单轮最多取几张。

    Returns:
        统计字典；任何异常都吞掉并返回空统计 —— **配图失败绝不能
        让调度器停摆**（那会导致所有来源都不再建批）。

    Note:
        这也是 `fetch-images` CLI 的内核，两处共用同一实现（唯一实现原则）。
    """
    try:
        from newsagg.config import get_settings
        from newsagg.db import Database
        from newsagg.imagestore import ImageStore
        from newsagg.repo import Repository

        settings = get_settings()
        db = Database(settings)
        store = ImageStore(settings, Repository(db), db)
        return store.process_pending(limit=batch)
    except Exception as exc:  # noqa: BLE001
        log.warning("配图下载失败（不影响采集调度）: %s", exc)
        return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="定时协调器：建批并投递采集任务")
    ap.add_argument("--force", action="store_true", help="忽略采集间隔，全部来源建批")
    ap.add_argument("--dry-run", action="store_true", help="只建批不投递")
    ap.add_argument("--loop", action="store_true", help="常驻，每 10 分钟跑一轮")
    ap.add_argument("--no-images", action="store_true", help="不自动跑配图下载")
    args = ap.parse_args(argv)

    bind_context(run_id=build_triggers())
    if not args.loop:
        run_coordinator(force=args.force, dry_run=args.dry_run)
        return 0

    import time

    from newsagg.config import get_settings

    settings = get_settings()
    #: 每多少轮跑一次配图下载（轮间隔 10 分钟 → 6 轮≈1 小时）
    images_every = max(1, getattr(settings, "images_every_rounds", 6))
    images_on = (not args.no_images) and getattr(settings, "images_auto", True)

    log.info("协调器常驻模式启动（间隔 10 分钟，配图自动下载=%s，每 %d 轮一次）",
             "开" if images_on else "关", images_every)
    rounds = 0
    try:
        while True:
            rounds += 1
            run_coordinator(force=args.force, dry_run=args.dry_run)

            # 配图下载：**放在协调之后** —— 先把新文章采进来，
            # 才可能有新的 pending 图。放在前面等于每轮都处理上一轮的存量。
            if images_on and rounds % images_every == 0:
                stats = run_image_fetch(
                    batch=getattr(settings, "images_batch", 200)
                )
                if stats.get("processed"):
                    log.info("配图下载：处理 %d（成功 %d / 失败 %d / 跳过 %d）",
                             stats["processed"], stats.get("downloaded", 0),
                             stats.get("failed", 0), stats.get("skipped", 0))

            time.sleep(600)
    except KeyboardInterrupt:
        log.info("协调器停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
