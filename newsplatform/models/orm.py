"""ORM 模型（改造方案 §2.3 models/orm.py）。

两个 Base 对应两个库
-------------------
``PlatformBase`` → ``powernews_platform``（中台自有，可写）
    ``api_keys`` / ``audit_logs`` / ``stats_snapshots``

``CollectorBase`` → ``powernews``（采集层产物，**只读**）
    ``provinces`` / ``sources`` / ``articles`` / ``crawl_jobs`` / ``article_tasks`` / ``crawl_runs``

⚠️ 采集库模型的列名必须与 ``sql/schema.sql`` 严格一致 —— 那张 DDL 由
``powernews init-db`` 执行，采集层（PyMySQL）和中台（SQLAlchemy）读的是同一批表。
改采集库结构时，``sql/schema.sql``、``newsagg/repo.py``、本文件三处必须同步改，
回归测试见 ``tests/test_schema_sync.py``。

时间字段约定（同 ``sql/schema.sql``）
    ``*_cst`` = 北京时间 naive；``*_utc`` = UTC naive。
    ORM 层不做时区转换，转换发生在 ``services`` 层的序列化里。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from newsplatform.core.database import CollectorBase, PlatformBase

__all__ = [
    "ApiKey",
    "AuditLog",
    "StatsSnapshot",
    "Province",
    "Source",
    "Article",
    "ArticleTask",
    "CrawlJob",
    "CrawlRun",
]


# ======================================================================
# 中台自有库（powernews_platform）
# ======================================================================
class ApiKey(PlatformBase):
    """API Key（管理接口鉴权，改造方案 §6.3「认证使用 Bearer Token」）。

    只存 ``sha256(raw_key)``；明文只在创建时返回一次，之后无法复原——
    数据库被读走也无法直接冒用。
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="用途名称")
    key_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="sha256(明文 Key) 十六进制"
    )
    #: 逗号分隔的权限范围：articles:read,sources:write,admin
    scopes: Mapped[str] = mapped_column(String(255), nullable=False, default="articles:read")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        UniqueConstraint("key_hash", name="uk_api_keys_hash"),
        Index("idx_api_keys_enabled", "enabled"),
    )

    def scope_set(self) -> set[str]:
        """把 ``scopes`` 拆成集合。"""
        return {s.strip() for s in (self.scopes or "").split(",") if s.strip()}


class AuditLog(PlatformBase):
    """写操作审计日志（改造方案 §6.3「所有写操作记录审计日志」）。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(CHAR(32), nullable=False, default="")
    api_key_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    api_key_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    client_ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_audit_created", "created_at"),
        Index("idx_audit_key", "api_key_id"),
        Index("idx_audit_path", "path"),
    )


class StatsSnapshot(PlatformBase):
    """统计快照（改造方案 §6.1「增加缓存层」/ §九.5「采集质量看板」）。

    把 ``overview`` / ``by_province`` 等聚合结果按日物化一份，
    供报表与看板免去每次请求都扫全表。
    """

    __tablename__ = "stats_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_date: Mapped[date] = mapped_column(nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, comment="overview/province/source/quality")
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        UniqueConstraint("snapshot_date", "scope", "scope_key", name="uk_snapshot"),
        Index("idx_snapshot_scope", "scope", "snapshot_date"),
    )


# ======================================================================
# 采集库（powernews）—— 只读映射
# ======================================================================
class Province(CollectorBase):
    """省级行政区字典（34 条，含港澳台）。"""

    __tablename__ = "provinces"

    code: Mapped[str] = mapped_column(CHAR(6), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    short_name: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    region: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    is_special: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Source(CollectorBase):
    """来源清单（39 条）。中台可改 ``enabled`` / ``list_url`` / ``note`` 等配置列。"""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    adapter_name: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    province_code: Mapped[str | None] = mapped_column(CHAR(6), nullable=True)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    news_path: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    list_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="sgcc")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    crawl_interval_hours: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    note: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    cooldown_until_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_discovery_success_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Article(CollectorBase):
    """文章主表（五类字段：标题 / 正文 / 发布时间 / 原文来源 / 原文链接）。"""

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    url_hash: Mapped[str] = mapped_column(CHAR(40), nullable=False)
    url: Mapped[str] = mapped_column(String(768), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    published_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at_cst: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    time_precision: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    content_hash: Mapped[str] = mapped_column(CHAR(40), nullable=False, default="")
    quality_flags: Mapped[str] = mapped_column(String(512), nullable=False, default="[]")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ArticleTask(CollectorBase):
    """列表页候选处理台账。"""

    __tablename__ = "article_tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discovered_url_hash: Mapped[str] = mapped_column(CHAR(40), nullable=False)
    url: Mapped[str] = mapped_column(String(768), nullable=False)
    list_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    failure_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CrawlJob(CollectorBase):
    """采集任务（单来源单窗口）。中台只读，供任务健康度看板使用。"""

    __tablename__ = "crawl_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_key: Mapped[str] = mapped_column(String(128), nullable=False)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    window_start_cst: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end_cst: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    saved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stop_reason: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    retry_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CrawlRun(CollectorBase):
    """协调器批次。"""

    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trigger_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    job_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
