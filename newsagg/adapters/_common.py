"""通用启发式解析（详情页 + 列表页）。

电力站群绝大多数用同一套 CMS 模板输出，正文容器、标题、来源、时间的
DOM 位置高度相似。本模块把这些共性抽象成 :class:`HeuristicAdapter`：
子类只需声明元信息（``source_id`` / ``hosts`` / ``list_url``），
解析逻辑直接继承；个别例外站点再覆写选择器。

解析失败时依次降级：

1. 声明的正文容器选择器；
2. 全文 ``<p>`` 密度法（滤导航/页脚后仍有足量文字即认作正文）；
3. GNE 通用抽取（见 ``_gne.py``，需 ``POWERNEWS_GNE_FALLBACK=true``）。
"""

from __future__ import annotations

import re

from parsel import Selector

from newsagg.adapters.base import BaseAdapter, DetailParse, ListItem, ListParse
from newsagg.adapters.listing import extract_links, find_next_page
from newsagg.cleaning import clean_text

__all__ = [
    "HeuristicAdapter",
    "ARTICLE_HINT",
    "SKIP_HINT",
    "FOOTER_HINT",
    "time_from_url",
    "BODY_SELECTORS",
    "TITLE_SELECTORS",
    "SOURCE_SELECTORS",
    "TIME_SELECTORS",
]

#: 文章详情页 URL 形态。来自电力站群真实样本的归纳。
ARTICLE_HINT = re.compile(
    r"(content_\d+"
    r"|/\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"|/\d{4}/\d{2}[-/]\d{2}"
    r"|/\d{6,}/"
    r"|/t\d{6,}\.s?html?$"
    r"|/\d{5,}\.s?html?$"
    r"|system/\d{8}"
    r"|/p\d+/\d+"
    r"|article|/detail|/news/\d"
    r"|/content/[0-9a-z_-]{6,}\.s?html?$"
    r"|col\d+/\d+"
    r")",
    re.I,
)

#: 明显不是文章的路径（栏目页/检索页/导航）。仅当节点段**作为路径结尾**时才排除，
#: 避免误杀形如 ``/node_xxx/yyy.shtml`` 的详情页。
SKIP_HINT = re.compile(
    r"(/list\d*\.s?html?$|/index\.s?html?$|/search|/login|/rss|\.xml$|"
    r"/tag/|/tags?$|/node_[0-9a-z]+/?$|/node\.[a-z]+$|javascript:|mailto:)",
    re.I,
)

#: 页脚/版权/导航等噪声短段
FOOTER_HINT = re.compile(
    r"(版权所有|Copyright|ICP|备案号|主办单位|承办单位|技术支持|友情链接|"
    r"责任编辑|扫一扫|分享到|上一条|下一条|阅读次数|来源：\s*$|"
    r"modal window|skip to main content)",
    re.I,
)

#: 正文容器候选（按优先级）
BODY_SELECTORS: tuple[str, ...] = (
    "div.TRS_Editor", "div.article-content", "div#content", "div.content",
    "div.article", "div#articleContent", "div.text", "article",
    "div.main-content", "div.news_content", "div.cont", "div.dtu_neir",
    "div.articleCon", "div.article-body", "div.news-con", "div.articlecont",
    "div.Custom_UnionStyle", "div#zoom", "div.gray", "div.contTxt",
    "div.news_con", "div.articleCont", "div.detail-content",
)

#: 标题候选
TITLE_SELECTORS: tuple[str, ...] = (
    "h1", "h1.title", "div.title h1", "div.article-title", "div.news-title",
    "div.dtu_bt", "div.cont-title", "div.h-title", "div.art-title",
)

#: 来源候选
SOURCE_SELECTORS: tuple[str, ...] = (
    "span.source", "div.source", "span.source-name", "div.news-source",
    "span.where", "div.info span", "div.dtu_zj", "span.editor", "div.zuoze",
)

#: 时间候选
TIME_SELECTORS: tuple[str, ...] = (
    "span.time", "div.time", "span.date", "div.date", "div.info span",
    "div.dtu_sj", "span.pubtime", "em#pubtime", "div.article-info span",
)

