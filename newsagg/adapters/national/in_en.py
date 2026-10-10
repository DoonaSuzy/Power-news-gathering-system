"""全国性行业来源 —— 国际能源网（in_en）。

来源清单：https://www.in-en.com
采集栏目：资讯中心 ``/news/``（能源行业动态聚合）

站点为传统静态 CMS，文章 URL 形态为
``/article/html/energy-XXXXXXX.shtml``，与电网站群的 ``content_`` 形态不同，
故需覆写 ``article_pattern``。

字段特例（实测）
----------------
* ``<h1>`` 固定是网站名（"国际能源资讯网-能源资讯"），不可作标题；
  真实标题在 ``<h1 id="title">``，兜底用 ``<title>`` 去后缀。
* ``div.time`` 只有 ``HH:MM``（无日期），**不可用**；
  真实日期在 ``<p class="source"><b>日期：2026-09-18</b>…来源：…</p>``。
"""

from __future__ import annotations

import re

from newsagg.adapters import register
from newsagg.adapters._common import HeuristicAdapter

#: 国际能源网文章详情 URL：``/article/html/energy-2343750.shtml``
IN_EN_ARTICLE = re.compile(r"/article/html/[a-z]+-\d+\.s?html?$", re.I)


class InEnAdapter(HeuristicAdapter):
    """国际能源网。"""

    source_id = "in_en"
    source_name = "国际能源网"
    province_code = None
    category = "industry"
    priority = 2
    crawl_interval_hours = 4
    language = "zh-CN"

    allowed_hosts = ("www.in-en.com", "in-en.com")
    list_url = "https://www.in-en.com/news/"
    column_url = "https://www.in-en.com/news/"
    article_pattern = IN_EN_ARTICLE

    #: 站点正文容器
    body_selectors = (
        "div.article-content", "div.content", "div.news-content",
        "div.art-content", "div.detail-content", "div#content",
        "div.articleCon", "article",
    )

    #: 标题容器（``<h1 id="title">`` 才是真标题；站点其他 ``<h1>`` 是网站名）
    title_selectors = (
        "h1#title", "div.article-title", "div.news-title", "div.art-title",
        "div.detail-title", "div.content-title", "h1.article-title",
    )
    #: 时间容器：``<p class="source">`` 内含「日期：YYYY-MM-DD」
    time_selectors = (
        "p.source", "div.article-info", "span.date", "div.pubtime",
    )

    #: 详情页「日期：」「来源：」文本形态
    _DATE_RE = re.compile(r"日期[：:]\s*(\d{4}-\d{1,2}-\d{1,2})")
    _SOURCE_RE = re.compile(r"来源[：:]\s*(?:<a[^>]*>)?\s*([^<\s][^<]{0,40})")

    def parse_detail(self, html: str, *, url: str):  # noqa: ANN201
        """详情页解析（标题 / 日期 / 来源三个字段均需专门处理）。"""
        from parsel import Selector

        selector = Selector(text=html)
        parsed = super().parse_detail(html, url=url)

        # 标题：优先专用选择器 → <title> 去后缀；命中有效标题才覆盖
        title = self._first_text(selector, self.title_selectors)
        if not title:
            title = self._title_from_head(selector)
        if title and not self._looks_like_site_name(title):
            parsed.title = title

        # 日期：从「日期：YYYY-MM-DD」提取（覆盖只有 HH:MM 的 div.time）
        m = self._DATE_RE.search(html)
        if m:
            parsed.published_text = m.group(1)

        # 原文来源：从「来源：xxx」提取
        m2 = self._SOURCE_RE.search(html)
        if m2:
            parsed.source_name = m2.group(1).strip()

        return parsed

    @staticmethod
    def _looks_like_site_name(text: str) -> bool:
        """判断文本是否更像站点名而非文章标题。"""
        return ("国际能源" in text and ("资讯" in text or "网" in text) and len(text) < 20)


register(InEnAdapter())
