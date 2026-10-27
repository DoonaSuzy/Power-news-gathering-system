"""站点级采集指标：把「每个源现在是什么状态」喂给 Prometheus。

为什么需要
----------
原来的 `/metrics` 只有 HTTP 层指标，加上几个**从未被调用**的 Gauge
（``record_article`` / ``set_queue_depth`` 只在测试里出现）。
结果就是：Prometheus 抓得到数据，但看不出「哪个站点停了」——
这正是「部署了 Prometheus 却没什么用」的原因。

本模块每 60 秒扫一次两个采集库，把每个源的产出与健康度写成带标签的 Gauge：

| 指标 | 含义 | 典型告警 |
| --- | --- | --- |
| ``powernews_source_enabled`` | 是否启用 | — |
| ``powernews_source_last_success_ts`` | 最近一次采集成功 | 超过 2 小时 |
| ``powernews_source_last_published_ts`` | 该源最新一篇的发布时间 | 超过 24 小时 |
| ``powernews_source_articles_24h`` | 近 24 小时入库数 | 启用但为 0 |
| ``powernews_source_failures_24h`` | 近 24 小时失败/重试任务数 | ≥3 |
| ``powernews_source_last_job_failed`` | 最近一次任务是否失败 | 1 |
| ``powernews_collector_lag_seconds`` | 最新文章距今秒数 | 超过 3 小时 |

设计取舍
--------
- **后台刷新 + 只读缓存**：Prometheus 的 collector 是同步接口，而我们是 async SQLAlchemy。
  与其在 scrape 里做异步桥接，不如后台定时刷新，scrape 只读内存。
- **60 秒**：够快（告警 for 5m 仍有效），又不会给库添压力。
- 刷新失败**不清空**旧值，只把 ``..._refresh_ts`` 停住——否则一次抖动会让所有告警齐响。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Gauge
from sqlalchemy import text

from newsplatform.core.logging import get_logger

__all__ = [
    "SOURCE_ENABLED",
    "SOURCE_LAST_SUCCESS_TS",
    "SOURCE_LAST_PUBLISHED_TS",
    "SOURCE_ARTICLES_24H",
    "SOURCE_FAILURES_24H",
    "SOURCE_LAST_JOB_FAILED",
    "SOURCE_INTERVAL_HOURS",
    "COLLECTOR_LAG_SECONDS",
    "SITE_METRICS_REFRESH_TS",
    "collect_site_rows",
    "apply_rows",
    "SiteMetricsRefresher",
]

log = get_logger("site_metrics")

_NS = "powernews"

#: 标签维度：源标识、数据域、省份、类别。
_LABELS = ["source", "channel", "province", "category"]

SOURCE_ENABLED = Gauge(
    f"{_NS}_source_enabled", "来源是否启用（1=启用）", _LABELS
)
SOURCE_LAST_SUCCESS_TS = Gauge(
    f"{_NS}_source_last_success_ts",
    "该源最近一次采集成功的时间戳（epoch 秒）",
    _LABELS,
)
SOURCE_LAST_PUBLISHED_TS = Gauge(
    f"{_NS}_source_last_published_ts",
    "该源已入库文章的最新发布时间（epoch 秒）",
    _LABELS,
)
SOURCE_ARTICLES_24H = Gauge(
    f"{_NS}_source_articles_24h", "该源近 24 小时入库文章数", _LABELS
)
SOURCE_FAILURES_24H = Gauge(
    f"{_NS}_source_failures_24h", "该源近 24 小时失败或待重试的任务数", _LABELS
)
SOURCE_LAST_JOB_FAILED = Gauge(
    f"{_NS}_source_last_job_failed", "该源最近一次任务是否失败（1=失败）", _LABELS
)
#: 采集间隔（小时）。告警阈值要按它分档：4h 的源在两次正常成功之间
#: 天然就会「2 小时没消息」，固定 2h 阈值必然误报。
SOURCE_INTERVAL_HOURS = Gauge(
    f"{_NS}_source_interval_hours", "该源的采集间隔（小时）", _LABELS
)
COLLECTOR_LAG_SECONDS = Gauge(
    f"{_NS}_collector_lag_seconds", "全库最新文章距当前时间的秒数"
)
SITE_METRICS_REFRESH_TS = Gauge(
    f"{_NS}_site_metrics_refresh_ts", "本组站点指标最后一次刷新成功的时间戳"
)

#: 电力采集库：一个源一行，附带近 24 小时产出与最近任务状态。
_SQL_POWER = text(
    """
    SELECT
        s.adapter_name                                        AS source,
        COALESCE(s.province_code, '')                         AS province,
        COALESCE(s.category, '')                              AS category,
        s.enabled                                             AS enabled,
        s.crawl_interval_hours                                AS interval_hours,
        s.last_discovery_success_at_utc                       AS last_success,
        (SELECT MAX(a.published_at_cst) FROM articles a
          WHERE a.source_id = s.id)                           AS last_published,
        (SELECT COUNT(*) FROM articles a
          WHERE a.source_id = s.id
            AND a.created_at >= UTC_TIMESTAMP() - INTERVAL 24 HOUR) AS articles_24h,
        (SELECT COUNT(*) FROM crawl_jobs j
          WHERE j.source_id = s.id
            AND j.created_at >= UTC_TIMESTAMP() - INTERVAL 24 HOUR
            AND j.status IN ('failed', 'retry_wait'))         AS failures_24h,
        (SELECT j.status FROM crawl_jobs j
          WHERE j.source_id = s.id ORDER BY j.id DESC LIMIT 1) AS last_status,
        (SELECT MAX(a.created_at) FROM articles a)            AS global_last_created
    FROM sources s
    """
)

#: 省级采集库：列名不同（media_name / verification_status / discovered_at_utc）。
_SQL_PROVINCE = text(
    """
    SELECT
        s.adapter_name                                        AS source,
        COALESCE(s.province_code, '')                         AS province,
        'province_media'                                      AS category,
        (s.verification_status = 'enabled')                   AS enabled,
        -- 省级库 sources 没有 crawl_interval_hours 列；照抄电力的列会让整页
        -- /metrics 报 Unknown column，所有站点告警一起消失。按历史日更口径给 24。
        24                                                    AS interval_hours,
        s.last_discovery_success_at_utc                       AS last_success,
        (SELECT MAX(a.published_at_cst) FROM articles a
          WHERE a.source_id = s.id)                           AS last_published,
        (SELECT COUNT(*) FROM articles a
          WHERE a.source_id = s.id
            AND a.discovered_at_utc >= UTC_TIMESTAMP() - INTERVAL 24 HOUR) AS articles_24h,
        (SELECT COUNT(*) FROM crawl_jobs j
          WHERE j.source_id = s.id
            AND j.created_at >= UTC_TIMESTAMP() - INTERVAL 24 HOUR
            AND j.status IN ('failed', 'retry_wait'))         AS failures_24h,
        (SELECT j.status FROM crawl_jobs j
          WHERE j.source_id = s.id ORDER BY j.id DESC LIMIT 1) AS last_status,
        (SELECT MAX(a.discovered_at_utc) FROM articles a)     AS global_last_created
    FROM sources s
    """
)

#: 视为失败的任务状态。
_FAILED_STATES = {"failed", "retry_wait"}


def _epoch(value: Any) -> float:
    """datetime / None → epoch 秒；无值记 0（Prometheus 里 0 表示「从没成功过」）。"""
    if isinstance(value, datetime):
        # 库里存的是 naive UTC（MySQL --default-time-zone=+00:00）。
        # 这里若按容器本地时区解释，全部 *_ts 指标会整体偏移（+08:00 就是 8 小时），
        # SourceStale / CollectorLagHigh 会因此集体误判。
        return value.replace(tzinfo=value.tzinfo or UTC).timestamp()
    return 0.0


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "enabled"}
    return False


async def collect_site_rows(session, *, channel: str, sql) -> list[dict[str, Any]]:
    """跑一次站点统计 SQL，返回统一形状的行。"""
    rows = (await session.execute(sql)).mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["channel"] = channel
        out.append(data)
    return out


def apply_rows(rows: list[dict[str, Any]]) -> float:
    """把行数据写进 Gauge，返回全库最新入库时间（epoch 秒）。

    先 ``clear()`` 再重设：站点被删/改名后，旧标签不会一直挂在指标里。
    """
    for gauge in (
        SOURCE_ENABLED,
        SOURCE_LAST_SUCCESS_TS,
        SOURCE_LAST_PUBLISHED_TS,
        SOURCE_ARTICLES_24H,
        SOURCE_FAILURES_24H,
        SOURCE_LAST_JOB_FAILED,
        SOURCE_INTERVAL_HOURS,
    ):
        gauge.clear()

    global_last = 0.0
    for row in rows:
        labels = {
            "source": str(row.get("source") or ""),
            "channel": str(row.get("channel") or ""),
            "province": str(row.get("province") or ""),
            "category": str(row.get("category") or ""),
        }
        SOURCE_ENABLED.labels(**labels).set(1.0 if _as_bool(row.get("enabled")) else 0.0)
        SOURCE_LAST_SUCCESS_TS.labels(**labels).set(_epoch(row.get("last_success")))
        SOURCE_LAST_PUBLISHED_TS.labels(**labels).set(_epoch(row.get("last_published")))
        SOURCE_ARTICLES_24H.labels(**labels).set(float(row.get("articles_24h") or 0))
        SOURCE_FAILURES_24H.labels(**labels).set(float(row.get("failures_24h") or 0))
        SOURCE_LAST_JOB_FAILED.labels(**labels).set(
            1.0 if str(row.get("last_status") or "") in _FAILED_STATES else 0.0
        )
        # 缺值时按 24h 保守处理：宁可晚报警，也不要因为指标缺项把阈值算成 0
        SOURCE_INTERVAL_HOURS.labels(**labels).set(
            float(row.get("interval_hours") or 24)
        )
        global_last = max(global_last, _epoch(row.get("global_last_created")))
    return global_last


class SiteMetricsRefresher:
    """后台定时刷新站点指标（默认 60 秒一次）。"""

    def __init__(self, database, *, interval_seconds: int = 60) -> None:
        self._db = database
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None

    async def refresh_once(self) -> int:
        """刷新一次，返回写入的站点数。异常向上抛，由调用方决定怎么记。"""
        rows: list[dict[str, Any]] = []
        async with self._db.collector_session() as session:
            rows.extend(await collect_site_rows(session, channel="power", sql=_SQL_POWER))
        if self._db.province_configured:
            async with self._db.province_session() as session:
                rows.extend(
                    await collect_site_rows(session, channel="province", sql=_SQL_PROVINCE)
                )

        global_last = apply_rows(rows)
        if global_last:
            COLLECTOR_LAG_SECONDS.set(max(0.0, time.time() - global_last))
        SITE_METRICS_REFRESH_TS.set(time.time())
        return len(rows)

    async def _loop(self) -> None:
        while True:
            try:
                count = await self.refresh_once()
                log.debug("站点指标已刷新：%d 个源", count)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台任务不能因为一次失败就退出
                log.warning("站点指标刷新失败：%s", exc)
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="site-metrics")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
