"""数据中台应用入口（FastAPI app 工厂 + lifespan）。

启动顺序（``lifespan``）
------------------------
1. 打印**脱敏后**的配置摘要——启动日志里一眼能看清连的是哪个库；
2. 探活平台库 / 采集库 / Redis，把连接问题暴露在**启动阶段**而不是首个请求；
3. ``shutdown`` 时释放两个连接池。

依赖探活**不阻塞启动**：采集库挂了平台仍要能提供鉴权与审计服务，
这一点与 ``/health`` 的 ``degraded`` 语义一致。

中间件装配顺序
--------------
``add_middleware`` 是**后进先出**（最后添加的最靠近请求），因此这里
先加 CORS、后加 ``RequestContextMiddleware``——让 request_id 最早生成，
CORS 预检响应也能带上 ``X-Request-Id``，便于前后端联调时对齐日志。

为什么不用 ``@app.on_event("startup")``？
    该 API 在 FastAPI 0.109+ 已弃用；``lifespan`` 上下文管理器是官方推荐写法，
    且天然保证「yield 前的资源一定在 yield 后被释放」。

本地启动::

    # 配置（口令只走环境变量或 .env）
    export POWERNEWS_PLATFORM_DATABASE_URL='mysql+aiomysql://powernews:***@127.0.0.1:3306/powernews_platform?charset=utf8mb4'
    export POWERNEWS_PLATFORM_COLLECTOR_DATABASE_URL='mysql+aiomysql://powernews:***@127.0.0.1:3306/powernews?charset=utf8mb4'

    python -m newsplatform.main            # 或 uvicorn newsplatform.main:app --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from newsplatform.api.v1 import api_router
from newsplatform.core.cache import get_cache
from newsplatform.core.config import PlatformSettings, get_platform_settings
from newsplatform.core.database import dispose_database, get_database
from newsplatform.core.exception_handlers import register_exception_handlers
from newsplatform.core.logging import get_logger
from newsplatform.core.metrics import instrument_app
from newsplatform.core.middleware import RequestContextMiddleware

__all__ = ["app", "create_app", "lifespan"]

log = get_logger("app")

#: OpenAPI 安全方案名（与下面 ``security`` 引用的是同一个）。
SECURITY_SCHEME_NAME = "BearerAuth"

#: 要注入到 ``components.securitySchemes`` 里的方案定义。
_SECURITY_SCHEME: dict[str, Any] = {
    "type": "http",
    "scheme": "bearer",
    "bearerFormat": "API Key",
    "description": (
        "填 API Key **本体**（`pn_` 开头），**不要**自己加 `Bearer ` 前缀——"
        "Swagger 会自动拼。\n\n"
        "没有 Key 时：用引导令牌调 `POST /api/v1/admin/api-keys` 创建一把 "
        "（引导令牌**仅当库内尚无任何 Key 时**可用，见 `.env` 的 "
        "`POWERNEWS_PLATFORM_BOOTSTRAP_ADMIN_TOKEN`）。"
    ),
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动探活 + 关闭释放连接池。"""
    settings = get_platform_settings()
    log.info("启动 %s %s", settings.app_name, settings.app_version)
    for key, value in settings.describe().items():
        log.info("  配置 %-20s = %s", key, value)

    if settings.secret_key == "change-me-in-production":
        log.warning("secret_key 仍是默认值，生产环境请通过 POWERNEWS_PLATFORM_SECRET_KEY 覆盖")

    db = get_database()
    platform_ok = await db.ping_platform()
    collector_ok = await db.ping_collector()
    redis_ok = get_cache().ping()
    log.info(
        "依赖探活: 平台库=%s 采集库=%s Redis=%s",
        "ok" if platform_ok else "FAIL",
        "ok" if collector_ok else "FAIL",
        "ok" if redis_ok else "FAIL",
    )
    if not platform_ok:
        log.warning("平台库不可用：鉴权与审计将失败，/health 会返回 503")
    if not collector_ok:
        log.warning("采集库不可用：文章与来源接口将降级（/health 返回 degraded）")

    try:
        yield
    finally:
        await dispose_database()
        log.info("应用已关闭")


