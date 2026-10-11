"""统计服务（改造方案 §3.6 ``/api/v1/stats/*`` + §九.5「采集质量看板」）。

两级缓存策略
------------
1. **Redis 短缓存**（``cache_ttl_seconds``，默认 60s）：挡掉看板高频轮询；
2. **库内按日快照**（``stats_snapshots``）：报表类查询用，免全表聚合。

缓存读不到就回落到实时聚合——**任何一层失效都不影响正确性**，只是慢一点。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.core.cache import Cache, get_cache
from newsplatform.models.schemas import (
    JobHealthItem,
    SourceStatsItem,
    StatsOverview,
    StatsQuality,
)
from newsplatform.repositories.admin import SnapshotRepository
from newsplatform.repositories.stats import StatsRepository

__all__ = ["StatsService"]


class StatsService:
    """采集统计聚合。

    为什么需要**两个**会话
    ----------------------
    统计读的是采集库（``powernews``：articles / sources / crawl_jobs），
    但 ``stats_snapshots`` 这张快照表属于中台库（``powernews_platform``）。

    早先的实现只注入了一个会话并把它同时用于读写，结果 ``POST /stats/snapshot``
    实际执行的是：

        INSERT INTO powernews.stats_snapshots ...   -- 表在中台库，这里不存在！

    报错是 ``(1146, "Table 'powernews.stats_snapshots' doesn't exist")``——
    错误信息里带着库名，却很容易被当成「表没建」去排查 DDL，
    而真正的问题是**连接用错了库**。

    所以这里把两个会话显式分开：``session`` 只读采集库，
    ``platform_session`` 只用于落快照；后者缺失时**直接抛错**，
    不做 ``or session`` 之类的静默回落——那正是当初埋下这个 bug 的写法。
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        platform_session: AsyncSession | None = None,
        cache: Cache | None = None,
    ) -> None:
        self.session = session
        self.platform_session = platform_session
        self.repo = StatsRepository(session)
        self.cache = cache or get_cache()

    @property
    def snapshots(self) -> SnapshotRepository:
        """快照仓储（中台库）。没有中台库会话时立即失败，避免写错库。"""
        if self.platform_session is None:
            raise RuntimeError(
                "缺少中台库会话：stats_snapshots 属于 powernews_platform，"
                "不能用采集库会话写入。构造 StatsService 时请传 platform_session=..."
            )
        return SnapshotRepository(self.platform_session)

    # ---------------- 概览 ----------------
    async def overview(self) -> StatsOverview:
        """全局概览（带 Redis 缓存）。"""
        cached = self.cache.get_json("stats:overview")
        if cached:
            return StatsOverview(**cached)
        data = await self.repo.overview()
        self.cache.set_json("stats:overview", data)
        return StatsOverview(**data)

    # ---------------- 质量 ----------------
    async def quality(self) -> StatsQuality:
        """字段完整率（带 Redis 缓存）。"""
        cached = self.cache.get_json("stats:quality")
        if cached:
            return StatsQuality(**cached)
        data = await self.repo.quality()
        self.cache.set_json("stats:quality", data)
        return StatsQuality(**data)

    # ---------------- 维度聚合 ----------------
    async def by_province(self) -> list[dict[str, Any]]:
        """按省份统计（含未覆盖省份）。

        复用 ``ProvinceRepository.coverage()`` 而不是另写一份 SQL——
        同一口径写在两处必然漂移，这是旧项目「CLI 数字与看板数字对不上」的根因。
        """
        from newsplatform.repositories.sources import ProvinceRepository

        return await ProvinceRepository(self.session).coverage()

    async def by_source(self, *, limit: int = 50) -> list[SourceStatsItem]:
        """按来源统计。"""
        rows = await self.repo.by_source(limit=limit)
        return [SourceStatsItem(**row) for row in rows]

    async def job_health(self) -> list[JobHealthItem]:
        """任务健康度。"""
        rows = await self.repo.job_health()
        return [JobHealthItem(**row) for row in rows]

    async def recent_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """最近任务。"""
        return await self.repo.recent_jobs(limit=limit)

    async def recent_runs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """最近协调器批次。"""
        return await self.repo.recent_runs(limit=limit)

    async def task_funnel(self) -> dict[str, int]:
        """候选漏斗。"""
        return await self.repo.task_funnel()

    # ---------------- 快照 ----------------
    async def write_snapshot(self, *, snapshot_date: date | None = None) -> dict[str, Any]:
        """把概览与质量物化一份到 ``stats_snapshots``（由定时任务调用）。"""
        day = snapshot_date or datetime.now(UTC).date()
        overview = (await self.overview()).model_dump(mode="json")
        quality = (await self.quality()).model_dump(mode="json")
        await self.snapshots.upsert(
            snapshot_date=day, scope="overview", scope_key="", payload=overview
        )
        await self.snapshots.upsert(
            snapshot_date=day, scope="quality", scope_key="", payload=quality
        )
        return {"snapshot_date": day.isoformat(), "overview": overview, "quality": quality}

    async def cached_overview_snapshot(self) -> dict[str, Any] | None:
        """读最近一份概览快照。"""
        return await self.snapshots.latest(scope="overview")

    def invalidate(self) -> None:
        """清掉统计缓存（写操作后调用，让看板立刻反映变更）。"""
        self.cache.delete("stats:overview", "stats:quality")
