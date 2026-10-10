"""南方电网体系通用适配器。

南方电网省级公司站点（``www.{简称}.csg.cn``）前端为 SPA + 瑞数类动态防护，
静态抓取只能拿到 1166 字节的 JS 空壳，无法直接解析列表。

经实测，南网体系中**唯一可静态直连**的新闻入口是集团总部站
``https://www.csg.cn/xwzx/qygg/``（企业公告栏目），该站为国家电网站群同源
CMS（``content_`` / ``tYYYYMMDD_ID.html`` 形态），一个栏目页可解析出 20 条。

因此南方电网各省来源统一改为从总部站采集，并通过关键词把新闻归属到具体
省级公司；无法归属的记为「南方电网（集团）」。这样既避开了省级 SPA 的
渲染成本，又保证广东/广西/云南/贵州/海南五省都有真实数据落地。
"""

from __future__ import annotations

import re

from newsagg.adapters._common import HeuristicAdapter
from newsagg.adapters.base import ListItem, ListParse
from newsagg.adapters.listing import extract_links, find_next_page

__all__ = ["CsgGenericAdapter", "CSG_ARTICLE_HINT", "PROVINCE_KEYWORDS"]

#: 南网总部站文章 URL 形态：
#: ``/xwzx/qygg/202609/t20260914_357275.html``
CSG_ARTICLE_HINT = re.compile(r"/t\d{8}_\d+\.s?html?$", re.I)

#: 省级公司关键词 → 行政区划代码。命中即把新闻归属到该省。
PROVINCE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("广东电网", "440000"),
    ("广州供电局", "440000"),
    ("深圳供电局", "440000"),
    ("广东", "440000"),
    ("广西电网", "450000"),
    ("广西", "450000"),
    ("云南电网", "530000"),
    ("云南", "530000"),
    ("贵州电网", "520000"),
    ("贵州", "520000"),
    ("海南电网", "460000"),
    ("海南", "460000"),
)


def infer_province(title: str, body: str = "") -> str | None:
    """按标题/正文关键词推断新闻归属省份代码。

    Args:
        title: 文章标题。
        body: 正文（可选，标题未命中时再扫正文）。

    Returns:
        命中则返回省级行政区划代码，否则返回 ``None``（视为集团新闻）。
    """
    haystack = title or ""
    for keyword, code in PROVINCE_KEYWORDS:
        if keyword in haystack:
            return code
    if body:
        head = body[:200]
        for keyword, code in PROVINCE_KEYWORDS:
            if keyword in head:
                return code
    return None


class CsgGenericAdapter(HeuristicAdapter):
    """南方电网总部站（www.csg.cn）通用适配器。

    列表页走 ``/xwzx/qygg/``，详情页解析复用启发式规则（同源 CMS）。
    """

    category = "csg"
    #: 总部站主域
    allowed_hosts: tuple[str, ...] = ("www.csg.cn",)
    #: 默认列表页（企业公告）
    list_url = "https://www.csg.cn/xwzx/qygg/"
    column_url = "https://www.csg.cn/xwzx/qygg/"
    #: 同站其他可抓入口
    extra_column_urls: tuple[str, ...] = ()
    #: 文章 URL 判定
    article_pattern = CSG_ARTICLE_HINT

    #: 本省归属关键词。南方电网 5 省共用总部站一个列表源，
    #: 靠关键词把新闻归属到具体省份；空元组表示不过滤（集团视角全收）。
    province_keywords: tuple[str, ...] = ()

    def accepts_title(self, title: str) -> bool:
        """判定列表项标题是否归属本来源省份。

        南网总部站的列表页对五个省级适配器是同一份内容，若不按省份过滤，
        五省会把同一批集团新闻各自重复入库一遍。因此这里用标题关键词做归属：
        标题命中本省关键词才收；标题完全不含任何省份线索时（集团通稿）
        保留给集团来源，省级来源跳过。

        Args:
            title: 列表项标题文本。

        Returns:
            ``True`` 表示该条属于本来源。
        """
        if not self.province_keywords:
            return True
        text = title or ""
        if any(kw in text for kw in self.province_keywords):
            return True
        # 标题里若出现别的省份关键词，明确不属于本省
        for keyword, _code in PROVINCE_KEYWORDS:
            if keyword in text:
                return False
        # 无省份线索的集团通稿：省级来源不收，避免重复
        return False

    def parse_list(self, html: str, *, base_url: str) -> ListParse:
        """南网总部站列表页解析。

        该站列表链接为相对路径 ``./202609/t20260914_357275.html``，
        ``extract_links`` 已统一绝对化，此处只需按文章正则过滤。
        """
        from parsel import Selector

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
            # 省份归属过滤：南网总部站五省共用一份列表，需按关键词分流
            if not self.accepts_title(link.text):
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
