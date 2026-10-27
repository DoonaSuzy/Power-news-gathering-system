"""数据域合并（channel）的回归测试。

覆盖三件最容易出错、又最难靠人工发现的事：

1. **跨域归并分页**：第 N 页的边界必须取两库各 ``page*page_size`` 条再切，
   只取一页会把另一个库的前 N 页整体漏掉；
2. **降级语义**：省级库没接时 ``channel=province`` 必须 503，
   ``channel=all`` 必须降级为 power 并在响应里如实标注；
3. **计数口径**：``all`` 的 total 是两库之和，不是其中之一。

全部用例不依赖数据库：用假仓储替掉 ``ArticleService.repo`` /
``province_repo``，只验证编排逻辑。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from newsplatform.core.exceptions import ArticleNotFound, BadRequest, ChannelUnavailable
from newsplatform.core.response import fail, ok
from newsplatform.repositories.articles import ArticleQuery
from newsplatform.repositories.base import Page
from newsplatform.services.article_service import (
    CHANNEL_ALL,
    CHANNEL_POWER,
    CHANNEL_PROVINCE,
    MERGE_MAX_SCAN,
    ArticleService,
)
from newsplatform.services.channel_service import ChannelService


# ======================================================================
# 假仓储
# ======================================================================
def _row(article_id: int, dt: datetime, *, source: str = "某来源", flags: str = "[]") -> dict:
    """构造一条与 ``ArticleListItem`` 字段对齐的行。"""
    return {
        "id": article_id,
        "title": f"标题{article_id}",
        "excerpt": "摘要",
        "source_name": source,
        "source_name_full": source,
        "adapter_name": "adapter",
        "province_code": "110000",
        "province_name": "北京市",
        "category": "sgcc",
        "published_at_cst": dt,
        "time_precision": "day",
        "url": f"https://example.com/{article_id}",
        "quality_flags": flags,
    }


class FakeRepo:
    """最小可用的仓储替身：按构造时给的顺序返回。"""

    def __init__(self, rows: list[dict], total: int | None = None) -> None:
        self.rows = rows
        self.total = len(rows) if total is None else total
        self.calls: list[tuple[str, int]] = []

    async def top(self, q: ArticleQuery, limit: int) -> list[dict]:
        self.calls.append(("top", limit))
        return self.rows[:limit]

    async def list(self, q: ArticleQuery) -> Page:
        start = (q.page - 1) * q.page_size
        return Page(self.rows[start : start + q.page_size], self.total, q.page, q.page_size)

    async def count(self, q: ArticleQuery) -> int:
        return self.total

    async def get(self, article_id: int) -> dict | None:
        for row in self.rows:
            if row["id"] == article_id:
                return dict(row)
        return None

    async def iter_export_rows(self, q: ArticleQuery, *, batch: int = 500, offset: int = 0):
        return self.rows[offset : offset + batch]

    async def latest_published_at(self):
        return max((r["published_at_cst"] for r in self.rows), default=None)


def _service(power: FakeRepo, province: FakeRepo | None = None) -> ArticleService:
    """装配一个只依赖假仓储的 ArticleService。"""
    svc = ArticleService(object())  # type: ignore[arg-type]
    svc.repo = power  # type: ignore[assignment]
    svc.province_repo = province  # type: ignore[assignment]
    return svc


POWER_ROWS = [
    _row(1, datetime(2026, 10, 3, 9)),
    _row(2, datetime(2026, 10, 1, 9)),
]
PROVINCE_ROWS = [
    _row(1, datetime(2026, 10, 4, 9)),
    _row(2, datetime(2026, 10, 2, 9)),
]


# ======================================================================
# 1. 跨域归并分页
# ======================================================================
class TestMergePagination:
    async def test_global_order_spans_both_domains(self):
        """两库交替的时间线，第一页必须是全局最新的两条。"""
        svc = _service(FakeRepo(POWER_ROWS), FakeRepo(PROVINCE_ROWS))
        page = await svc.list(ArticleQuery(channel=CHANNEL_ALL, page=1, page_size=2))

        assert [i.published_at_cst.day for i in page.items] == [4, 3]
        assert [i.channel for i in page.items] == [CHANNEL_PROVINCE, CHANNEL_POWER]
        assert page.total == 4
        assert page.pages == 2
        assert page.channels == [CHANNEL_POWER, CHANNEL_PROVINCE]

    async def test_second_page_does_not_repeat_first_page(self):
        """翻页不能重复也不能漏——这是归并分页最典型的错法。"""
        svc = _service(FakeRepo(POWER_ROWS), FakeRepo(PROVINCE_ROWS))
        first = await svc.list(ArticleQuery(channel=CHANNEL_ALL, page=1, page_size=2))
        second = await svc.list(ArticleQuery(channel=CHANNEL_ALL, page=2, page_size=2))

        ids = [(i.channel, i.id) for i in first.items + second.items]
        assert len(ids) == len(set(ids)) == 4
        assert [i.published_at_cst.day for i in second.items] == [2, 1]

    async def test_scan_window_covers_page_depth(self):
        """第 N 页要求两库各扫 ``N*size`` 条，而不是只扫 ``size`` 条。"""
        power, province = FakeRepo(POWER_ROWS), FakeRepo(PROVINCE_ROWS)
        svc = _service(power, province)
        await svc.list(ArticleQuery(channel=CHANNEL_ALL, page=2, page_size=2))

        assert power.calls == [("top", 4)]
        assert province.calls == [("top", 4)]

    async def test_deep_page_is_rejected(self):
        """超过扫描上限必须 400，而不是悄悄只查一部分。"""
        svc = _service(FakeRepo(POWER_ROWS), FakeRepo(PROVINCE_ROWS))
        with pytest.raises(BadRequest, match="扫描上限"):
            await svc.list(
                ArticleQuery(channel=CHANNEL_ALL, page=MERGE_MAX_SCAN // 20 + 1, page_size=20)
            )

    async def test_single_domain_uses_sql_pagination(self):
        """单域不走归并，直接返回该库的分页结果。"""
        svc = _service(FakeRepo(POWER_ROWS, total=99), FakeRepo(PROVINCE_ROWS))
        page = await svc.list(ArticleQuery(channel=CHANNEL_POWER, page=1, page_size=2))

        assert page.total == 99
        assert page.channels == [CHANNEL_POWER]
        assert all(i.channel == CHANNEL_POWER for i in page.items)


# ======================================================================
# 2. 降级与拒绝
# ======================================================================
class TestChannelResolution:
    async def test_province_without_db_is_503(self):
        svc = _service(FakeRepo(POWER_ROWS))
        with pytest.raises(ChannelUnavailable):
            await svc.list(ArticleQuery(channel=CHANNEL_PROVINCE))

    async def test_all_degrades_to_power_when_province_missing(self):
        """没接省级库时 all 仍要能用，并且如实说明只查了一个域。"""
        svc = _service(FakeRepo(POWER_ROWS))
        page = await svc.list(ArticleQuery(channel=CHANNEL_ALL, page=1, page_size=10))

        assert page.channels == [CHANNEL_POWER]
        assert page.total == 2

    async def test_invalid_channel_is_400(self):
        svc = _service(FakeRepo(POWER_ROWS))
        with pytest.raises(BadRequest, match="channel"):
            await svc.list(ArticleQuery(channel="powerr"))

    async def test_empty_channel_falls_back_to_all(self):
        svc = _service(FakeRepo(POWER_ROWS), FakeRepo(PROVINCE_ROWS))
        page = await svc.list(ArticleQuery(channel="", page=1, page_size=10))
        assert page.total == 4


# ======================================================================
# 3. 计数 / 详情 / 字段整形
# ======================================================================
class TestCountAndDetail:
    async def test_total_is_sum_of_both_domains(self):
        svc = _service(FakeRepo(POWER_ROWS, total=2155), FakeRepo(PROVINCE_ROWS, total=1300))
        assert await svc.count(ArticleQuery(channel=CHANNEL_ALL)) == 3455

    async def test_detail_prefers_power_then_falls_back(self):
        svc = _service(FakeRepo(POWER_ROWS), FakeRepo(PROVINCE_ROWS))
        detail = await svc.detail(2, CHANNEL_ALL)
        assert detail.channel == CHANNEL_POWER

        # 电力域没有 id=9，但省级域有
        svc = _service(
            FakeRepo(POWER_ROWS),
            FakeRepo([_row(9, datetime(2026, 10, 5, 9))]),
        )
        detail = await svc.detail(9, CHANNEL_ALL)
        assert detail.channel == CHANNEL_PROVINCE

    async def test_detail_not_found_mentions_searched_channels(self):
        svc = _service(FakeRepo(POWER_ROWS), FakeRepo(PROVINCE_ROWS))
        with pytest.raises(ArticleNotFound, match="power, province"):
            await svc.detail(999, CHANNEL_ALL)

    async def test_blank_quality_flags_normalized(self):
        """省级旧数据 ``quality_flags`` 可能是空串，前端 JSON.parse 会炸。"""
        svc = _service(FakeRepo([_row(1, datetime(2026, 10, 3), flags="")]))
        page = await svc.list(ArticleQuery(channel=CHANNEL_POWER))
        assert page.items[0].quality_flags == "[]"


# ======================================================================
# 4. 覆盖度合并
# ======================================================================
class FakeProvinceRepo:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def coverage(self) -> list[dict]:
        return self.rows

    async def list(self) -> list[dict]:
        return self.rows


def _power_coverage() -> list[dict]:
    return [
        {"code": "110000", "name": "北京市", "short_name": "北京", "region": "华北",
         "is_special": False, "source_count": 1, "enabled_source_count": 1,
         "article_count": 10, "empty_body": 2, "last_published_at": datetime(2026, 10, 3)},
        {"code": "310000", "name": "上海市", "short_name": "上海", "region": "华东",
         "is_special": False, "source_count": 1, "enabled_source_count": 1,
         "article_count": 5, "empty_body": 0, "last_published_at": datetime(2026, 10, 1)},
    ]


def _province_coverage() -> list[dict]:
    return [
        {"code": "110000", "name": "北京市", "source_count": 2, "enabled_source_count": 2,
         "article_count": 30, "empty_body": 1, "last_published_at": datetime(2026, 10, 4)},
    ]


class TestCoverageMerge:
    def _service(self) -> ChannelService:
        svc = ChannelService(object())  # type: ignore[arg-type]
        svc.province_repo = FakeProvinceRepo(_power_coverage())  # type: ignore[assignment]
        svc.pv_source_repo = FakeProvinceRepo(_province_coverage())  # type: ignore[assignment]
        return svc

    async def test_counts_are_summed_not_replaced(self):
        rows = await self._service().coverage(CHANNEL_ALL)
        beijing = next(r for r in rows if r["code"] == "110000")

        assert beijing["source_count"] == 3      # 1 + 2
        assert beijing["article_count"] == 40    # 10 + 30
        assert beijing["empty_body"] == 3        # 2 + 1
        assert beijing["last_published_at"] == datetime(2026, 10, 4)

    async def test_provinces_without_province_data_keep_power_counts(self):
        rows = await self._service().coverage(CHANNEL_ALL)
        shanghai = next(r for r in rows if r["code"] == "310000")
        assert shanghai["article_count"] == 5

    async def test_power_channel_ignores_province_rows(self):
        rows = await self._service().coverage(CHANNEL_POWER)
        assert all(r["code"] != "999999" for r in rows)
        assert next(r for r in rows if r["code"] == "110000")["article_count"] == 10


# ======================================================================
# 5. 响应信封
# ======================================================================class TestEnvelope:
    def test_success_carries_message_and_timestamp(self):
        body = ok({"x": 1})
        assert body["code"] == 0
        assert body["message"] == "ok"
        assert body["timestamp"].endswith("+00:00")

    def test_failure_keeps_detail_alias(self):
        """``detail`` 是给老客户端的兼容别名，不能在这一版删掉。"""
        body = fail(3003, "文章不存在")
        assert body["message"] == "文章不存在"
        assert body["detail"] == "文章不存在"


# ======================================================================
# 6. SQL 可编译性（不连库也能抓出列名/联表错误）
# ======================================================================
class TestProvinceSql:
    """把省级仓储的 SELECT 编译成 MySQL 方言字符串。

    没有数据库也能验证：表名、列名、JOIN 条件是否成立。这类错误如果只在
    有库的环境才暴露，本地开发就等于没测。
    """

    def test_selects_compile_for_mysql(self):
        from sqlalchemy.dialects import mysql

        from newsplatform.repositories.province_articles import ProvinceArticleRepository

        repo = ProvinceArticleRepository(object())  # type: ignore[arg-type]
        q = ArticleQuery(channel=CHANNEL_PROVINCE, page=1, page_size=20)
        base = str(repo._base_select(q).compile(dialect=mysql.dialect()))
        count = str(repo._count_select(q).compile(dialect=mysql.dialect()))
        for sql in (base, count):
            assert "FROM articles" in sql
            assert "JOIN sources" in sql
        # 省级库的列名与电力库不同，编译结果必须真的用 media_name
        assert "media_name" in base

    def test_source_shape_matches_schema(self):
        """省级来源整形后的字段必须能被 ``SourceItem`` 接住。

        少字段 → pydantic 默认值兜住（看不出来）；多字段 → 被静默丢弃。
        两种都不会报错，所以只能显式比对键集合。
        """
        from newsplatform.models.schemas import SourceItem
        from newsplatform.repositories.province_articles import ProvinceSourceRepository

        # 采集库（newsagg）的真实列结构：verification_status，无 article_count
        raw = {
            "id": 1,
            "adapter_name": "beijing",
            "name": "千龙网",
            "province_code": "11",
            "province_name": "北京",
            "list_url": "https://example.com/list",
            "verification_status": "enabled",
            "last_discovery_success_at_utc": None,
        }
        shaped = ProvinceSourceRepository._shape_source(dict(raw))
        allowed = set(SourceItem.model_fields)
        assert set(shaped) <= allowed, f"多出字段: {set(shaped) - allowed}"
        SourceItem(**shaped)  # 不抛异常即字段齐全


# ======================================================================
# 7. 端点面（合并后的契约）
# ======================================================================
class TestEndpointSurface:
    def test_merged_api_has_42_endpoints(self):
        """合并前 40 个，新增 /channels 与 /health/ready = 42。"""
        from fastapi.routing import APIRoute

        from newsplatform.main import app

        biz = [
            r for r in app.routes
            if isinstance(r, APIRoute) and r.path.startswith("/api/v1")
        ]
        assert len(biz) == 42
        assert len({r.path for r in biz}) == 38
        paths = {r.path for r in biz}
        assert "/api/v1/channels" in paths
        assert "/api/v1/health/ready" in paths


# ======================================================================
# 8. 来源列表的跨域行为
# ======================================================================
class FakeSourceRepo:
    """``SourceRepository`` 的最小替身。"""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def list(self, *, province_codes=None, category=None, enabled=None,
                   keyword=None, page=1, page_size=50) -> Page:
        rows = self.rows
        if category:
            rows = [r for r in rows if r.get("category") == category]
        start = (page - 1) * page_size
        return Page(rows[start : start + page_size], len(rows), page, page_size)

    async def total(self) -> int:
        return len(self.rows)


def _src(sid: int, name: str, category: str = "sgcc") -> dict:
    return {
        "id": sid, "adapter_name": f"a{sid}", "name": name,
        "province_code": "110000", "province_name": "北京市",
        "category": category, "priority": 0, "crawl_interval_hours": 1,
        "enabled": True, "status": "active", "list_url": "", "note": "",
        "cooldown_until_utc": None, "last_discovery_success_at_utc": None,
    }


def _pv_src(sid: int, name: str) -> dict:
    return {
        "id": sid, "adapter_name": f"p{sid}", "name": name,
        "province_code": "110000", "province_name": "北京市",
        "list_url": "", "status": "enabled", "article_count": 0,
        "last_sync_at_utc": None,
    }


class TestSourceChannels:
    def _service(self) -> ChannelService:
        svc = ChannelService(object())  # type: ignore[arg-type]
        svc.source_repo = FakeSourceRepo([_src(1, "国网北京")])  # type: ignore[assignment]
        svc.pv_source_repo = FakeSourceRepo([_pv_src(9, "北京日报")])  # type: ignore[assignment]
        return svc

    async def test_all_returns_both_domains(self):
        payload = await self._service().sources(channel=CHANNEL_ALL, page=1, page_size=20)
        assert payload["total"] == 2
        assert {i["channel"] for i in payload["items"]} == {CHANNEL_POWER, CHANNEL_PROVINCE}

    async def test_power_category_excludes_province_rows(self):
        """按电力类别筛时不能混进省级来源——它没有 category 这个概念。"""
        payload = await self._service().sources(
            channel=CHANNEL_ALL, category="sgcc", page=1, page_size=20
        )
        assert [i["channel"] for i in payload["items"]] == [CHANNEL_POWER]

    async def test_province_with_power_category_is_empty(self):
        payload = await self._service().sources(
            channel=CHANNEL_PROVINCE, category="sgcc", page=1, page_size=20
        )
        assert payload["total"] == 0


# ======================================================================
# 9. 省份编码归一（6 位 ↔ 2 位）
# ======================================================================
class TestProvinceCodeNormalization:
    """电力库用 6 位（440000），省级库用 2 位（44）。

    不归一的话，按省查省级域会**静默返回 0 条**——这是合并时最隐蔽的一类缺陷。
    """

    def test_six_digit_expands_to_both_forms(self):
        from newsplatform.repositories.province_articles import code_variants

        assert code_variants("440000") == ("440000", "44")

    def test_two_digit_expands_back_to_six(self):
        from newsplatform.repositories.province_articles import code_variants

        assert code_variants("44") == ("44", "440000")

    def test_pad_code_normalizes_output(self):
        from newsplatform.repositories.province_articles import pad_code

        assert pad_code("44") == "440000"
        assert pad_code("440000") == "440000"
        assert pad_code(None) is None

    def test_conditions_match_both_forms(self):
        """WHERE 里必须同时出现 6 位与 2 位，否则省级库一行都查不到。"""
        from sqlalchemy import select
        from sqlalchemy.dialects import mysql

        from newsplatform.models.province_orm import ProvinceSource
        from newsplatform.repositories.province_articles import ProvinceArticleRepository

        repo = ProvinceArticleRepository(object())  # type: ignore[arg-type]
        conds = repo.conditions(
            ArticleQuery(channel=CHANNEL_PROVINCE, province_codes=("440000",))
        )
        # literal_binds：IN 列表默认是 postcompile 参数，不内联就看不到取值
        sql = str(
            select(ProvinceSource.id)
            .where(*conds)
            .compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})
        )
        assert "440000" in sql
        assert "'44'" in sql

    def test_expand_dedupes_and_keeps_order(self):
        from newsplatform.repositories.province_articles import _expand

        assert _expand(("440000", "320000")) == ("440000", "44", "320000", "32")
        assert _expand(("440000", "44")) == ("440000", "44")


# ======================================================================
# 10. 省级库表结构守卫
# ======================================================================
class TestProvinceSchemaContract:
    """省级库接的是**采集库**（newsagg），不是平台库（newsagg_platform）。

    两者 `sources` 的列名不同：采集库是 `verification_status`，
    平台库是 `status`；采集库 `articles` 没有 `created_at`。
    映射错会在查询时报 1054，但只在有库的环境才暴露——所以用静态断言钉住。
    """

    def test_source_uses_collector_columns(self):
        from newsplatform.models.province_orm import ProvinceSource

        cols = set(ProvinceSource.__table__.columns.keys())
        assert "verification_status" in cols
        assert "status" not in cols
        assert "article_count" not in cols
        assert "last_discovery_success_at_utc" in cols

    def test_article_has_no_created_at(self):
        from newsplatform.models.province_orm import ProvinceArticle

        cols = set(ProvinceArticle.__table__.columns.keys())
        assert "discovered_at_utc" in cols
        assert "created_at" not in cols

    def test_province_code_is_two_chars(self):
        """省级库的 provinces.code 是 CHAR(2)，这是编码归一存在的根本原因。"""
        from newsplatform.models.province_orm import ProvinceDict

        assert ProvinceDict.__table__.columns["code"].type.length == 2
