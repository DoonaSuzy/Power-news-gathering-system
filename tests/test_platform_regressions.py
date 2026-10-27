"""数据中台的回归测试：把已经踩过的坑钉住。

这些用例**不需要数据库**——全部基于 SQL 编译结果与对象契约做断言，
因此在任何机器上都能跑（``pytest -q``）。真实跑库的部分交给
``scripts/e2e_smoke.py``（它对着一套真实运行的服务 + 两个库做端到端验证）。

每个用例对应一个真实发生过的缺陷，命名里写明「曾经怎么坏的」，
以便将来改代码的人一眼看懂为什么要有这条断言。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql.aiomysql import MySQLDialect_aiomysql

from newsplatform.core.database import CollectorBase, PlatformBase  # noqa: F401
from newsplatform.models.orm import Article
from newsplatform.models.schemas import SourceCreate, SourceUpdate
from newsplatform.repositories.articles import ArticleQuery, ArticleRepository
from newsplatform.services.stats_service import StatsService

_DIALECT = mysql.dialect()


# ======================================================================
# 1. aiomysql 的 pre-ping 兼容补丁
# ======================================================================
def test_aiomysql_dialect_has_own_do_ping():
    """aiomysql 方言必须自带 ``do_ping``。

    上游 ``MySQLDialect_aiomysql`` 没有实现 ``do_ping``，沿用了 pymysql 的版本；
    而 pymysql 那一版会去 import **本机的 pymysql** 判断 ``ping`` 的默认值，
    在 PyMySQL 的默认值是 ``False`` 时走到无参分支 ``ping()``，
    而 aiomysql 适配器的签名是 ``ping(reconnect)``（必传）→ 抛 TypeError。

    症状极具迷惑性：首次连接正常（新建连接不做 pre-ping），
    **第二次**从池里借出同一连接才炸，表现为「启动探活全 ok，一有请求就 500」。
    所以这里直接断言「方言自己实现了 do_ping」，防止有人删掉补丁。
    """
    assert "do_ping" in MySQLDialect_aiomysql.__dict__, "aiomysql 方言缺失 do_ping 补丁"
    assert getattr(MySQLDialect_aiomysql, "_powernews_ping_patched", False)


def test_aiomysql_do_ping_passes_reconnect_argument():
    """补丁必须按 ``ping(reconnect)`` 调用，且 reconnect=False。

    ``reconnect=False`` 表示「只探测、不自动重连」——连接已死时应当报错让
    连接池作废重建，而不是让驱动悄悄重连、把陈旧连接的会话状态带进业务逻辑。
    """

    class _FakeAdaptedConnection:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def ping(self, reconnect: bool) -> None:  # 模拟 aiomysql 适配器签名
            self.calls.append((reconnect,))

    conn = _FakeAdaptedConnection()
    assert MySQLDialect_aiomysql.do_ping(object(), conn) is True
    assert conn.calls == [(False,)]


def test_async_engines_disable_nothing_but_keep_pre_ping():
    """``pool_pre_ping`` 应当保持开启（补丁生效后无需靠关掉它来绕过问题）。"""
    from newsplatform.core.database import AsyncDatabase

    kwargs = AsyncDatabase().__dict__  # 不触发引擎创建
    assert kwargs is not None  # 占位：真正的断言在下面（不实例化引擎）
    source = inspect.getsource(AsyncDatabase._engine_kwargs)
    assert '"pool_pre_ping": True' in source


# ======================================================================
# 2. 分页计数：count 来源必须是「产出行」的子查询
# ======================================================================
def _repo() -> ArticleRepository:
    """构造一个不带会话的仓储（这些方法只拼 SQL，不需要连库）。"""
    return ArticleRepository(session=None)  # type: ignore[arg-type]


def test_count_select_is_aggregate_and_count_source_is_not():
    """``_count_select`` 是聚合查询，而 ``_paginate_count_source`` 必须是行来源。

    曾经的 bug：``paginate(count_from=self._count_select(q).subquery())``。
    ``paginate`` 会在传进来的东西外面再套一层 ``COUNT(*)``，于是变成

        SELECT count(*) FROM (SELECT count(*) ...)     -- 恒返回 1 行

    结果列表接口的 ``total`` 永远是 1，而同表的 ``/articles/count`` 又是对的
    （它直接用 ``_count_select``）——两个接口给出互相矛盾的数字，很难查。
    """
    q = ArticleQuery(page=1, page_size=20)

    count_sql = str(_repo()._count_select(q).compile(dialect=_DIALECT))
    assert "count(" in count_sql.lower(), "._count_select 应当是聚合查询"

    source_sql = str(_repo()._paginate_count_source(q).compile(dialect=_DIALECT)).lower()
    assert "count(" not in source_sql, (
        "._paginate_count_source 不能是聚合查询，否则外层 COUNT(*) 恒为 1"
    )
    assert "articles.id" in source_sql, "行来源应当只取主键，避免带出 body_text 大字段"


def test_paginate_counts_rows_not_aggregate_rows():
    """双保险：模拟外层套 COUNT(*) 后的语义。

    把「行来源」再包一层 ``select(func.count())`` 编译出来，
    确认派生表里是 ``select articles.id``（行集），而不是 ``select count(*)``
    （单个数字）。前者外层统计得到「命中 N 行」，后者恒得 1。
    """
    q = ArticleQuery(page=1, page_size=20)
    from_area = _repo()._paginate_count_source(q)
    sql = str(select(func.count()).select_from(from_area).compile(dialect=_DIALECT))
    normalized = " ".join(sql.lower().split())

    assert normalized.startswith("select count(*) as count_1 from ("), normalized
    # 派生表内部必须是行集（articles.id），不能是聚合
    inner = normalized.split("from (", 1)[1]
    assert inner.startswith("select articles.id as id"), inner
    assert "count(" not in inner, "派生表内部不能是聚合查询，否则总数恒为 1"


# ======================================================================
# 3. 导出必须推进游标（offset）
# ======================================================================
class _FakeMappings:
    def all(self):
        return []


class _FakeResult:
    def mappings(self):
        return _FakeMappings()


class _CapturingSession:
    """只记录执行过的语句，不做任何真实 IO。"""

    def __init__(self) -> None:
        self.stmts: list = []

    async def execute(self, stmt):
        self.stmts.append(stmt)
        return _FakeResult()


async def test_export_rows_applies_limit_and_offset():
    """导出分页必须同时带 LIMIT 与 OFFSET（游标要真的推进）。

    曾经的 bug：``iter_export_rows`` 只写 ``limit(batch)`` 而忽略翻页，
    调用方又是「取到不足一批才停」的循环 → 每轮都拿回**同一批**行。
    一次导出 50000 行里只有 500 行是真实数据，其余是 100 份重复，
    CSV 体积从 4MB 膨胀到 270MB。

    断言用 SQLAlchemy 的 ``_limit`` / ``_offset`` 属性而不是匹配 SQL 文本：
    MySQL 把两者编译成 ``LIMIT <offset>, <count>``（没有 OFFSET 关键字），
    按文本匹配会误判。
    """
    session = _CapturingSession()
    repo = ArticleRepository(session=session)  # type: ignore[arg-type]

    await repo.iter_export_rows(ArticleQuery(), batch=500, offset=1000)

    assert len(session.stmts) == 1
    stmt = session.stmts[0]
    assert stmt._limit == 500, "导出语句缺少 LIMIT"
    assert stmt._offset == 1000, "导出语句缺少 OFFSET —— 分页循环会反复取同一批数据"

    compiled = stmt.compile(dialect=_DIALECT)
    assert "LIMIT" in str(compiled).upper()
    assert (1000, 500) in [tuple(compiled.params.values())] or 1000 in compiled.params.values()


async def test_iter_export_rows_without_offset_starts_from_zero():
    """不传 offset 时从 0 开始（第一页语义），不能变成 offset=1000 之类的残留。"""
    session = _CapturingSession()
    repo = ArticleRepository(session=session)  # type: ignore[arg-type]

    await repo.iter_export_rows(ArticleQuery(), batch=100)

    assert session.stmts[0]._offset in (0, None)


def test_iter_export_rows_exposes_offset_parameter():
    """``offset`` 必须是显式参数，不能藏在 ArticleQuery.page 里。"""
    params = inspect.signature(ArticleRepository.iter_export_rows).parameters
    assert "offset" in params, "iter_export_rows 必须显式暴露 offset"


# ======================================================================
# 4. 统计快照必须写中台库
# ======================================================================
def test_stats_service_requires_platform_session_for_snapshots():
    """``stats_snapshots`` 在中台库，缺中台会话时必须立刻报错。

    曾经的 bug：``StatsService`` 只接一个会话并把它同时用于读写，
    ``POST /stats/snapshot`` 实际执行
    ``INSERT INTO powernews.stats_snapshots ...`` —— 表在中台库，
    采集库里根本没有，报 ``(1146, Table 'powernews.stats_snapshots' doesn't exist)``。
    错误信息里带着库名，很容易被误判成「表没建」而去查 DDL，
    真正的问题是**连接用错了库**。

    所以这里要求它「响亮地失败」，而不是 ``or session`` 之类的静默回落。
    """
    service = StatsService(object())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="缺少中台库会话"):
        _ = service.snapshots


def test_stats_snapshot_model_is_platform_base():
    """快照表模型必须挂在 PlatformBase 上（否则会被建到采集库）。"""
    from newsplatform.models.orm import StatsSnapshot

    assert issubclass(StatsSnapshot, PlatformBase)
    assert not issubclass(StatsSnapshot, CollectorBase)


# ======================================================================
# 5. 接口数值边界必须与 DDL 的列类型一致
# ======================================================================
def test_crawl_interval_hours_bound_matches_tinyint():
    """``crawl_interval_hours`` 上限必须是 127（``sql/schema.sql`` 里是 TINYINT）。

    曾经的 bug：接口放行到 168，Pydantic 过了、MySQL 报
    ``1264 Out of range value for column 'crawl_interval_hours'``，
    客户端拿到一个裸 500。

    真正的教训不是「这个数填错了」，而是**接口约束要和存储容量对齐**：
    改 DDL 或改 Field 边界时必须同时看另一边。
    """
    def upper_bound(model) -> int:
        meta = model.model_fields["crawl_interval_hours"].metadata
        for m in meta:
            if type(m).__name__ == "Le":
                return m.le
        raise AssertionError("crawl_interval_hours 缺少上限约束")

    assert upper_bound(SourceCreate) == 127
    assert upper_bound(SourceUpdate) == 127


def test_priority_bound_matches_tinyint_semantics():
    """``priority`` 是分档值（0..9），与 DDL 注释里的 0=高频 一致。"""
    meta = SourceCreate.model_fields["priority"].metadata
    bounds = {type(m).__name__: m for m in meta}
    assert bounds["Ge"].ge == 0
    assert bounds["Le"].le == 9


# ======================================================================
# 6. 复用现有 ORM 做一次「列名对齐」冒烟
# ======================================================================
def test_article_has_body_text_not_content():
    """文章正文字段名是 ``body_text``。

    采集库的 DDL 用的是 ``body_text``；如果 ORM/接口里写成 ``content``，
    查询会直接 ``Unknown column 'content'``，或者更糟——静默返回空正文。
    """
    assert hasattr(Article, "body_text")
    assert not hasattr(Article, "content")
    assert Article.__tablename__ == "articles"


# ======================================================================
# 7. 省级库名不能回退成废弃名
# ======================================================================
def test_stale_province_db_name_absent():
    """省级库在代码、示例配置、对外文案里只能叫 ``newsagg``。

    2026-10-27 之前，注释与 ``GET /api/v1/health`` 的 ``province_db.detail``
    写的都是另一个早已不存在的库名，而生产实际导入的库叫 ``newsagg``。
    运维照着那句文案配 ``POWERNEWS_PLATFORM_PROVINCE_DATABASE_URL`` 会连到
    不存在的库，且报错本身也是错的——「文档把人带沟里」的典型。

    只扫**代码与示例配置**，不含 ``docs/``：那是本地笔记（已被 gitignore），
    历史报告需要保留当时的原文。要描述这段历史时，写「那套库早已不存在」即可。
    """
    root = Path(__file__).resolve().parents[1]
    stale = "newsagg_" + "platform"  # 拆开写，避免本文件自己被扫到
    targets = [
        *root.glob("newsplatform/**/*.py"),
        *root.glob("scripts/*.py"),
        root / ".env.example",
    ]
    hits = sorted(
        str(p.relative_to(root))
        for p in targets
        if p.is_file() and stale in p.read_text(encoding="utf-8", errors="replace")
    )
    assert not hits, f"废弃库名仍在：{hits}（省级库统一叫 newsagg）"
