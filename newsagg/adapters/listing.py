"""列表页通用工具：链接抽取、时间文本识别、下一页定位。

把这些逻辑从适配器里抽出来，是因为电力站群的列表页模板差异集中在
「链接容器」上，而「取 href + 规范化 + 相对路径补全 + 时间文本识别」
是完全共通的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from parsel import Selector

from newsagg.cleaning import clean_text
from newsagg.urls import is_absolute

__all__ = ["Link", "extract_links", "find_next_page", "TIME_TEXT_RE", "NEXT_PAGE_HINT"]

#: 列表项旁的日期文本形态
TIME_TEXT_RE = re.compile(
    r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2})"
)

#: 「下一页」链接的锚文本 / 属性特征
NEXT_PAGE_HINT = re.compile(
    r"(下一页|下页|后一页|next|>|»|›)",
    re.I,
)
#: 纯数字分页链接（如 ``2`` / ``[3]``）
NUMERIC_PAGE = re.compile(r"^\s*\[?(\d{1,3})\]?\s*$")

#: 明显不是列表项的锚文本
_NOISE_TEXT = re.compile(
    r"^(更多|更多>>|more|详情|查看|首页|返回|上一页|下一页|尾页|末页|"
    r"登录|注册|English|设置|分享|打印|关闭)$",
    re.I,
)


@dataclass(slots=True)
class Link:
    """从列表页抽到的一条链接。"""

    url: str
    text: str = ""
    text_time: str = ""
    title_attr: str = ""


def _text_of(anchor) -> tuple[str, str]:  # noqa: ANN001
    """取锚文本与其后紧邻的日期文本。"""
    # 1) 锚点自身文本
    text = clean_text(" ".join(anchor.css("::text").getall()))
    title_attr = (anchor.attrib.get("title") or "").strip()

    # 2) 兄弟节点里的日期（列表项常见结构：<a>标题</a><span>2026-10-09</span>）
    text_time = ""
    parent = anchor.xpath("..")
    if parent:
        sibling_text = clean_text(" ".join(parent[0].css("::text").getall()))
        match = TIME_TEXT_RE.search(sibling_text)
        if match:
            text_time = match.group(1)
    if not text_time:
        # 3) ``<a>`` 自身带日期（<a>标题 2026-10-09</a>）
        match = TIME_TEXT_RE.search(text)
        if match:
            text_time = match.group(1)

    if not text and title_attr:
        text = title_attr
    return text, text_time


def extract_links(selector: Selector, *, base_url: str) -> list[Link]:
    """抽取页面内全部有效链接（绝对化 + 去噪）。"""
    links: list[Link] = []
    seen: set[tuple[str, str]] = set()

    for anchor in selector.css("a"):
        href = (anchor.attrib.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        url = urljoin(base_url, href)
        if not is_absolute(url):
            continue

        text, text_time = _text_of(anchor)
        title_attr = (anchor.attrib.get("title") or "").strip()

        if text and _NOISE_TEXT.match(text):
            continue

        # 同一 URL 只保留信息量最大的那条（优先带标题与时间的）
        key = (url, text)
        if key in seen:
            continue
        seen.add(key)

        links.append(Link(url=url, text=text, text_time=text_time, title_attr=title_attr))

    return links


def find_next_page(selector: Selector, *, base_url: str) -> str | None:
    """定位「下一页」URL；没有则返回 None。

    优先找锚文本明确指向下一页的链接，其次找「当前页 + 1」的数字链接。
    站点用 JS 渲染分页时返回 None（蜘蛛会停止翻页，不会空转）。
    """
    candidates: list[tuple[int, str]] = []

    for anchor in selector.css("a"):
        href = (anchor.attrib.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        text = clean_text(" ".join(anchor.css("::text").getall()))
        rel = (anchor.attrib.get("rel") or "").lower()

        if "next" in rel:
            candidates.append((0, href))
        elif text and NEXT_PAGE_HINT.search(text) and "上一页" not in text:
            candidates.append((1, href))
        elif text and NUMERIC_PAGE.match(text):
            candidates.append((2, href))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    return urljoin(base_url, candidates[0][1])
