"""全局依赖注入（改造方案 §2.3 dependencies.py / §6.2「依赖注入」）。

为什么要集中在这里？
    路由函数只需要声明「我要什么」，不关心它是怎么造出来的。这样测试里
    ``app.dependency_overrides`` 换个实现就能跑，业务代码一行不改。

本模块提供三类依赖：
1. 资源：``SettingsDep`` / ``CollectorSessionDep`` / ``PlatformSessionDep``
2. 分页：``PaginationDep``（统一 page/page_size，并强制上限）
3. 查询条件：``ArticleQueryDep``（把 query string 翻译成 ``ArticleQuery``）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.core.config import PlatformSettings, get_platform_settings
from newsplatform.core.database import get_collector_session, get_platform_session
from newsplatform.core.exceptions import BadRequest
from newsplatform.repositories.articles import ArticleQuery

__all__ = [
    "SettingsDep",
    "PlatformSessionDep",
    "CollectorSessionDep",
    "Pagination",
    "PaginationDep",
    "ArticleQueryDep",
    "parse_day",
    "CATEGORY_CHOICES",
]

SettingsDep = Annotated[PlatformSettings, Depends(get_platform_settings)]
PlatformSessionDep = Annotated[AsyncSession, Depends(get_platform_session)]
CollectorSessionDep = Annotated[AsyncSession, Depends(get_collector_session)]

#: 来源类别（与 newsagg/sources_seed.py 的 CATEGORY_LABELS 保持一致）
CATEGORY_CHOICES = ("sgcc", "csg", "independent", "hmt", "industry")


# ======================================================================
# 分页
# ======================================================================
@dataclass(slots=True)
class Pagination:
    """分页参数（已收敛到合法区间）。"""

    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


async def get_pagination(
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，最大 100")] = 20,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> Pagination:
    """统一分页参数（改造方案 §6.3：默认 20、最大 100）。"""
    limit = settings.max_page_size
    size = int(page_size or settings.default_page_size)
    return Pagination(page=max(1, page), page_size=max(1, min(limit, size)))


PaginationDep = Annotated[Pagination, Depends(get_pagination)]


# ======================================================================
# 日期解析
# ======================================================================
_DAY_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d")


def parse_day(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    """解析日期字符串。

    只给日期（``2026-10-01``）时，``end_of_day=True`` 会补齐到 ``23:59:59``——
    否则 ``until=2026-10-01`` 会静默漏掉当天全部数据（旧项目踩过的坑）。
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in _DAY_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if end_of_day and fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            return datetime.combine(parsed.date(), dt_time(23, 59, 59))
        return parsed
    raise BadRequest(f"无法解析日期: {value}（示例 2026-10-01 或 2026-10-01 12:30）")


# ======================================================================
# 文章查询条件
# ======================================================================
async def get_article_query(
    pagination: PaginationDep,
    province: Annotated[str | None, Query(description="行政区划代码，如 440000")] = None,
    category: Annotated[str | None, Query(description="来源类别")] = None,
    source: Annotated[str | None, Query(description="适配器名，如 sgcc_gd")] = None,
    source_id: Annotated[int | None, Query(ge=1, description="来源 id")] = None,
    since: Annotated[str | None, Query(description="起始时间（北京时间）")] = None,
    until: Annotated[str | None, Query(description="结束时间（北京时间，只给日期则含当天）")] = None,
    keyword: Annotated[str | None, Query(description="标题或正文关键词")] = None,
    body_only: Annotated[bool, Query(description="只返回正文非空的文章")] = False,
    exclude_duplicates: Annotated[bool, Query(description="排除转载标记的文章")] = False,
) -> ArticleQuery:
    """把 query string 翻译成 :class:`ArticleQuery`。"""
    if category and category not in CATEGORY_CHOICES:
        raise BadRequest(f"category 取值必须是 {', '.join(CATEGORY_CHOICES)} 之一")
    if province and not (province.isdigit() and len(province) == 6):
        raise BadRequest("province 必须是 6 位行政区划代码，如 440000")

    return ArticleQuery(
        province_code=province,
        category=category,
        adapter_name=source,
        source_id=source_id,
        date_from=parse_day(since),
        date_to=parse_day(until, end_of_day=True),
        keyword=keyword,
        body_only=body_only,
        exclude_duplicates=exclude_duplicates,
        page=pagination.page,
        page_size=pagination.page_size,
    )


ArticleQueryDep = Annotated[ArticleQuery, Depends(get_article_query)]
