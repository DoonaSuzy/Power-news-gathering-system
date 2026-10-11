"""统计接口（改造方案 §3.6 ``/api/v1/stats/*`` + §九.5「采集质量看板」）。

缓存的语义
----------
``overview`` / ``quality`` 走 Redis 短缓存（默认 60s），挡住看板的高频轮询；
其余维度聚合实时查。**任何一层缓存失效都不影响正确性**，只是慢一点。
写操作（``POST /stats/snapshot``）完成后会主动清缓存，让看板立刻反映变更。

采集质量看板的三个核心问题
--------------------------
1. **采集能力**：采了多少、覆盖多少省  → ``/overview``
2. **数据质量**：五类字段完整率        → ``/quality``
3. **系统健康**：任务成功率、漏斗       → ``/jobs``、``/funnel``
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from newsplatform.api.v1._audit import audit_write
from newsplatform.core.deps import CollectorSessionDep, PlatformSessionDep
from newsplatform.core.ratelimit import enforce_rate_limit
from newsplatform.core.response import ok
from newsplatform.core.security import AuthContext, require_scope
from newsplatform.services.stats_service import StatsService

__all__ = ["router"]

ReadDep = Depends(require_scope("stats:read"))
ReadAuthDep = Depends(require_scope("stats:read"))

router = APIRouter(
    prefix="/stats",
    tags=["stats"],
    dependencies=[ReadDep, Depends(enforce_rate_limit)],
)


@router.get("/overview", summary="全局概览")
async def stats_overview(session: CollectorSessionDep) -> dict:
    """文章总量、近 24h 增量、省份覆盖、来源启停、任务成功率。"""
    data = await StatsService(session).overview()
    return ok(data.model_dump(mode="json"))


@router.get("/quality", summary="字段完整率")
async def stats_quality(session: CollectorSessionDep) -> dict:
    """标题 / 正文 / 来源 / 发布时间 / 原文链接五类字段的完整率。"""
    data = await StatsService(session).quality()
    return ok(data.model_dump(mode="json"))


@router.get("/provinces", summary="按省份统计")
async def stats_by_province(session: CollectorSessionDep) -> dict:
    """省份维度聚合（含未覆盖省份），与 ``/provinces/coverage`` 同口径。"""
    rows = await StatsService(session).by_province()
    return ok({"items": rows, "total": len(rows)})


@router.get("/sources", summary="按来源统计")
async def stats_by_source(
    session: CollectorSessionDep,
    limit: Annotated[int, Query(ge=1, le=200, description="返回条数上限")] = 50,
) -> dict:
    """来源维度产出与字段缺漏（默认取产出最多的前 50 个）。"""
    rows = await StatsService(session).by_source(limit=limit)
    return ok({"items": [r.model_dump(mode="json") for r in rows], "total": len(rows)})


@router.get("/jobs", summary="任务健康度")
async def stats_jobs(session: CollectorSessionDep) -> dict:
    """按状态聚合的任务健康度（queued / running / succeeded / failed / retry_wait）。"""
    rows = await StatsService(session).job_health()
    return ok({"items": [r.model_dump(mode="json") for r in rows], "total": len(rows)})


@router.get("/runs", summary="最近协调器批次")
async def stats_runs(
    session: CollectorSessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> dict:
    """最近若干次调度建批记录（运维排查「为什么这个小时没采」用）。"""
    rows = await StatsService(session).recent_runs(limit=limit)
    return ok({"items": rows, "total": len(rows)})


@router.get("/funnel", summary="候选漏斗")
async def stats_funnel(session: CollectorSessionDep) -> dict:
    """候选清单漏斗：发现 → 成功 → 跳过 → 失败。"""
    return ok(await StatsService(session).task_funnel())


@router.post("/snapshot", summary="物化统计快照")
async def write_snapshot(
    request: Request,
    session: CollectorSessionDep,
    platform: PlatformSessionDep,
    auth: AuthContext = ReadAuthDep,
) -> dict:
    """把概览与质量按日物化到 ``stats_snapshots``，供报表免去每次全表聚合。

    注意两个会话的分工：``session``（采集库）只负责**读**聚合数据，
    ``platform``（中台库）负责**写**快照表。快照表在中台库，写错库会直接
    报 ``Table 'powernews.stats_snapshots' doesn't exist``。
    """
    service = StatsService(session, platform_session=platform)
    async with audit_write(platform, request, auth, detail="write stats snapshot"):
        result = await service.write_snapshot()
    service.invalidate()
    return ok(result)
