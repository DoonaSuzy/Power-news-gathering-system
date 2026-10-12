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
from typing import Annotated, Literal

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from newsagg.sources_seed import CATEGORY_LABELS
from newsagg.timefilter import RANGE_KEYS, RANGE_LABELS, resolve_range
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
    "TimeRange",
    "TIME_RANGE_DESC",
]

SettingsDep = Annotated[PlatformSettings, Depends(get_platform_settings)]
PlatformSessionDep = Annotated[AsyncSession, Depends(get_platform_session)]
CollectorSessionDep = Annotated[AsyncSession, Depends(get_collector_session)]
#: 采集库**写**会话：仅用于 ``sources`` 配置列。用错会话会让写入被静默回滚。
CollectorWriteSessionDep = Annotated[AsyncSession, Depends(get_collector_write_session)]

#: 来源类别。**直接取自种子清单的标签表**，不再抄一份字面量。
#: 早先这里写死 ``("sgcc", "csg", "independent", "hmt", "industry")``，而
#: ``sources_seed`` 后来补了 ``gov``（政府能源主管部门），于是库里 20+ 篇
#: ``category=gov`` 的文章在 ``/articles?category=gov`` 上直接 400——
#: 一份合法取值抄了两处，加类别时必然漂移。
CATEGORY_CHOICES = tuple(CATEGORY_LABELS)

#: 时间快捷档的取值集合。必须与 ``newsagg.timefilter.RANGE_KEYS`` 一致
#: （``test_timefilter_presets.py`` 把两者钉在一起，改一处就会红）。
TimeRange = Literal["today", "week", "month", "custom"]

#: Swagger 下拉框说明（档位标签取自 timefilter，文案只有一处）
_TIME_RANGE_DESC = (
    "时间范围快捷档："
    + "、".join(f"{key} = {RANGE_LABELS[key]}" for key in RANGE_KEYS if key != "custom")
    + f"；{RANGE_LABELS['custom']} = 用 since/until 自己给边界。"
    + "按**北京时间自然日**且含当天；与 since/until 互斥。"
)

#: 对外别名。``/articles`` 与 ``/news/summary`` 共用同一份描述文案——
#: 两个查询面各写一句必然漂移（本项目已经在「合法取值抄两处」上踩过坑）。
TIME_RANGE_DESC = _TIME_RANGE_DESC


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
    range_key: Annotated[
        TimeRange | None,
        Query(alias="range", description=_TIME_RANGE_DESC, examples=["week"]),
    ] = None,
    since: Annotated[str | None, Query(description="自定义起始时间（北京时间）")] = None,
    until: Annotated[
        str | None, Query(description="自定义结束时间（北京时间，只给日期则含当天）")
    ] = None,
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

    ``range``（今日 / 近1周 / 近1个月）与 ``since`` / ``until`` 也是互斥关系而不是
    优先级关系：两者同时给无法判断用户想要哪个窗口，直接 400 让他选一个，
    比「按 range 忽略自定义边界」或反之都更诚实。
    """
    if category and category not in CATEGORY_CHOICES:
        raise BadRequest(f"category 取值必须是 {', '.join(CATEGORY_CHOICES)} 之一")

    date_from, date_to = resolve_time_window(range_key, since=since, until=until)

    province_codes = await resolve_province_codes(session, province)

    return ArticleQuery(
        province_codes=tuple(province_codes) if province_codes else None,
        category=category,
        adapter_name=source,
        source_name=source_name,
        source_id=source_id,
        date_from=date_from,
        date_to=date_to,
        keyword=keyword or q,
        body_only=body_only,
        exclude_duplicates=exclude_duplicates,
        page=pagination.page,
        page_size=pagination.page_size,
    )


def resolve_time_window(
    range_key: str | None,
    *,
    since: str | None = None,
    until: str | None = None,
) -> tuple[datetime | None, datetime | None]:
    """把 ``range`` / ``since`` / ``until`` 三个参数解析成闭区间。

    **这是唯一实现**：``/articles``（列表、计数、导出）与 ``/news/summary``
    都必须调它。两处各写一遍的后果是「接口 A 说 307 篇、接口 B 说 306 篇」，
    而且没法解释差在哪 —— 这个缺陷本项目已经踩过一次。

    ``range`` 与 ``since`` / ``until`` 是**互斥**而非优先级关系：同时给无法判断
    用户想要哪个窗口，直接 400 让他选一个，比按某一种优先级静默解释诚实。

    Args:
        range_key: 快捷档键名（``today`` / ``week`` / ``month`` / ``custom``）。
        since: 自定义起始时间（北京时间）。
        until: 自定义结束时间（只给日期则含当天 23:59:59）。

    Returns:
        ``(date_from, date_to)``，两端可能为 ``None``（表示该侧不过滤）。

    Raises:
        BadRequest: ``range`` 与 ``since`` / ``until`` 同时给出。
    """
    window = resolve_range(range_key)
    if window is not None and (since or until):
        raise BadRequest(
            "range 与 since/until 二选一：用快捷档就别再传自定义边界，"
            "要自定义请传 range=custom 或不传 range"
        )
    if window is not None:
        return window
    return parse_day(since), parse_day(until, end_of_day=True)


ArticleQueryDep = Annotated[ArticleQuery, Depends(get_article_query)]
