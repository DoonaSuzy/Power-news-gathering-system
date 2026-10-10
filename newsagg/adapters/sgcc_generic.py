"""国家电网 / 南方电网 / 独立电网站群的通用适配器。

这三类电网官网共用同一套站群 CMS（国家电网 ``www.{简称}.sgcc.com.cn``、
南方电网 ``www.{简称}.csg.cn``），列表页与详情页 DOM 结构高度一致，
因此绝大多数来源**无需专用适配器**，继承 :class:`SgccGenericAdapter`
并声明元信息即可。

只有两类情况需要写专用适配器（覆写 ``parse_list`` / ``parse_detail``）：

1. 站点改版，通用选择器失效；
2. 正文由 JS 注入或走独立接口（需渲染或直连接口）。

港澳台电力公司与行业媒体的模板差异较大，见 ``adapters/hongkong`` 等目录。
"""

from __future__ import annotations

import re
from typing import Any

from newsagg.adapters._common import HeuristicAdapter
from newsagg.adapters.base import BaseAdapter, DetailParse, ListParse
from newsagg.sources_seed import SOURCE_BY_ID

__all__ = [
    "SgccGenericAdapter",
    "build_adapter_class",
    "SGCC_ARTICLE_HINT",
    "CSG_ARTICLE_HINT",
    "COLUMN_SUFFIXES",
]

#: 国家电网站群文章 URL 形态：``/html/main/col81/2026-10/09/content_xxxx.html``、
#: ``/p1/20261009/12345.html``、``/p1/spxw/202610/t20261009_123.html``、
#: ``/xwzx/gsyw/2026/14112.shtml``（新版站群：栏目/年/数字.shtml）、
#: ``/ttxw/2026/13382.shtml``
SGCC_ARTICLE_HINT = re.compile(
    r"(col\d+/\d+"
    r"|content_\d+"
    r"|/p\d+/\d{6,}"
    r"|/p\d+/[a-z]+/\d{6}"
    r"|/t\d{6,}_\d+\.s?html?$"
    r"|/[a-z]+\d*/\d{4}/\d{4,}\.s?html?$"   # /xwzx/gsyw/2026/14112.shtml
    r"|/\d{6,}\.s?html?$"
    r"|/art/\d+/\d+"                          # /art/2026/9/8/art_xxx.html
    r"|/articles/\d{4,6}/[a-z]*\d+\.s?html?$"  # /homepage/wbw/articles/202609/a1165973.html
    r"|[0-9a-f]{32}\.s?html?$"                # /p1/2c9482f59a4e6b34019fefcc99517ab8.html
    r")",
    re.I,
)

#: 南方电网站群：``/html/gdgd/col1234/2026-10/09/content_xxx.html``、``/xwzx/12345.html``
CSG_ARTICLE_HINT = re.compile(
    r"(content_\d+"
    r"|col\d+/\d+"
    r"|/\d{4}-\d{2}/\d{2}/"
    r"|/xwzx/\d+\.s?html?$"
    r"|/gsxw/\d+"
    r")",
    re.I,
)

#: 常见新闻栏目路径后缀（用于拼栏目页 URL，来源清单里多数为 ``/p1/``）
COLUMN_SUFFIXES: tuple[str, ...] = (
    "/p1/",              # 公司要闻（国网默认）
    "/p1/spxw.html",     # 视频新闻（浙江）
    "/p1/mtjj/",         # 媒体聚焦
    "/p1/yxfc/",         # 一线风采
    "/html/main/col81/", # 四川特例
)


