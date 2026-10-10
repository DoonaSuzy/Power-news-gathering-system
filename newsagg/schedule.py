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

from newsagg.cleaning import now_cst, now_utc_naive, to_utc_naive
from newsagg.config import get_settings
from newsagg.db import Database
from newsagg.logutil import bind_context, get_logger
from newsagg.queue import StreamQueue
from newsagg.repo import Repository
from newsagg.timewindow import JOB_FULL_RESCAN, JOB_INCREMENTAL, build_window
from newsagg.timewindow import window_key

log = get_logger("schedule")

__all__ = ["run_coordinator", "build_triggers", "publish_jobs", "main"]


def build_triggers(now_utc: datetime | None = None) -> str:
    """生成本轮批次的 trigger_key（按小时粒度，天然幂等）。"""
    now_utc = now_utc or now_utc_naive()
    cst = now_cst() if now_utc is None else None
    if cst is None:
        from newsagg.cleaning import to_cst_naive

        cst = to_cst_naive(now_utc)
    return f"hourly:{cst:%Y%m%d%H}"


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

    conn = db.connect()
    job_ids: list[int] = []
    considered = 0
    try:
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
    if job_ids and not dry_run:
        try:
            published = publish_jobs(job_ids, {"trigger": trigger}, queue)
        except Exception as exc:  # noqa: BLE001
            log.error("投递失败（任务已入库，可稍后重投）: %s", exc)
    elif dry_run:
        log.info("[dry-run] 将建批 %d 条，不投递", len(job_ids))

    summary = {"considered": considered, "created": len(job_ids), "published": published}
    log.info("协调完成 %s", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="定时协调器：建批并投递采集任务")
    ap.add_argument("--force", action="store_true", help="忽略采集间隔，全部来源建批")
    ap.add_argument("--dry-run", action="store_true", help="只建批不投递")
    ap.add_argument("--loop", action="store_true", help="常驻，每 10 分钟跑一轮")
    args = ap.parse_args(argv)

    bind_context(run_id=build_triggers())
    if not args.loop:
        run_coordinator(force=args.force, dry_run=args.dry_run)
        return 0

    import time

    log.info("协调器常驻模式启动（间隔 10 分钟）")
    try:
        while True:
            run_coordinator(force=args.force, dry_run=args.dry_run)
            time.sleep(600)
    except KeyboardInterrupt:
        log.info("协调器停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
