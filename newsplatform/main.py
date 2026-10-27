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
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from newsplatform.api.v1 import api_router
from newsplatform.core.cache import get_cache
from newsplatform.core.config import PlatformSettings, get_platform_settings
from newsplatform.core.database import dispose_database, get_database
from newsplatform.core.exception_handlers import register_exception_handlers
from newsplatform.core.logging import get_logger
from newsplatform.core.metrics import instrument_app
from newsplatform.core.middleware import RequestContextMiddleware
from newsplatform.services.notice_service import get_notice_service

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

    # 公告栏预热 + 定时刷新。
    # 预热是**后台任务**，不阻塞启动：一次 LLM 调用要 4~15 秒，
    # 阻塞会让所有端点一起等。首访可能拿到 ready=false，那好过全体等 15 秒。
    notice_service = get_notice_service()
    await notice_service.start_scheduler()

    try:
        yield
    finally:
        await notice_service.stop_scheduler()
        await dispose_database()
        log.info("应用已关闭")


class _SPAStaticFiles(StaticFiles):
    """支持 SPA 路由回退的静态文件服务。

    ## 为什么需要它

    Vue Router 用 **history 模式**时，浏览器地址是 ``/article/123`` 这种
    「看起来像真实路径」的形式。但磁盘上并没有 ``article/123`` 这个文件 ——
    `StaticFiles` 默认直接 **404**。

    正确做法是**回退到 index.html**，让前端路由去解析地址。

    ## 只对 404 回退

    其他状态（403 目录遍历、416 等）**照常抛出** —— 一律回退会把真实的
    权限/路径问题掩盖成「页面能打开但内容不对」，更难排查。

    ## ⚠️ 必须捕获 **starlette** 的 HTTPException

    `StaticFiles` 内部抛的是 `starlette.exceptions.HTTPException`。
    **FastAPI 的 `HTTPException` 是它的子类**，所以
    `except (fastapi.HTTPException)` **捕不到父类异常** —— 异常会冒泡给
    中台的统一异常处理器，返回业务错误体 `{"code":3001,...}` 而不是回退。
    这个坑很隐蔽：日志里看不到报错，只是「SPA 路由打不开」。
    """

    async def get_response(self, path: str, scope):  # noqa: ANN201
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


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
            "分布式新闻采集系统的数据中台：电力行业新闻（60 源）+ 省级官方新闻（34 源）"
            "+ 行情与气象时序数据。\n\n"
            "## 数据域（channel）\n\n"
            "**省级新闻没有独立的接口路径**，它和电力新闻共用同一批端点，"
            "用 `channel` 参数区分：\n\n"
            "| channel | 数据源 | 内容 |\n"
            "| --- | --- | --- |\n"
            "| `power` | `powernews` 库 | 电网公司 / 能源主管部门 / 行业媒体，60 源 |\n"
            "| `province` | `newsagg` 库 | 省级党报与广电官方新闻，34 源 |\n"
            "| `all`（默认） | 两者合并 | 94 源 |\n\n"
            "**支持 `channel` 的端点**：`/articles`（列表 / 计数 / 导出 / 详情）、"
            "`/sources`（列表）、`/provinces`（字典 / 覆盖度 / 某省文章）、`/channels`。\n\n"
            "**为什么不做成两套路径**：`/articles`、`/sources`、`/provinces`、`/health` "
            "等 6 条路径在两套后端里同名，挂在一起会互相覆盖且不报错；"
            "而且两个库的 id 各自自增，同名 id 不是同一条数据。\n\n"
            "## 先看这个\n\n"
            "`GET /api/v1/channels` 返回每个数据域的来源数、文章数与最新时间。"
            "不传 `channel` 时默认 `all`；省级库未接入时自动降级为 `power`，"
            "响应里的 `data.channels` 会如实标注本次实际查询了哪些域。"
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

    # 6) 前端静态托管（构建产物由 compose 挂载到 /app/web/dist）
    #
    # 为什么由中台托管而不是单独起 Nginx：
    # - **同域** → 前端 `/api/v1/...` 直接命中同源路由，天然免 CORS；
    # - **不引新镜像** → 84 拉 Docker Hub 极慢（实测 nginx:alpine 拉了 3 分钟没下来），
    #   而中台容器本就在跑，挂个目录即可；
    # - **一个入口**：用户只记 8080 一个端口。
    #
    # ⚠️ 必须在**所有 API 路由之后** mount("/")，否则会把 /api 一起吞掉。
    _dist = Path("/app/web/dist")
    if (_dist / "index.html").is_file():
        app.mount("/", _SPAStaticFiles(directory=str(_dist), html=True), name="web")
    else:
        @app.get("/", include_in_schema=False)
        async def index() -> dict:
            """前端产物未挂载时的指路牌（避免访问 / 得到 404 不知道从哪开始）。"""
            return {
                "app": f"{cfg.app_name} {cfg.app_version}",
                "api_prefix": cfg.api_prefix,
                "docs": cfg.docs_url,
                "health": f"{cfg.api_prefix}/health",
                "web": "前端产物未挂载（compose 需把 web/dist 挂到 /app/web/dist）",
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
