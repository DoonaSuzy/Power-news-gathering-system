"""中国大唐集团（china-cdt.com）—— 专属适配器。

站点结构（2026-10-12 实测）
--------------------------
- 新闻中心：``/dtwz/xwzx/jtxw/A024004002Gone1.html``（集团新闻）；
- 文章 URL：**``/dtwz/xwzx/<栏目>/YYYY/MM/<ID>.html``**，
  如 ``/dtwz/xwzx/jtxw/2026/10/I1557686437837537280.html``
  —— **URL 自带年月**，可作为时间兜底；
- 详情页正文容器：**``div.xwzx-content``**（实测 761 字、5 个 ``<p>``）；
  同层还有 ``div.second-detail``（内容相同，作为备选）。

robots：**无 robots.txt**。

为什么单独写
------------
1. 文章 URL 形态是本站独有的（``/dtwz/xwzx/<栏目>/YYYY/MM/<ID>.html``），
   通用正则的 ``ARTICLE_HINT`` 覆盖不到 ``/I1557...html`` 这种大写字母开头的 ID；
2. 正文容器 ``div.xwzx-content`` 不在通用选择器表里；
3. 列表页 12 条/页且日期规整，是四家央企里**结构最标准**的一个。

⚠️ 注意：正文外层 ``div.footer`` 的文本量（999 字）**大于**正文容器（761 字），
   因为页脚含大量链接文字。所以**不能按「文本最长」挑容器**，必须用实测的选择器。
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from parsel import Selector

from newsagg.adapters import register
from newsagg.adapters._common import HeuristicAdapter
from newsagg.adapters.base import DetailParse, ListItem, ListParse
from newsagg.adapters.listing import find_next_page

#: ``/dtwz/xwzx/jtxw/2026/10/I1557686437837537280.html`` → (2026, 10)
_URL_YM = re.compile(r"/dtwz/xwzx/[a-z]+/(\d{4})/(\d{1,2})/[A-Za-z]\d+\.html?$", re.I)


class ChinaCdtAdapter(HeuristicAdapter):
    """中国大唐集团 —— 集团新闻。"""

    source_id = "china_cdt"
    source_name = "中国大唐集团"
    province_code = None
    category = "independent"
    priority = 1
    crawl_interval_hours = 4
    list_url = "https://www.china-cdt.com/dtwz/xwzx/jtxw/A024004002Gone1.html"
    column_url = "https://www.china-cdt.com/dtwz/xwzx/jtxw/A024004002Gone1.html"
    allowed_hosts = ("www.china-cdt.com", "china-cdt.com")
    language = "zh-CN"

    #: 文章 URL：``/dtwz/xwzx/<栏目>/YYYY/MM/<ID>.html``。
    #: ``<ID>`` 形如 ``I1557686437837537280``（大写字母开头 + 数字），
    #: 通用 ``ARTICLE_HINT`` 的 ``/\d{5,}\.s?html?$`` 要求**纯数字**，匹配不到。
    article_pattern = re.compile(
        r"/dtwz/xwzx/[a-z]+/\d{4}/\d{1,2}/[A-Za-z]\d+\.html?$", re.I
    )

    #: 正文容器（实测）。``div.footer`` 文本更长但那是页脚 —— 不能用「最长」策略。
    body_selectors = (
        "div.xwzx-content",
        "div.second-detail",
    )

    #: 标题节点。``<title>`` 只有「集团新闻」这类栏目名（实测），
    #: 真正的稿件标题在 ``div.detail-title``（位于 ``div.xwzx-content`` 内）。
    title_selectors = (
        "div.detail-title",
        "div.detail-top h1",
        "div.second-detail h1",
    )

    def parse_list(self, html: str, *, base_url: str) -> ListParse:
        """列表解析：**必须走专属 DOM 提取完整日期**。

        ⚠️ 为什么不能用基类实现 + URL 兜底：

        本站列表项结构是 ::

            <div class="news-item">
              <div class="news-item-title"><a href="...">标题</a></div>
              <div class="news-item-date">2026-10-08</div>
            </div>

        基类的 ``extract_links`` 取不到 ``news-item-date`` 的文本（它在 ``<a>`` 之外），
        于是 ``list_time_text`` 为空。早期实现用 URL 的 ``YYYY/MM`` 兜底 ——
        但那只到**月**精度，导致窗口回溯判断失效：``_oldest_list_time`` 把
        「还没到窗口起点」当成真的，于是**一路翻页**（本站分页深达 **100 页**），
        实测抓出 118 篇窗口外旧文（最早到 2025-07），而窗口内只有 4 篇。

        现在直接解析 ``div.news-item``，拿到每条稿件的**完整日期**，
        窗口回溯才能真正在 30 天边界停下。
        """
        sel = Selector(text=html)
        items: list[ListItem] = []
        seen: set[str] = set()

        for node in sel.css("div.news-item"):
            a = node.css("div.news-item-title a")
            if not a:
                continue
            href = a.attrib.get("href") or ""
            title = re.sub(r"\s+", " ", " ".join(a.css("::text").getall())).strip()
            if not href or len(title) < 6:
                continue
            url = urljoin(base_url, href)
            if url in seen or not self.is_article(url):
                continue
            seen.add(url)
            date = re.sub(
                r"\s+", " ",
                " ".join(node.css("div.news-item-date::text").getall()),
            ).strip()
            items.append(ListItem(url=url, title=title, list_time_text=date))

        return ListParse(
            items=items,
            next_url=find_next_page(sel, base_url=base_url),
            descending=True,
        )

    def parse_detail(self, html: str, *, url: str) -> DetailParse:
        detail = super().parse_detail(html, url=url)
        # 列表页与详情页都有时间，这里只在缺的时候用 URL 年月兜底（精度为「月」，
        # 由 cleaning.parse_published 的精度识别自然处理）
        if not detail.published_text:
            m = _URL_YM.search(url)
            if m:
                detail.published_text = f"{m.group(1)}-{int(m.group(2)):02d}-01"
        return detail


register(ChinaCdtAdapter())