def _attach_openapi_security(app: FastAPI) -> None:
    """把 Bearer 鉴权写进 OpenAPI，让 Swagger UI 出现 Authorize 按钮。

    **为什么需要它**：鉴权走的是自定义 Header 依赖（``core/security.py`` 里的
    ``authorization: Annotated[str | None, Header(...)]``），FastAPI **不会**
    把 ``Header`` 参数自动登记进 ``components.securitySchemes``。
    后果是 Swagger UI **不渲染 Authorize 按钮**——用户以为是自己没找到按钮，
    实际是文档里根本没声明，于是页面里试任何接口都只能拿到 401。

    为什么是覆写 ``app.openapi`` 而不是 ``FastAPI(openapi_extra=...)``：
    ``openapi_extra`` 是**路由装饰器**的参数，**不是** ``FastAPI.__init__`` 的。
    传给构造函数会被收进 ``app.extra`` 后**永远不被使用**——不报错、静默失效，
    和 ``/articles?q=`` 那次是同一类坑（传了没效果也不吭声）。

    幂等：schema 有缓存且用名字做守卫，重复调用不会重复注入。
    ``/health`` 免鉴权，由该路由的 ``openapi_extra={"security": []}`` 覆盖。
    """
    original_openapi = app.openapi

    def _openapi_with_security() -> dict[str, Any]:
        schema: dict[str, Any] = original_openapi()
        schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
        if SECURITY_SCHEME_NAME not in schemes:
            schemes[SECURITY_SCHEME_NAME] = dict(_SECURITY_SCHEME)
            schema.setdefault("security", [{SECURITY_SCHEME_NAME: []}])
        return schema

    app.openapi = _openapi_with_security  # type: ignore[method-assign]


def create_app(settings: PlatformSettings | None = None) -> FastAPI:
    """构造 FastAPI 应用（工厂函数便于测试里换配置重建实例）。"""
    cfg = settings or get_platform_settings()

    app = FastAPI(
        title=cfg.app_name,
        version=cfg.app_version,
        description=(
            "分布式电力新闻采集系统的数据中台：对外提供文章、来源、省份、"
            "统计四类只读接口，以及来源配置与 API Key 两类写接口。"
        ),
        docs_url=cfg.docs_url,
        redoc_url=cfg.redoc_url,
        openapi_url=cfg.openapi_url,
        lifespan=lifespan,
    )

    # 4) 把 Bearer 鉴权写进 OpenAPI（详见函数 docstring）
    _attach_openapi_security(app)

    # 1) CORS（外层）
    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-Id", "X-Export-Rows", "X-Export-Truncated"],
        )

    # 2) 请求上下文 / 访问日志（内层，后加即更靠近业务）
    app.add_middleware(RequestContextMiddleware)

    # 3) 三层异常处理器
    register_exception_handlers(app)

    # 4) 业务路由
    app.include_router(api_router, prefix=cfg.api_prefix)

    # 5) Prometheus 监控：ASGI 中间件 + ``GET /metrics``（幂等）。
    #    放在最后挂载，因此它在中间件栈最外层，耗时统计覆盖完整请求链路。
    instrument_app(app)

    @app.get("/", include_in_schema=False)
    async def index() -> dict:
        """根路径给一个指路牌，避免访问到 404 不知道从哪开始。"""
        return {
            "app": f"{cfg.app_name} {cfg.app_version}",
            "api_prefix": cfg.api_prefix,
            "docs": cfg.docs_url,
            "health": f"{cfg.api_prefix}/health",
        }

    return app


#: uvicorn / gunicorn 的目标对象：``newsplatform.main:app``
app = create_app()


def main() -> None:
    """``python -m newsplatform.main`` 直接起服务。"""
    import uvicorn

    cfg = get_platform_settings()
    uvicorn.run(
        "newsplatform.main:app",
        host=cfg.host,
        port=cfg.port,
        reload=cfg.debug,
        log_level="debug" if cfg.debug else "info",
    )


if __name__ == "__main__":
    main()
