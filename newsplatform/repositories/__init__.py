"""数据访问层：SQL 的唯一落点（async SQLAlchemy 2.0）。"""

from __future__ import annotations

from newsplatform.repositories.admin import ApiKeyRepository, AuditRepository, SnapshotRepository
from newsplatform.repositories.articles import ArticleQuery, ArticleRepository
from newsplatform.repositories.base import BaseRepository, Page
from newsplatform.repositories.sources import (
    ProvinceRepository,
    SourceRepository,
    empty_page,
)
from newsplatform.repositories.stats import StatsRepository

__all__ = [
    "BaseRepository",
    "Page",
    "ArticleRepository",
    "ArticleQuery",
    "SourceRepository",
    "ProvinceRepository",
    "StatsRepository",
    "ApiKeyRepository",
    "AuditRepository",
    "SnapshotRepository",
    "empty_page",
]
