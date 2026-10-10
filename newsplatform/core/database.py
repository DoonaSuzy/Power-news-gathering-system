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

from typing import AsyncIterator

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
    "dispose_database",
]


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
    """采集库会话依赖（只读）。"""
    async with get_database().collector_session() as session:
        yield session
