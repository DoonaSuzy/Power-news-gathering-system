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


@router.get(
    "/health",
    summary="健康检查",
    response_model=None,
    # 这是**唯一免鉴权**的接口，要覆盖掉 app 上的全局 security（见
    # ``main.py`` 的 ``_OPENAPI_SECURITY_EXTRA``），否则文档会给它也加一把锁，
    # 编排/巡检的人会误以为探活也要带 Token。
    #
    # 为什么用 ``openapi_extra`` 而不是 ``security=[]``：
    # ``APIRouter.get()`` **没有** ``security`` 参数（那是 ``APIRoute`` 的），
    # 传了会直接 ``TypeError``。而 FastAPI 生成 operation 时走的是
    # ``operation.setdefault("security", []).extend(...)``——键已存在就不再覆盖，
    # 所以这里预置一个空列表是安全且有效的。
    openapi_extra={"security": []},
)
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

    # 省级库是**可选**依赖：没配置就不出现在列表里，配了但连不上也不改变
    # 整体状态——它只影响 channel=province，不该让整个服务被判为 degraded。
    if db.province_configured:
        province_ok = await db.ping_province()
        dependencies.append(DependencyHealth(
            name="province_db",
            ok=province_ok,
            detail="省级新闻库（newsagg_platform，可选域）" if province_ok
            else "不可用：channel=province 将返回 503，channel=all 自动降级",
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


@router.get(
    "/health/ready",
    summary="就绪探针",
    response_model=None,
    openapi_extra={"security": []},
)
async def readiness(response: Response) -> dict:
    """就绪探针：只判「能不能对外提供文章查询」。

    与 ``/health`` 的区别（合并省级后端时补上）：

    - ``/health`` 给人看，列出全部依赖与最近文章时间，**总是 200**（除非平台库挂了）；
    - ``/health/ready`` 给编排看，**只用状态码说话**：平台库 / 采集库 / Redis
      任一不可用即 503。

    省级库**不计入**就绪判定：它是可选域，挂了只影响 ``channel=province``，
    因此不该让整个 Pod 被摘流量。
    """
    db = get_database()
    checks = {
        "platform_db": await db.ping_platform(),
        "collector_db": await db.ping_collector(),
        "redis": get_cache().ping(),
    }
    ready = all(checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ok({"ready": ready, "checks": checks})
