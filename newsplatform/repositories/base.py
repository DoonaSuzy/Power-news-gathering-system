"""数据访问层基类。

分层约定（改造方案 §2.3）：**SQL 只写在这一层**。
服务层拿到的永远是 ORM 对象或普通 dict，不会看到 ``select()``；
换存储（加缓存、换库）时改动被限制在本目录内。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["BaseRepository", "Page", "escape_like"]

T = TypeVar("T")


def escape_like(value: str) -> str:
    """转义 ``LIKE`` 模式里的通配符，返回可直接包进 ``%...%`` 的字面量。

    为什么必须转义
    --------------
    ``LIKE`` 里的 ``%`` 和 ``_`` 是通配符。用户搜「100%新能源」时，
    未转义的 ``%`` 会变成「任意字符」——看起来搜到了，其实匹配的是别的东西；
    搜 ``a_b`` 也会连带匹配到 ``axb``。参数化查询能防注入，但**防不住通配符语义**。

    反斜杠本身也要转义，且必须**最先**替换，否则后面补进去的转义符会被二次转义。
    """
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


class Page:
    """分页结果容器（元素为 dict，便于直接进 Pydantic）。"""

    __slots__ = ("items", "total", "page", "page_size")

    def __init__(self, items: Sequence[Any], total: int, page: int, page_size: int) -> None:
        self.items = list(items)
        self.total = total
        self.page = page
        self.page_size = page_size

    @property
    def pages(self) -> int:
        if self.page_size <= 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "pages": self.pages,
        }


class BaseRepository:
    """带会话的仓储基类。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------------- 通用分页 ----------------
    async def paginate(
        self,
        stmt: Select,
        *,
        page: int,
        page_size: int,
        count_from: Any | None = None,
    ) -> Page:
        """对任意 ``select()`` 做一次 SQL 层分页，返回 dict 列表。

        分页**只在 SQL 层做一次**（``LIMIT/OFFSET``），绝不在 Python 侧再切一刀——
        两层截断会静默少返数据（旧项目踩过的坑，见 ``newsagg/query.py`` 注释）。

        ``count_from`` 允许把 count 语句换成更廉价的来源（例如只 count 主表 +
        同样的 WHERE），避免 ``COUNT(*)`` 被外层联表放大。
        """
        page = max(1, int(page or 1))
        page_size = max(1, int(page_size or 20))

        from_area = count_from if count_from is not None else stmt.subquery()
        count_stmt = select(func.count()).select_from(from_area)
        total = int((await self.session.execute(count_stmt)).scalar() or 0)

        result = await self.session.execute(
            stmt.limit(page_size).offset((page - 1) * page_size)
        )
        rows = [dict(r) for r in result.mappings().all()]
        return Page(rows, total, page, page_size)
