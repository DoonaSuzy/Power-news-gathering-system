"""华润电力（cr-power.com）—— 专属适配器。

站点结构（2026-10-12 实测）
--------------------------
- 新闻中心：``/zhxw/index.html``（新闻资讯）与 ``/news2/index.html``（下属企业动态）
  两个并列栏目 —— **项目落地类信息集中在 ``/news2/``**，门户首页看不到；
- 文章 URL：**``/zhxw/YYYY-MM-DD/<数字>.html``** 或 ``/news2/YYYY-MM-DD/<数字>.html``
  —— **URL 自带日期**，可直接取发布时间，比在列表 DOM 里找时间戳可靠；
- 详情页正文容器：``div.content``（实测 3,229 字）。

robots：``crpower.com.hk``（门户）有 Disallow；**``cr-power.com`` 无 robots.txt**。

为什么单独写
------------
1. URL 的 ``YYYY-MM-DD`` 段是本站独有的强特征，收窄正则即可高精度识别文章；
2. 两个栏目共用同一套 URL 规则，用一条正则覆盖（避免维护两份）；
3. 列表页的日期是**栏目级统一渲染**（实测全为同一天），**不能采信** ——
   所以发布时间一律从 URL 解析。
"""

from __future__ import annotations

import re

from newsagg.adapters import register
from newsagg.adapters._common import HeuristicAdapter
from newsagg.adapters.base import DetailParse, ListItem, ListParse

#: ``/zhxw/2026-09-14/2832944.html`` → (2026, 9, 14)
#: 两个栏目（``zhxw`` 新闻资讯 / ``news2`` 下属企业动态）共用此形态。
_URL_DATE = re.compile(r"/(?:zhxw|news2)/(\d{4})-(\d{1,2})-(\d{1,2})/\d+\.html?$", re.I)


class CrPowerAdapter(HeuristicAdapter):
    """华润电力 —— 集团总部新闻。"""

    source_id = "cr_power"
    source_name = "华润电力"
    province_code = None
    category = "independent"
    priority = 1
    crawl_interval_hours = 4
    list_url = "https://www.cr-power.com/zhxw/index.html"
    column_url = "https://www.cr-power.com/news2/index.html"
    allowed_hosts = ("www.cr-power.com", "cr-power.com")
    language = "zh-CN"

    #: 文章 URL：``/{zhxw|news2}/YYYY-MM-DD/<数字>.html``。
    #: **不要用 ``^`` 锚定** —— ``is_article`` 收到的是绝对 URL。
    article_pattern = re.compile(
        r"/(?:zhxw|news2)/\d{4}-\d{1,2}-\d{1,2}/\d+\.html?$", re.I
    )

    #: 正文容器。``div.content`` 是实测的纯正文节点。
    #: 更外层（``div.w1000`` / ``div#body``）含导航与页脚，不用。
    body_selectors = (
        "div.content",
        "div.main",
        "div#body",
    )

    #: 标题节点。``<title>`` 只有「华润电力门户官网-欢迎您」这类站点名，
    #: 真正的标题在正文容器内的 ``h3.news_h``。不加这条会把站点名当标题入库。
    title_selectors = (
        "h3.news_h",
        "div.content h3",
        "div.content h1",
        "div.content h2",
    )

    def parse_list(self, html: str, *, base_url: str) -> ListParse:
        """列表解析，并从 URL 回填发布时间。

        本站列表页的日期是**栏目级统一渲染**（实测同一栏目下所有条目显示同一天），
        不能反映稿件真实发布时间。URL 里的 ``YYYY-MM-DD`` 才是可靠的，
        所以用它填充 ``list_time_text``。
        """
        result = super().parse_list(html, base_url=base_url)
        enriched: list[ListItem] = []
        for item in result.items:
            m = _URL_DATE.search(item.url)
            if m:
                list_time = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            else:
                list_time = item.list_time_text
            enriched.append(
                ListItem(url=item.url, title=item.title, list_time_text=list_time)
            )
        return ListParse(items=enriched, next_url=result.next_url, descending=result.descending)

    def parse_detail(self, html: str, *, url: str) -> DetailParse:
        """解析正文，并在详情页无时间时用 URL 日期兜底。"""
        detail = super().parse_detail(html, url=url)
        if not detail.published_text:
            m = _URL_DATE.search(url)
            if m:
                detail.published_text = (
                    f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                )
        return detail


register(CrPowerAdapter())