#: URL 内嵌日期（页面无时间字样时的最后兜底）
_URL_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"/t(\d{4})(\d{2})(\d{2})(?:_\d+)?\.s?html?$", re.I),
    re.compile(r"/(\d{4})(\d{2})(\d{2})[/_-]"),
    re.compile(r"/(\d{4})[-/](\d{1,2})[-/](\d{1,2})[/_-]"),
    re.compile(r"[/_](\d{4})(\d{2})(\d{2})\.s?html?$", re.I),
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def time_from_url(url: str) -> str:
    """从 URL 抽发布日期，返回 ``YYYY-MM-DD``；抽不到返回空串。"""
    for pattern in _URL_DATE_PATTERNS:
        match = pattern.search(url)
        if not match:
            continue
        year, month, day = (int(g) for g in match.groups())
        if not (1995 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
            continue
        return f"{year:04d}-{month:02d}-{day:02d}"
    return ""


class HeuristicAdapter(BaseAdapter):
    """基于通用启发式规则的适配器。

    子类通常只需声明元信息；解析行为由本类提供。
    """

    body_selectors: tuple[str, ...] = BODY_SELECTORS
    title_selectors: tuple[str, ...] = TITLE_SELECTORS
    source_selectors: tuple[str, ...] = SOURCE_SELECTORS
    time_selectors: tuple[str, ...] = TIME_SELECTORS
    #: 文章 URL 判定（覆写可收窄/放宽）
    article_pattern: re.Pattern[str] = ARTICLE_HINT
    #: 每条列表项的最少文本长度（过滤空链接）
    min_link_text: int = 6

    # ===================== 列表页 =====================
    def parse_list(self, html: str, *, base_url: str) -> ListParse:
        selector = Selector(text=html)
        items: list[ListItem] = []
        seen: set[str] = set()

        for link in extract_links(selector, base_url=base_url):
            if link.url in seen:
                continue
            if not self.is_article(link.url):
                continue
            if len(link.text) < self.min_link_text:
                continue
            if not link.text_time and not link.text:
                continue
            seen.add(link.url)
            items.append(
                ListItem(url=link.url, title=link.text, list_time_text=link.text_time)
            )

        return ListParse(
            items=items,
            next_url=find_next_page(selector, base_url=base_url),
            descending=True,
        )

    def is_article(self, url: str) -> bool:
        if not self.allows(url):
            return False
        if SKIP_HINT.search(url):
            return False
        return bool(self.article_pattern.search(url))

    # ===================== 详情页 =====================
    def parse_detail(self, html: str, *, url: str) -> DetailParse:
        selector = Selector(text=html)
        title = self._first_text(selector, self.title_selectors) or self._title_from_head(selector)
        paragraphs = self._extract_paragraphs(selector)

        raw_source = self._first_text(selector, self.source_selectors)
        source_name = self._clean_source(raw_source)
        if not source_name:
            source_name = self._clean_source(self._scan_source_in_text(html))

        published_text = self._first_text(selector, self.time_selectors)
        if not published_text:
            published_text = self._scan_time_in_text(html)
        if not published_text:
            published_text = time_from_url(url)

        media_only = len(paragraphs) == 0
        used_fallback = False

        # 通用正文抽取兜底（GNE）
        if not paragraphs and self.allow_gne_fallback:
            from newsagg.adapters._gne import extract_with_gne

            gne = extract_with_gne(html, url=url)
            if gne:
                paragraphs = gne.get("paragraphs") or []
                if not title and gne.get("title"):
                    title = gne["title"]
                if not published_text and gne.get("published_text"):
                    published_text = gne["published_text"]
                if paragraphs:
                    media_only = False
                    used_fallback = True

        return DetailParse(
            title=title,
            paragraphs=paragraphs,
            source_name=source_name,
            published_text=published_text,
            media_only=media_only,
            used_fallback=used_fallback,
        )

    # --------------------- 内部工具 ---------------------
    @staticmethod
    def _title_from_head(selector: Selector) -> str:
        og = selector.css("meta[property='og:title']::attr(content)").get()
        if og and len(og.strip()) >= 4:
            return og.strip()
        title = selector.css("title::text").get()
        if title:
            title = clean_text(title)
            # 去掉「标题 - 站点名」后缀
            title = re.split(r"\s+[-_|—]\s+", title)[0].strip()
            if len(title) >= 4:
                return title
        return ""

    @staticmethod
    def _first_text(selector: Selector, candidates: tuple[str, ...]) -> str:
        """取首个匹配元素的文本：先取直接文本节点，再退化为后代文本。"""
        for css in candidates:
            nodes = selector.css(css)
            if not nodes:
                continue
            node = nodes[0]
            own = "".join(node.css("::text").getall()).strip()
            if own:
                return clean_text(own)
            for text in node.css("*::text").getall():
                text = text.strip()
                if len(text) >= 2:
                    return clean_text(text)
        return ""

    def _extract_paragraphs(self, selector: Selector) -> list[str]:
        for css in self.body_selectors:
            node = selector.css(css)
            if not node:
                continue
            paragraphs = [
                " ".join(p.css("::text").getall()).strip()
                for p in node.css("p")
            ]
            paragraphs = [p for p in paragraphs if p and len(p) >= 8]
            if paragraphs:
                return paragraphs
            text = " ".join(node.css("::text").getall()).strip()
            if text and len(text) >= 20:
                return [text]
        # 站点没有常见正文容器时，退回全文 <p> 密度法
        return self._fallback_paragraphs(selector)

    @staticmethod
    def _fallback_paragraphs(selector: Selector) -> list[str]:
        """全文 ``<p>`` 兜底：按长度、链接占比、页脚特征筛出正文段落。"""
        paragraphs: list[str] = []
        for node in selector.css("p"):
            text = " ".join(node.css("::text").getall()).strip()
            if len(text) < 15:
                continue
            link_text = "".join(node.css("a::text").getall()).strip()
            if link_text and len(link_text) > len(text) * 0.6:
                continue
            if FOOTER_HINT.search(text) and len(text) < 80:
                continue
            paragraphs.append(text)
        if sum(len(p) for p in paragraphs) >= 120:
            return paragraphs
        return []

    @staticmethod
    def _clean_source(raw: str) -> str:
        from newsagg.cleaning import clean_source

        return clean_source(raw)

    @staticmethod
    def _scan_source_in_text(html: str) -> str:
        """来源兜底：去标签后在全文里找「来源：xxx」。"""
        text = _WS_RE.sub(" ", _TAG_RE.sub(" ", html))
        match = re.search(r"来源\s*[：:]\s*([^\s]{2,25})", text)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _scan_time_in_text(html: str) -> str:
        text = _TAG_RE.sub(" ", html)
        match = re.search(
            r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?"
            r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?",
            text,
        )
        return match.group(0) if match else ""
