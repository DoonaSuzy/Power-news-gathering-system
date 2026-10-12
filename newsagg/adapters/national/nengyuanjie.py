"""能源界（nengyuanjie.net）—— 专属适配器。

站点结构（2026-10-12 实测）
--------------------------
- 列表页：``https://www.nengyuanjie.net/``，首页含 39 条文章链接；
- 文章 URL 形态：**``/article/<数字>.html``**（唯一形态，收窄正则即可高精度识别）；
- 详情页正文容器：**``div.art-show``**（实测 2,025 字，只含正文，
  天然排除了标题/来源/导航 —— 比外层的 ``div.channel-left.fl``（3,671 字，
  含面包屑与推荐位）干净）；
- 标题带站点后缀 `` - 能源界``，需剥离。

robots：``Disallow: /caches /core /api /admin.php`` —— 新闻页不受限。

为什么单独写而不是用通用启发式
------------------------------
通用解析靠 ``ARTICLE_HINT`` 大正则碰运气。本站在 URL 形态上很规整，
收窄到 ``/article/\\d+\\.html`` 后既不会漏（形态唯一）也不会误收栏目页；
正文容器同理 —— ``div.art-show`` 是本站专属类名，通用选择器表里没有。
"""

from __future__ import annotations

import re

from newsagg.adapters import register
from newsagg.adapters._common import HeuristicAdapter
from newsagg.adapters.base import DetailParse


class NengyuanjieAdapter(HeuristicAdapter):
    """能源界 —— 全球中文能源门户。"""

    source_id = "nengyuanjie"
    source_name = "能源界"
    province_code = None
    category = "industry"
    priority = 2
    crawl_interval_hours = 4
    list_url = "https://www.nengyuanjie.net/"
    column_url = "https://www.nengyuanjie.net/"
    allowed_hosts = ("www.nengyuanjie.net", "nengyuanjie.net")
    language = "zh-CN"

    #: 本站文章 URL 唯一形态。收窄后 ``is_article`` 的精度远高于通用大正则。
    article_pattern = re.compile(r"/article/\d+\.html?$", re.I)

    #: 正文容器（按优先级）。``div.art-show`` 是实测的纯正文节点。
    body_selectors = (
        "div.art-show",
        "div.channel-left.fl",
        "div.sub-main",
    )

    #: 标题后缀（来自 ``<title>`` 的站点名）。
    _TITLE_SUFFIX = re.compile(r"\s*[-_|｜]\s*能源界\s*$")

    #: 正文首段的元信息行：``2026-10-12 12:04  来源：吉利远程  浏览：123``
    #:
    #: 本站把发布时间、来源、浏览量直接印在正文第一段里，而不是放在独立的
    #: meta 节点。通用选择器因此取不到来源（实测把日期 ``10-12`` 当成了来源），
    #: 且这一行会作为「正文首段」入库 —— 必须显式提取并剔除。
    _LEAD_META = re.compile(
        r"(?P<time>\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2})?)?[^\S\n]*"
        r"来源[:：][^\S\n]*(?P<src>[^\s\xa0浏览]{1,30})"
    )

    def parse_detail(self, html: str, *, url: str) -> DetailParse:
        """解析正文，剥离标题后缀，并从首段提取「来源 / 时间」元信息。

        正文抽取本身不用覆写：基类的 ``_extract_paragraphs`` 已带
        「声明的容器 → 全文 <p> 密度法 → GNE」三级降级。
        """
        detail = super().parse_detail(html, url=url)
        if detail.title:
            detail.title = self._TITLE_SUFFIX.sub("", detail.title).strip()

        if detail.paragraphs:
            first = detail.paragraphs[0]
            # 只在「明显是元信息行」时处理：短、且含「来源」
            if len(first) < 90 and "来源" in first:
                m = self._LEAD_META.search(first)
                if m:
                    if m.group("src"):
                        detail.source_name = m.group("src").strip()
                    if m.group("time") and not detail.published_text:
                        detail.published_text = m.group("time").strip()
                detail.paragraphs = detail.paragraphs[1:]
        return detail


register(NengyuanjieAdapter())
