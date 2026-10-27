"""Pydantic 请求/响应模型（改造方案 §2.3 models/schemas.py）。

约定
----
- 时间统一序列化成 ``"YYYY-MM-DD HH:MM:SS"`` 字符串：
  ``*_cst`` 是北京时间（业务展示口径），``*_utc`` 是 UTC（排障口径），
  字段名已经写明时区，字符串里就不再带偏移量，避免下游误当本地时间二次转换。
- 列表项**不含正文全文**（只给 ``excerpt`` 摘要），正文走 ``GET /articles/{id}``。
- 写请求用独立模型（``SourceCreate`` / ``SourceUpdate``），
  不让 ``id`` / ``created_at`` 这类服务端字段出现在入参里。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

__all__ = [
    "ArticleListItem",
    "ArticleDetail",
    "ArticlePage",
    "SourceItem",
    "SourceCreate",
    "SourceUpdate",
    "SourcePage",
    "ProvinceItem",
    "ProvinceCoverageItem",
    "StatsOverview",
    "StatsQuality",
    "SourceStatsItem",
    "JobHealthItem",
    "ApiKeyItem",
    "ApiKeyCreate",
    "ApiKeyCreated",
    "AuditLogItem",
    "HealthStatus",
]

_DT_FMT = "%Y-%m-%d %H:%M:%S"

T = TypeVar("T")


# ======================================================================
# 通用
# ======================================================================
class _Base(BaseModel):
    """统一配置：允许从 ORM / dict 构造。"""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    @field_serializer("*", when_used="json")
    def _serialize_dt(self, value: Any) -> Any:  # pragma: no cover - 由 pydantic 调用
        if isinstance(value, datetime):
            return value.strftime(_DT_FMT)
        if isinstance(value, date):
            return value.isoformat()
        return value


class PageMeta(BaseModel):
    """分页元信息。"""

    total: int = Field(0, description="命中总条数")
    page: int = Field(1, description="当前页码")
    page_size: int = Field(20, description="每页条数")
    pages: int = Field(0, description="总页数")


class ArticlePage(PageMeta):
    items: list[ArticleListItem] = Field(default_factory=list)
    #: 本次实际查询的数据域。``channel=all`` 但省级库未接入时会退化成
    #: ``["power"]``——把这个事实告诉客户端，比让它自己猜「为什么少了一半」好。
    channels: list[str] = Field(default_factory=lambda: ["power"])
    #: ``true`` 表示 ``total`` 超出跨域可翻页窗口、``pages`` 已封顶。
    #: 此时 ``pages * page_size < total`` 是**预期**，不是分页算错——
    #: 跨域查询要在内存里归并两个库，深翻页会真的把两个库各拉一遍。
    truncated: bool = Field(
        False, description="可翻页数是否因跨域扫描窗口而封顶（此时 pages*page_size < total）"
    )


class SourcePage(PageMeta):
    items: list[SourceItem] = Field(default_factory=list)


# ======================================================================
# 文章
# ======================================================================
class ArticleListItem(_Base):
    """文章列表项（不含正文全文）。"""

    id: int
    title: str = ""
    excerpt: str = Field("", description="正文前 200 字摘要")
    source_name: str = Field("", description="原文来源（站点自述）")
    source_name_full: str = Field("", description="采集来源名")
    adapter_name: str = ""
    province_code: str | None = None
    province_name: str | None = None
    category: str = ""
    published_at_cst: datetime | None = Field(None, description="发布时间（北京时间）")
    time_precision: str = Field("unknown", description="second/minute/hour/day/unknown")
    url: str = ""
    quality_flags: str = Field("[]", description="质量标记 JSON 数组")
    channel: str = Field("power", description="数据域：power / province")


class ArticleImage(_Base):
    """正文配图（已上传 RustFS 的才有 object_url）。"""

    url: str = Field(..., description="图片原始地址")
    object_key: str = Field("", description="RustFS 对象键")
    object_url: str = Field("", description="可直接访问的地址（未下载成功时为空）")
    status: str = Field("pending", description="pending/downloaded/skipped/failed")
    ordinal: int = Field(0, description="正文中出现顺序")
    alt_text: str = Field("", description="alt 原文")
    byte_size: int = Field(0, description="字节数")


class ArticleDetail(ArticleListItem):
    """文章详情（含正文）。"""

    body_text: str | None = Field(None, description="正文纯文本，段落以空行分隔")
    published_at_utc: datetime | None = Field(None, description="发布时间（UTC）")
    content_hash: str = Field("", description="正文 SimHash")
    images: list[ArticleImage] = Field(default_factory=list,
                                       description="正文配图（按正文顺序）")
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ======================================================================
# 来源
# ======================================================================
class SourceItem(_Base):
    """来源（含生效配置）。"""

    id: int
    adapter_name: str
    name: str
    province_code: str | None = None
    province_name: str | None = None
    category: str = ""
    priority: int = 0
    crawl_interval_hours: int = 1
    enabled: bool = True
    status: str = "active"
    list_url: str = ""
    note: str = ""
    cooldown_until_utc: datetime | None = None
    last_discovery_success_at_utc: datetime | None = None
    channel: str = Field("power", description="数据域：power / province")


class SourceCreate(_Base):
    """新增来源请求。

    ⚠️ 新增来源**必须**先有对应的适配器文件（``newsagg/adapters/<pkg>/<adapter_name>.py``），
    否则协调器建批后引擎会以 ``AdapterNotFound`` 失败。本接口只登记配置。
    """

    adapter_name: Annotated[str, Field(min_length=2, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=128)]
    province_code: Annotated[str | None, Field(None, max_length=6)] = None
    base_url: str = ""
    news_path: str = ""
    list_url: str = ""
    category: Annotated[str, Field(pattern="^(sgcc|csg|independent|hmt|industry)$")] = "sgcc"
    priority: Annotated[int, Field(ge=0, le=9)] = 0
    crawl_interval_hours: Annotated[
        int,
        # 上限 127 对齐 sql/schema.sql 的 TINYINT（有符号）。
        # 曾经写成 168，Pydantic 放行但 MySQL 报 1264 Out of range，
        # 用户侧看到的是一个裸 500。改边界时必须同步看 DDL 的列类型。
        Field(ge=1, le=127),
    ] = 1
    enabled: bool = True
    note: Annotated[str, Field("", max_length=512)] = ""

    @field_validator("province_code")
    @classmethod
    def _check_province(cls, value: str | None) -> str | None:
        if value and not (value.isdigit() and len(value) == 6):
            raise ValueError("province_code 必须是 6 位行政区划代码")
        return value


class SourceUpdate(_Base):
    """更新来源**配置列**（白名单见 SourceRepository.UPDATABLE_FIELDS）。"""

    enabled: bool | None = None
    list_url: Annotated[str | None, Field(None, max_length=512)] = None
    crawl_interval_hours: Annotated[int | None, Field(None, ge=1, le=127)] = None
    priority: Annotated[int | None, Field(None, ge=0, le=9)] = None
    note: Annotated[str | None, Field(None, max_length=512)] = None
    status: Annotated[str | None, Field(None, pattern="^(active|blocked|degraded)$")] = None


# ======================================================================
# 省份
# ======================================================================
class ProvinceItem(_Base):
    """省级行政区。"""

    code: str
    name: str
    short_name: str = ""
    region: str = ""
    is_special: bool = False


class ProvinceCoverageItem(ProvinceItem):
    """省份覆盖度。"""

    source_count: int = 0
    enabled_source_count: int = 0
    article_count: int = 0
    empty_body: int = 0
    last_published_at: datetime | None = None


# ======================================================================
# 统计
# ======================================================================
class StatsOverview(_Base):
    """全局概览。"""

    articles: int = 0
    articles_24h: int = 0
    provinces_covered: int = 0
    provinces_total: int = 34
    provinces_missing: int = 0
    sources_total: int = 0
    sources_enabled: int = 0
    jobs_total: int = 0
    jobs_succeeded: int = 0
    jobs_unhealthy: int = 0
    success_rate: float = 0.0


class StatsQuality(_Base):
    """字段完整率。"""

    total: int = 0
    empty_title: int = 0
    empty_body: int = 0
    empty_source: int = 0
    no_time: int = 0
    empty_url: int = 0
    title_fill_rate: float = 0.0
    body_fill_rate: float = 0.0
    source_fill_rate: float = 0.0
    time_fill_rate: float = 0.0
    url_fill_rate: float = 0.0


class SourceStatsItem(_Base):
    """按来源统计。"""

    id: int
    adapter_name: str
    name: str
    province_code: str | None = None
    category: str = ""
    priority: int = 0
    crawl_interval_hours: int = 1
    enabled: bool = True
    article_count: int = 0
    empty_title: int = 0
    empty_body: int = 0
    no_time: int = 0
    last_published_at: datetime | None = None


class JobHealthItem(_Base):
    """任务健康度。"""

    status: str
    n: int = 0
    avg_discovered: float | None = None
    avg_saved: float | None = None
    last_finished: datetime | None = None


# ======================================================================
# 中台管理
# ======================================================================
class ApiKeyItem(_Base):
    """API Key 元信息（永不含明文/哈希）。"""

    id: int
    name: str
    scopes: str
    enabled: bool
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime | None = None


class ApiKeyCreate(_Base):
    """创建 API Key 请求。"""

    name: Annotated[str, Field(min_length=1, max_length=64)]
    scopes: str = Field("articles:read", description="逗号分隔，如 articles:read,sources:write")
    expires_in_days: Annotated[int | None, Field(None, ge=1, le=3650)] = None


class ApiKeyCreated(_Base):
    """创建成功响应——``key`` 明文**只在这一次返回**。"""

    id: int
    name: str
    key: str = Field(..., description="明文 Key，请立即保存；服务端只存 sha256")
    scopes: str
    expires_at: datetime | None = None


class AuditLogItem(_Base):
    """审计日志条目。"""

    id: int
    request_id: str = ""
    api_key_id: int | None = None
    api_key_name: str = ""
    method: str = ""
    path: str = ""
    status_code: int = 200
    client_ip: str = ""
    duration_ms: int = 0
    detail: str = ""
    created_at: datetime | None = None


# ======================================================================
# 健康检查
# ======================================================================
class DependencyHealth(BaseModel):
    """单个依赖的健康状态。"""

    name: str
    ok: bool
    detail: str = ""


class HealthStatus(BaseModel):
    """``/api/v1/health`` 响应。"""

    status: str = Field("ok", description="ok / degraded / down")
    app: str = ""
    version: str = ""
    uptime_seconds: float = 0.0
    dependencies: list[DependencyHealth] = Field(default_factory=list)
    latest_article_at: str | None = Field(None, description="最近一篇文章的发布时间（北京时间）")


# 解决 ArticlePage / SourcePage 的前向引用
ArticlePage.model_rebuild()
SourcePage.model_rebuild()
