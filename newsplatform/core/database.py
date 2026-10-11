"""异步数据库层（改造方案 §2.3 / §4.1：SQLAlchemy 2.0 async）。

两个引擎，职责严格分开
----------------------
- ``platform_engine`` → ``powernews_platform`` 库。中台**自己**的状态：
  API Key、审计日志、统计快照。可读写。
- ``collector_engine`` → ``powernews`` 库。采集层的产物：文章、来源、省份、任务。
  中台只读——采集层的写入方永远是 ``newsagg``，避免两套代码争抢同一批行。

为什么不用一个引擎切库？
    两个库的读写权限、连接池规格、可用性 SLA 都不同：采集库挂了应当只退化为
    「文章接口不可用」，而中台的鉴权/审计仍要活着。物理隔离才能做到这一点。

连接池参数（``db_pool_size`` / ``db_max_overflow`` / ``db_pool_recycle``）来自
``core/config.py``；``pool_recycle`` 默认 1800 秒，用于规避 MySQL 的
``wait_timeout``（默认 8 小时后断连）导致的 "MySQL server has gone away"。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from newsplatform.core.config import PlatformSettings, get_platform_settings
from newsplatform.core.logging import get_logger

log = get_logger("db")

__all__ = [
    "PlatformBase",
    "CollectorBase",
    "AsyncDatabase",
    "get_database",
    "get_platform_session",
    "get_collector_session",
    "get_collector_write_session",
    "dispose_database",
]


# ======================================================================
# 兼容补丁：SQLAlchemy aiomysql 方言的 do_ping 参数 bug
# ======================================================================
# 现象
# ----
# 只要引擎带 ``pool_pre_ping=True``，**第二次**从连接池借出同一连接时必然抛：
#
#     TypeError: AsyncAdapt_aiomysql_connection.ping()
#                missing 1 required positional argument: 'reconnect'
#
# 首次连接不会触发（新建连接不做 pre-ping），因此表现为「启动探活全 ok，
# 一有请求就 500」——极具迷惑性。
#
# 根因
# ----
# 1. ``MySQLDialect_aiomysql`` 自己没有实现 ``do_ping``，沿用了
#    ``MySQLDialect_pymysql.do_ping``；
# 2. 而 pymysql 那版 ``do_ping`` 靠 ``_send_false_to_ping`` 决定调用形态：
#        dbapi_connection.ping(False)   # 判断为 True
#        dbapi_connection.ping()        # 判断为 False
# 3. 该判断的实现是**去 import 本机的 pymysql**，再看 py 签名里的 reconnect 默认值——
#    即使当前方言是 aiomysql 也在看 pymysql，属于上游实现瑕疵；
# 4. 本机 PyMySQL 的签名是 ``ping(self, reconnect=False)``（默认 False），
#    判定结果为 ``False`` → 走无参分支 ``ping()``；
# 5. 但 aiomysql 适配器的签名是 ``ping(self, reconnect: bool)``——**必传**，
#    于是 TypeError。
#
# 为什么不能靠升级/降级依赖绕过
# ----------------------------
# 这个 bug 由「本机 PyMySQL 的默认值」和「aiomysql 适配器的签名」共同决定，
# 与 SQLAlchemy 版本无必然关系（实测 2.0.36 / 2.0.43 均复现）。
# 换驱动、关掉 pre_ping 都是可用但不划算的退让——前者改动面大，后者丢掉
# stale connection 防护。所以这里在本项目内**显式覆盖** ``do_ping``，
# 让行为不再取决于第三方包的版本组合。
#
# 语义说明
# --------
# ``reconnect=False`` = 「只探测、不自动重连」。这正是 pre-ping 想要的：
# 连接已死时报错，交由连接池把该连接作废并重建，而不是让驱动悄悄重连后
# 把陈旧连接的会话状态（事务、临时表）带进业务逻辑。
def _aiomysql_do_ping(self, dbapi_connection) -> bool:  # noqa: ANN001
    """aiomysql 方言的 pre-ping 实现（按适配器的 ``ping(reconnect)`` 签名调用）。"""
    ping = dbapi_connection.ping
    try:
        ping(False)
    except TypeError:
        # 兜底：万一底层 DBAPI 的 ping 不接受参数（同步驱动场景），退回无参调用
        ping()
    return True


def _install_aiomysql_ping_patch() -> None:
    """给 ``MySQLDialect_aiomysql`` 装补丁（幂等）。"""
    from sqlalchemy.dialects.mysql.aiomysql import MySQLDialect_aiomysql

    if getattr(MySQLDialect_aiomysql, "_powernews_ping_patched", False):
        return
    MySQLDialect_aiomysql.do_ping = _aiomysql_do_ping  # type: ignore[method-assign]
    MySQLDialect_aiomysql._powernews_ping_patched = True  # type: ignore[attr-defined]
    log.debug("已应用 aiomysql do_ping 兼容补丁")


_install_aiomysql_ping_patch()


class PlatformBase(DeclarativeBase):
    """中台自有库的 ORM 基类。"""


class CollectorBase(DeclarativeBase):
    """采集库的只读 ORM 基类（映射既有表，不参与建表）。"""


class AsyncDatabase:
    """两个异步引擎的生命周期容器。"""

    def __init__(self, settings: PlatformSettings | None = None) -> None:
        self.settings = settings or get_platform_settings()
        self._platform_engine: AsyncEngine | None = None
        self._collector_engine: AsyncEngine | None = None
        self._platform_session: async_sessionmaker[AsyncSession] | None = None
        self._collector_session: async_sessionmaker[AsyncSession] | None = None

    # ---------------- 引擎 ----------------
    def _engine_kwargs(self) -> dict[str, object]:
        return {
            "pool_size": self.settings.db_pool_size,
            "max_overflow": self.settings.db_max_overflow,
            "pool_recycle": self.settings.db_pool_recycle,
            "pool_pre_ping": True,   # 取连接前先探活，规避 stale connection
            "echo": self.settings.db_echo,
        }

    @property
    def platform_engine(self) -> AsyncEngine:
        if self._platform_engine is None:
            self._platform_engine = create_async_engine(
                self.settings.database_url, **self._engine_kwargs()
            )
            log.info("平台库引擎已创建")
        return self._platform_engine

    @property
    def collector_engine(self) -> AsyncEngine:
        if self._collector_engine is None:
            self._collector_engine = create_async_engine(
                self.settings.collector_database_url, **self._engine_kwargs()
            )
            log.info("采集库引擎已创建（只读）")
        return self._collector_engine

    # ---------------- 会话 ----------------
    @property
    def platform_session(self) -> async_sessionmaker[AsyncSession]:
        if self._platform_session is None:
            self._platform_session = async_sessionmaker(
                self.platform_engine, expire_on_commit=False, class_=AsyncSession
            )
        return self._platform_session

    @property
    def collector_session(self) -> async_sessionmaker[AsyncSession]:
        if self._collector_session is None:
            self._collector_session = async_sessionmaker(
                self.collector_engine, expire_on_commit=False, class_=AsyncSession
            )
        return self._collector_session

    # ---------------- 健康检查 ----------------
    async def ping_platform(self) -> bool:
        """平台库连通性。"""
        try:
            from sqlalchemy import text

            async with self.platform_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("平台库不可用: %s", exc)
            return False

    async def ping_collector(self) -> bool:
        """采集库连通性。"""
        try:
            from sqlalchemy import text

            async with self.collector_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("采集库不可用: %s", exc)
            return False

    # ---------------- 关闭 ----------------
    async def dispose(self) -> None:
        """释放全部连接池（应用 shutdown 调用）。"""
        for engine in (self._platform_engine, self._collector_engine):
            if engine is not None:
                await engine.dispose()
        self._platform_engine = None
        self._collector_engine = None
        self._platform_session = None
        self._collector_session = None
        log.info("数据库连接池已释放")


# ----------------------------------------------------------------------
# 应用级单例（由 main.py 的 lifespan 建立与销毁）
# ----------------------------------------------------------------------
_database: AsyncDatabase | None = None


def get_database() -> AsyncDatabase:
    """取应用级 :class:`AsyncDatabase`（惰性创建）。"""
    global _database
    if _database is None:
        _database = AsyncDatabase()
    return _database


async def dispose_database() -> None:
    """关闭并清空全局实例。"""
    global _database
    if _database is not None:
        await _database.dispose()
        _database = None


# ----------------------------------------------------------------------
# FastAPI 依赖
# ----------------------------------------------------------------------
async def get_platform_session() -> AsyncIterator[AsyncSession]:
    """中台库会话依赖（可写，退出时自动 rollback 未提交事务）。"""
    async with get_database().platform_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_collector_session() -> AsyncIterator[AsyncSession]:
    """采集库会话依赖（只读）。

    ⚠️ 退出时**不提交**。拿它做写操作会被静默回滚——接口照样返回 200 甚至
    带着自增 id，库里却什么都没多（本项目实测：返回 ``id=46``，表里仍是 45 行）。
    要写采集库请用 :func:`get_collector_write_session`。
    """
    async with get_database().collector_session() as session:
        yield session


async def get_collector_write_session() -> AsyncIterator[AsyncSession]:
    """采集库**写**会话依赖：退出时提交，异常时回滚。

    唯一的合法用途是中台修改 ``sources`` 的**配置列**（``enabled`` /
    ``list_url`` / ``crawl_interval_hours`` / ``priority`` / ``note`` / ``status``，
    白名单见 ``SourceRepository.UPDATABLE_FIELDS``）。

    「中台对采集库只读」是我们给两个库划的边界，但来源配置是个**有意的例外**：
    采集层负责产出数据，配置的增删改查交给中台，否则运维改一个 ``enabled``
    还得登服务器跑 CLI。既然有这个例外，就把它显式化成独立依赖——
    这样「这个路由会改采集库」在函数签名里就能看出来，
    而不是从只读会话里偷偷写（那正是原先 5 个写接口全部静默失效的原因）。

    产出行（``articles`` / ``crawl_jobs`` / ``article_tasks`` / ``lease_*``）
    仍然永远只由采集层写。
    """
    async with get_database().collector_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
