"""业务服务层：校验、缓存、字段拼装、异常语义都在这里定。

路由层只做 HTTP 编排；数据访问一律经 ``repositories``。
"""

from __future__ import annotations

from newsplatform.services.admin_service import AdminService, AuditService
from newsplatform.services.article_service import EXPORT_COLUMNS, ArticleService
from newsplatform.services.source_service import ProvinceService, SourceService
from newsplatform.services.stats_service import StatsService

__all__ = [
    "ArticleService",
    "EXPORT_COLUMNS",
    "SourceService",
    "ProvinceService",
    "StatsService",
    "AdminService",
    "AuditService",
]
