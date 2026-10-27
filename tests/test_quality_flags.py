"""质量标记的回归测试：`no_title` / `no_source` / `no_time` 不能再恒真。

背景
----
`spider._store()` 原先在函数**开头**就调 `quality_flags(paragraphs, is_media_only=…)`，
而 `title`、`source_name`、护栏后的 `cst` 都在它之后才算出来。三个实参全没传，
于是 every 文章都被打上 `no_title + no_source + no_time`——生产抽样 300 条
100% 命中，`/stats/quality` 与「按质量标记筛选」因此完全失真。

所以这里有两层断言：
1. 纯函数 `quality_flags()` 的判否逻辑（字段齐全时必须是空列表）；
2. `_store()` 真的把三个实参传下去了——**这一层才是真正的守卫**，
   因为纯函数一直是对的，坏的是调用方。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from newsagg.cleaning import quality_flags
from newsagg.crawl.spider import SiteSpider

_PUBLISHED = datetime(2026, 10, 27, 9, 0)
_BODY = ["正文内容" * 40]          # 160 字，超过 short_body 的 120 字阈值


def test_complete_article_has_no_flags():
    """四个字段齐全 → 一个标记都不该有。"""
    assert quality_flags(
        _BODY, is_media_only=False, title="标题", source_name="来源", published=_PUBLISHED
    ) == []


def test_missing_published_marks_no_time_only():
    flags = quality_flags(_BODY, title="标题", source_name="来源", published=None)
    assert flags == ["no_time"]


def test_missing_title_and_source_are_reported_separately():
    assert quality_flags(_BODY, source_name="来源", published=_PUBLISHED) == ["no_title"]
    assert quality_flags(_BODY, title="标题", published=_PUBLISHED) == ["no_source"]


def test_short_and_media_only_flags():
    """正文不足 120 字 → short_body；纯图集 → media_only。"""
    flags = quality_flags(["短"], is_media_only=True, title="t", source_name="s",
                          published=_PUBLISHED)
    assert flags == ["short_body", "media_only"]


def test_empty_body_flag():
    assert quality_flags([], title="t", source_name="s", published=_PUBLISHED) == ["empty_body"]


class _FakeSession:
    """只实现 `_store` 真正会用到的那三个成员。"""

    def __init__(self) -> None:
        self.window = SimpleNamespace(end_cst=datetime(2026, 10, 27, 12, 0))
        self.stored: dict | None = None

    def list_title_for(self, nh: str) -> str:      # noqa: ARG002 - 接口对齐
        return ""

    def store_detail(self, **kwargs) -> None:
        self.stored = kwargs


def _run_store(parsed_value: datetime | None, *, title: str, source: str) -> dict:
    """用假 session 跑一遍 `_store`，返回真正落库的那批字段。"""
    session = _FakeSession()
    fake_self = SimpleNamespace(session=session)
    detail = SimpleNamespace(
        paragraphs=list(_BODY),
        media_only=False,
        title=title,
        source_name=source,
        image_refs=[],
    )
    parsed = SimpleNamespace(value=parsed_value, precision="minute")

    SiteSpider._store(fake_self, detail, "https://example.com/a", "hash-a", parsed)

    assert session.stored is not None
    return session.stored


def test_store_passes_title_source_and_time():
    """字段齐全时，落库的 quality_flags 必须是空的。

    这条用例的价值不在 `quality_flags()` 本身，而在「调用方有没有传参」——
    缺陷正是这么漏的，而修复前该函数零覆盖。
    """
    stored = _run_store(_PUBLISHED, title="标题", source="来源")
    assert stored["quality_flags"] == []
    assert stored["title"] == "标题"
    assert stored["source_name"] == "来源"


def test_store_marks_no_time_only_when_publish_missing():
    stored = _run_store(None, title="标题", source="来源")
    assert stored["quality_flags"] == ["no_time"]
    assert stored["time_precision"] == "unknown"


def test_store_drops_implausible_time_before_flagging():
    """1956 这类离谱年份被护栏置空后，应算「没有可信时间」。"""
    stored = _run_store(datetime(1956, 10, 8), title="标题", source="来源")
    assert stored["published_cst"] is None
    assert stored["quality_flags"] == ["no_time"]
