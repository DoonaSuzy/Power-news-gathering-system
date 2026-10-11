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
from datetime import datetime
from datetime import time as dt_time
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.core.config import PlatformSettings, get_platform_settings
from newsplatform.core.database import (
    get_collector_session,
    get_collector_write_session,
    get_platform_session,
)
from newsplatform.core.exceptions import BadRequest
from newsplatform.core.provinces import load_rows
from newsplatform.core.provinces import resolve_code as _resolve_code
from newsplatform.core.provinces import resolve_codes as _resolve_codes
from newsplatform.repositories.articles import ArticleQuery

__all__ = [
    "SettingsDep",
    "PlatformSessionDep",
    "CollectorSessionDep",
    "CollectorWriteSessionDep",
    "Pagination",
    "PaginationDep",
    "ArticleQueryDep",
    "parse_day",
    "resolve_province_codes",
    "resolve_province_code",
    "CATEGORY_CHOICES",
]

SettingsDep = Annotated[PlatformSettings, Depends(get_platform_settings)]
PlatformSessionDep = Annotated[AsyncSession, Depends(get_platform_session)]
CollectorSessionDep = Annotated[AsyncSession, Depends(get_collector_session)]
#: 采集库**写**会话：仅用于 ``sources`` 配置列。用错会话会让写入被静默回滚。
CollectorWriteSessionDep = Annotated[AsyncSession, Depends(get_collector_write_session)]

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
# 省份解析
# ======================================================================
async def resolve_province_codes(session: AsyncSession, value: str | None) -> list[str] | None:
    """把用户写的省份解析成代码列表。

    返回 ``None`` 表示「不加省份过滤」（输入为空 / ``全国`` / ``all``）。
    解析不出来时抛 :class:`BadRequest`，消息里带全部可用取值。
    """
    if value is None or not str(value).strip():
        return None
    return _resolve_codes(value, await load_rows(session))


async def resolve_province_code(session: AsyncSession, value: str | None) -> str:
    """解析成**唯一**一个省份代码（``/provinces/{code}`` 这类单值场景）。

    输入是大区名（一对多，如 ``华南``）时明确报错，而不是随手取一个。
    """
    return _resolve_code(value, await load_rows(session))


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
    session: CollectorSessionDep,
    province: Annotated[
        str | None,
        Query(
            description=(
                "省份：代码 / 全称 / 简称 / 大区名均可。"
                "例：440000、广东省、广东、华南、港澳台、全国（不过滤）"
            ),
            examples=["广东"],
        ),
    ] = None,
    category: Annotated[str | None, Query(description="来源类别")] = None,
    source: Annotated[
        str | None,
        Query(description="适配器名（程序用的标识），如 sgcc_gd", examples=["sgcc_gd"]),
    ] = None,
    source_name: Annotated[
        str | None,
        Query(
            description="站点名称模糊匹配（人可读），如「北京」匹配「国网北京市电力公司」",
            examples=["北京"],
        ),
    ] = None,
    source_id: Annotated[int | None, Query(ge=1, description="来源 id")] = None,
    since: Annotated[str | None, Query(description="起始时间（北京时间）")] = None,
    until: Annotated[str | None, Query(description="结束时间（北京时间，只给日期则含当天）")] = None,
    keyword: Annotated[str | None, Query(description="标题或正文关键词")] = None,
    q: Annotated[
        str | None,
        Query(
            description=(
                "``keyword`` 的简写别名（等价，与 ``/sources?q=`` 保持一致）。"
                "两个都给时以 ``keyword`` 为准。"
            ),
            examples=["电网"],
        ),
    ] = None,
    body_only: Annotated[bool, Query(description="只返回正文非空的文章")] = False,
    exclude_duplicates: Annotated[bool, Query(description="排除转载标记的文章")] = False,
) -> ArticleQuery:
    """把 query string 翻译成 :class:`ArticleQuery`。

    省份在这里从「用户写法」解析成「代码列表」——接口面不该逼用户记行政区划代码。
    解析规则（代码 / 全称 / 简称 / 大区 / 全国）见 ``core/provinces.py``；
    解析不出来时抛 400 并列出全部可用取值。

    ``q`` 与 ``keyword`` 是同一个东西的两个名字：``keyword`` 是正式名，
    ``q`` 是为了和 ``/sources?q=`` 对齐而留的别名。**不接受**「传了但被静默忽略」
    这种结果——那正是本轮修掉的那类缺陷（``/sources?province=`` 曾经就这样）。
    """
    if category and category not in CATEGORY_CHOICES:
        raise BadRequest(f"category 取值必须是 {', '.join(CATEGORY_CHOICES)} 之一")

    province_codes = await resolve_province_codes(session, province)

    return ArticleQuery(
        province_codes=tuple(province_codes) if province_codes else None,
        category=category,
        adapter_name=source,
        source_name=source_name,
        source_id=source_id,
        date_from=parse_day(since),
        date_to=parse_day(until, end_of_day=True),
        keyword=keyword or q,
        body_only=body_only,
        exclude_duplicates=exclude_duplicates,
        page=pagination.page,
        page_size=pagination.page_size,
    )


ArticleQueryDep = Annotated[ArticleQuery, Depends(get_article_query)]