class SgccGenericAdapter(HeuristicAdapter):
    """电网站群通用适配器基类。

    子类只需声明 ``source_id`` / ``source_name`` / ``province_code`` /
    ``allowed_hosts`` / ``list_url``，解析行为全部继承。
    """

    #: 栏目页之外追加的频道（多入口）
    extra_column_urls: tuple[str, ...] = ()
    #: 覆盖 URL 判定
    article_pattern = SGCC_ARTICLE_HINT

    #: 站点可能需要调整的选择器（子类按需覆写）
    body_selectors = (
        "div.TRS_Editor", "div#content", "div.content", "div.article-content",
        "div.article", "div.news_content", "div#zoom", "div.art-con",
        "div.articleCont",
    )
    title_selectors = (
        "h1", "div.title h1", "div.article-title", "div.news-title",
        "div.cont-title", "div.detail-title",
    )
    source_selectors = (
        "span.source", "div.source", "span.source-name", "div.news-source",
        "div.info span", "div.detail-info span",
    )
    time_selectors = (
        "span.time", "div.time", "span.date", "div.pubtime", "div.info span",
        "div.detail-info span",
    )

    # ------------------------------------------------------------------
    def parse_list(self, html: str, *, base_url: str) -> ListParse:
        """电网站群列表页：除通用链接抽取外，额外兼容 ``<li>`` 包裹的条目。"""
        return super().parse_list(html, base_url=base_url)

    def parse_detail(self, html: str, *, url: str) -> DetailParse:
        """详情页：通用启发式 + GNE 兜底。"""
        return super().parse_detail(html, url=url)


# --------------------------------------------------------------------------
def build_adapter_class(source_id: str, **overrides: Any) -> type[SgccGenericAdapter]:
    """按来源清单记录动态生成适配器类。

    用于两类场景：

    1. 脚手架生成 ``adapters/<province>/<source_id>.py`` 时的初始实现；
    2. 新增来源时先跑通通用解析，再决定是否需要专用适配器。

    :raises KeyError: ``source_id`` 不在 ``sources_seed.SOURCES`` 中
    """
    source = SOURCE_BY_ID[source_id]
    base_url = source["base_url"]
    list_url = base_url.rstrip("/") + (source.get("news_path") or "/")

    from urllib.parse import urlsplit

    host = urlsplit(base_url).hostname or ""

    attrs: dict[str, Any] = {
        "source_id": source_id,
        "source_name": source["name"],
        "province_code": source["province_code"],
        "category": source["category"],
        "priority": source["priority"],
        "crawl_interval_hours": source["interval_hours"],
        "language": "zh-TW" if source["category"] == "hmt" else "zh-CN",
        "list_url": list_url,
        "column_url": list_url,
        "allowed_hosts": (host,) if host else (),
        "article_pattern": (
            CSG_ARTICLE_HINT if source["category"] == "csg" else SGCC_ARTICLE_HINT
        ),
        "extra_column_urls": _default_extra_columns(source),
    }
    attrs.update(overrides)

    class_name = "".join(part.capitalize() for part in source_id.split("_")) + "Adapter"
    return type(class_name, (SgccGenericAdapter,), attrs)


def _default_extra_columns(source: dict[str, Any]) -> tuple[str, ...]:
    """按来源类别给出默认的扩展栏目页（多入口）。"""
    base = source["base_url"].rstrip("/")
    primary = source.get("news_path") or "/"

    if source["category"] in ("sgcc", "independent"):
        suffix = ("/p1/", "/p1/mtjj/", "/p1/yxfc/")
    elif source["category"] == "csg":
        suffix = ("/xwzx/", "/gsxw/")
    else:
        return ()

    urls: list[str] = []
    for path in suffix:
        url = base + path
        if path != primary and url not in urls:
            urls.append(url)
    return tuple(urls)


def assert_adapter_contract(adapter: BaseAdapter) -> list[str]:
    """校验一个适配器是否满足最小契约，返回问题列表。"""
    problems: list[str] = []
    if not adapter.source_id:
        problems.append("缺少 source_id")
    if not (adapter.list_url or adapter.column_url):
        problems.append("缺少 list_url / column_url")
    if not adapter.allowed_hosts:
        problems.append("缺少 allowed_hosts")
    if adapter.source_id and adapter.source_id not in SOURCE_BY_ID:
        problems.append(f"source_id={adapter.source_id} 不在来源清单中")
    return problems
