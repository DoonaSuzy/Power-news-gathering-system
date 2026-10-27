"""数据域目录与跨域聚合。

这个服务回答三类问题：

1. **有哪些域、各有多少数据**（``GET /channels``）——客户端据此决定是否要
   显示「省级新闻」这个 tab，而不是先请求一次拿到 503 再猜。
2. **省份覆盖度怎么合**（``GET /provinces/coverage?channel=all``）——
   两个库各有一份按省聚合，口径必须相加而不是取其一。
3. **省份字典怎么合**——电力侧 34 个（含港澳台），省级侧 31 个，
   ``all`` 时以电力侧为骨架（它有 ``region`` / ``is_special`` 这些展示字段），
   省级侧只补计数。

聚合原则：**以电力侧的省份表为骨架**。它更全（34 > 31），且带展示字段；
省级侧只贡献「来源数 / 文章数 / 空正文数 / 最近时间」四个计数。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.core.exceptions import ChannelUnavailable
from newsplatform.repositories.articles import ArticleRepository
from newsplatform.repositories.province_articles import (
    PROVINCE_CATEGORY,
    ProvinceArticleRepository,
    ProvinceSourceRepository,
)
from newsplatform.repositories.sources import ProvinceRepository, SourceRepository
from newsplatform.repositories.stats import StatsRepository
from newsplatform.services.article_service import (
    CHANNEL_ALL,
    CHANNEL_CHOICES,
    CHANNEL_POWER,
    CHANNEL_PROVINCE,
)

__all__ = ["ChannelService", "CHANNEL_LABELS"]

#: 渠道中文名（对外展示用，只此一处）
CHANNEL_LABELS: dict[str, str] = {
    CHANNEL_POWER: "电力行业新闻",
    CHANNEL_PROVINCE: "省级官方新闻",
    CHANNEL_ALL: "全部",
}

#: 覆盖度里需要跨域相加的计数字段
_COUNT_FIELDS = ("source_count", "enabled_source_count", "article_count", "empty_body")


class ChannelService:
    """数据域目录与跨域覆盖度。"""

    def __init__(
        self, session: AsyncSession, province_session: AsyncSession | None = None
    ) -> None:
        self.session = session
        self.province_session = province_session
        self.article_repo = ArticleRepository(session)
        self.source_repo = SourceRepository(session)
        self.province_repo = ProvinceRepository(session)
        if province_session is not None:
            self.pv_article_repo = ProvinceArticleRepository(province_session)
            self.pv_source_repo = ProvinceSourceRepository(province_session)
        else:
            self.pv_article_repo = None
            self.pv_source_repo = None

    @property
    def province_available(self) -> bool:
        return self.pv_source_repo is not None

    def _require_province(self) -> None:
        if self.pv_source_repo is None:
            raise ChannelUnavailable(
                "省级数据域未接入（POWERNEWS_PLATFORM_PROVINCE_DATABASE_URL 未配置）"
            )

    # ---------------- 目录 ----------------
    async def catalog(self) -> list[dict[str, Any]]:
        """每个数据域的规模与新鲜度。

        未接入的域也返回一行（``available=false``、计数为 0），
        让客户端能区分「这个域存在但没开」和「这个域不存在」。
        """
        items: list[dict[str, Any]] = [
            {
                "channel": CHANNEL_POWER,
                "label": CHANNEL_LABELS[CHANNEL_POWER],
                "available": True,
                "sources": await self.source_repo.total(),
                "articles": await self.article_repo.total(),
                "latest_published_at": await self.article_repo.latest_published_at(),
            }
        ]
        if self.pv_article_repo is not None and self.pv_source_repo is not None:
            items.append(
                {
                    "channel": CHANNEL_PROVINCE,
                    "label": CHANNEL_LABELS[CHANNEL_PROVINCE],
                    "available": True,
                    "sources": await self.pv_source_repo.total(),
                    "articles": await self.pv_article_repo.total(),
                    "latest_published_at": await self.pv_article_repo.latest_published_at(),
                }
            )
        else:
            items.append(
                {
                    "channel": CHANNEL_PROVINCE,
                    "label": CHANNEL_LABELS[CHANNEL_PROVINCE],
                    "available": False,
                    "sources": 0,
                    "articles": 0,
                    "latest_published_at": None,
                }
            )
        total_sources = sum(int(i["sources"]) for i in items)
        total_articles = sum(int(i["articles"]) for i in items)
        items.append(
            {
                "channel": CHANNEL_ALL,
                "label": CHANNEL_LABELS[CHANNEL_ALL],
                "available": True,
                "sources": total_sources,
                "articles": total_articles,
                "latest_published_at": max(
                    (i["latest_published_at"] for i in items if i["latest_published_at"]),
                    default=None,
                ),
            }
        )
        return items

    # ---------------- 来源 ----------------
    async def sources(
        self,
        *,
        channel: str = CHANNEL_ALL,
        province_codes: tuple[str, ...] | None = None,
        category: str | None = None,
        enabled: bool | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """来源列表（跨域时归并后切片）。

        来源总量很小（60 + 34），跨域时直接两库各取全量再归并——
        为它设计一套跨库游标分页不值得，反而容易在边界上出错。
        单域仍然走 SQL 分页。
        """
        if channel == CHANNEL_PROVINCE:
            self._require_province()
            assert self.pv_source_repo is not None
            # 省级域没有 category 维度：传了电力侧类别（如 sgcc）就是「查不到」，
            # 不能当成没传——静默忽略筛选条件比返回空更糟。
            if category and category != PROVINCE_CATEGORY:
                return {
                    "items": [], "total": 0, "page": page,
                    "page_size": page_size, "pages": 0, "channel": channel,
                }
            page_obj = await self.pv_source_repo.list(
                province_codes=province_codes,
                enabled=enabled,
                keyword=keyword,
                page=page,
                page_size=page_size,
            )
            return {**page_obj.to_dict(), "channel": channel}

        if channel == CHANNEL_POWER:
            page_obj = await self.source_repo.list(
                province_codes=province_codes,
                category=category,
                enabled=enabled,
                keyword=keyword,
                page=page,
                page_size=page_size,
            )
            return {**page_obj.to_dict(), "channel": channel}

        if self.pv_source_repo is None:
            page_obj = await self.source_repo.list(
                province_codes=province_codes,
                category=category,
                enabled=enabled,
                keyword=keyword,
                page=page,
                page_size=page_size,
            )
            return {**page_obj.to_dict(), "channel": CHANNEL_POWER}

        power = await self.source_repo.list(
            province_codes=province_codes,
            category=category,
            enabled=enabled,
            keyword=keyword,
            page=1,
            page_size=500,
        )
        province = await self.pv_source_repo.list(
            province_codes=province_codes,
            enabled=enabled,
            keyword=keyword,
            page=1,
            page_size=500,
        )
        merged = [{**r, "channel": CHANNEL_POWER} for r in power.items]
        # 按电力侧类别筛选时，省级域本来就不该出现（它没有 category）
        if not (category and category != PROVINCE_CATEGORY):
            merged += [{**r, "channel": CHANNEL_PROVINCE} for r in province.items]
        merged.sort(key=lambda r: (r.get("province_code") or "", r.get("channel"), r.get("id") or 0))
        start = (page - 1) * page_size
        return {
            "items": merged[start : start + page_size],
            "total": len(merged),
            "page": page,
            "page_size": page_size,
            "pages": (len(merged) + page_size - 1) // page_size if page_size else 0,
            "channel": CHANNEL_ALL,
        }

    # ---------------- 来源产出统计 ----------------
    async def source_stats(
        self,
        *,
        channel: str = CHANNEL_ALL,
        province_codes: tuple[str, ...] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """按来源统计产出（跨域归并）。

        ``province_codes`` 是**6 位**码，省级仓储内部会自行降位匹配。
        排序统一按文章数倒序：两个域各自有序，拼起来再排一次才是全局有序。
        """
        rows: list[dict[str, Any]] = []
        if channel in (CHANNEL_POWER, CHANNEL_ALL):
            power = await StatsRepository(self.session).by_source(
                limit=limit, province_codes=province_codes
            )
            rows.extend({**r, "channel": CHANNEL_POWER} for r in power)
        if channel in (CHANNEL_PROVINCE, CHANNEL_ALL) and self.pv_source_repo is not None:
            province = await self.pv_source_repo.by_source(
                province_codes=province_codes, limit=limit
            )
            rows.extend(province)
        rows.sort(key=lambda r: (-int(r.get("article_count") or 0), str(r.get("id"))))
        return rows[:limit]

    # ---------------- 省份 ----------------
    async def provinces(self, channel: str = CHANNEL_ALL) -> list[dict[str, Any]]:
        """省份字典。``all`` / ``power`` 用电力侧 34 个，``province`` 用省级 31 个。"""
        if channel not in CHANNEL_CHOICES:
            raise ChannelUnavailable(f"未知数据域: {channel}")
        if channel == CHANNEL_PROVINCE:
            self._require_province()
            assert self.pv_source_repo is not None
            return [
                {
                    "code": row["code"],
                    "name": row["name"],
                    "short_name": "",
                    "region": "",
                    "is_special": False,
                }
                for row in await self.pv_source_repo.list_provinces()
            ]
        return await self.province_repo.list()

    async def coverage(self, channel: str = CHANNEL_ALL) -> list[dict[str, Any]]:
        """按省覆盖度。``all`` 时把省级计数加到电力骨架上。"""
        if channel == CHANNEL_PROVINCE:
            self._require_province()
            assert self.pv_source_repo is not None
            return [self._normalize_province_row(r) for r in await self.pv_source_repo.coverage()]

        power_rows = await self.province_repo.coverage()
        if channel == CHANNEL_POWER or self.pv_source_repo is None:
            return power_rows

        province_rows = {
            r["code"]: r for r in await self.pv_source_repo.coverage()
        }
        merged: list[dict[str, Any]] = []
        for row in power_rows:
            extra = province_rows.get(row["code"])
            if extra:
                row = {**row}
                for field in _COUNT_FIELDS:
                    row[field] = int(row.get(field) or 0) + int(extra.get(field) or 0)
                latest = [d for d in (row.get("last_published_at"),
                                      extra.get("last_published_at")) if d]
                row["last_published_at"] = max(latest) if latest else None
            merged.append(row)
        return merged

    @staticmethod
    def _normalize_province_row(row: dict[str, Any]) -> dict[str, Any]:
        """省级覆盖度补齐电力侧才有的展示字段（省级库没有这些列）。"""
        return {
            "code": row.get("code"),
            "name": row.get("name"),
            "short_name": "",
            "region": "",
            "is_special": False,
            "source_count": int(row.get("source_count") or 0),
            "enabled_source_count": int(row.get("enabled_source_count") or 0),
            "article_count": int(row.get("article_count") or 0),
            "empty_body": int(row.get("empty_body") or 0),
            "last_published_at": row.get("last_published_at"),
        }
