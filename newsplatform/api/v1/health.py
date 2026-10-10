"""健康检查（改造方案 §3.6 ``GET /api/v1/health``）。

语义分层（别把所有失败都当 500）：

- ``status=ok``       所有依赖正常；
- ``status=degraded`` 平台库正常，但采集库或 Redis 有问题——**仍返回 200**，
  因为「文章查不了」不等于「服务死了」，容器编排不该据此重启容器；
- ``status=down``     平台库不可用（鉴权/审计都做不了），返回 503。

这个区分是给 Docker healthcheck 与 Prometheus 用的：前者看 HTTP 状态码，
后者看 ``status`` 字段，两者各自拿到自己需要的粒度。
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.core.cache import get_cache
from newsplatform.core.config import get_platform_settings
from newsplatform.core.database import get_database
from newsplatform.core.logging import get_logger
from newsplatform.core.response import ok
from newsplatform.models.schemas import DependencyHealth, HealthStatus
from newsplatform.repositories.articles import ArticleRepository

log = get_logger("health")

__all__ = ["router", "STARTED_AT"]

router = APIRouter(tags=["health"])

#: 进程启动时刻（模块导入时间，足够近似；用于 uptime）
import time as _time  # noqa: E402  - 放在此处避免与上面 import 混淆

STARTED_AT = _time.time()


@router.get("/health", summary="健康检查", response_model=None)
async def health(response: Response) -> dict:
    """检查平台库 / 采集库 / Redis，并给出最近一篇文章的发布时间。"""
    settings = get_platform_settings()
    db = get_database()

    dependencies: list[DependencyHealth] = []

    platform_ok = await db.ping_platform()
    dependencies.append(DependencyHealth(
        name="platform_db", ok=platform_ok,
        detail="中台自有库（api_keys / audit_logs / stats_snapshots）" if platform_ok
        else "不可用：鉴权与审计将失败",
    ))

    collector_ok = await db.ping_collector()
    dependencies.append(DependencyHealth(
        name="collector_db", ok=collector_ok,
        detail="采集库（articles / sources）" if collector_ok else "不可用：文章与来源接口降级",
    ))

    redis_ok = get_cache().ping()
    dependencies.append(DependencyHealth(
        name="redis", ok=redis_ok,
        detail="缓存与限流可用" if redis_ok else "不可用：缓存/限流自动降级为放行",
    ))

    latest: str | None = None
    if collector_ok:
        try:
            async with db.collector_session() as session:  # type: AsyncSession
                value = await ArticleRepository(session).latest_published_at()
                latest = value.strftime("%Y-%m-%d %H:%M:%S") if value else None
        except Exception as exc:  # noqa: BLE001
            log.warning("读取最近发布时间失败: %s", exc)

    if not platform_ok:
        overall = "down"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif not (collector_ok and redis_ok):
        overall = "degraded"
    else:
        overall = "ok"

    payload = HealthStatus(
        status=overall,
        app=f"{settings.app_name} {settings.app_version}",
        version=settings.app_version,
        uptime_seconds=round(_time.time() - STARTED_AT, 1),
        dependencies=dependencies,
        latest_article_at=latest,
    )
    return ok(payload.model_dump(mode="json"))
