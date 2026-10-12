"""电力网（mm.chinapower.com.cn）—— 专属适配器。

站点结构（2026-10-12 实测）
--------------------------
- 列表页：``http://mm.chinapower.com.cn/``，首页含 70 条文章链接；
- 文章 URL 形态：**``/<栏目>/<子栏目>/<YYYYMMDD>/<数字>.html``**，
  如 ``/xw/zyxw/20261010/286423.html``（实测该站当日有稿）；
- 详情页正文容器：**``div.content``**（实测 4,846 字）；
- 标题带 ``_电力网`` 后缀。

栏目结构（首页实测）
--------------------
``/xw/zyxw/`` 中央要闻 ｜ ``/xw/gnxw/`` 国内新闻 ｜ ``/dww/dfbd/`` 地方报道
``/dlxxh/cjxx/`` 电力信息化·产业信息

robots：``Disallow: /d/ /e/class/ /e/config/ /e/data/ /e/enews/ /e/update/``
—— 全是 EmpireCMS 的管理与缓存路径，**新闻栏目页不受限**。

为什么单独写
------------
URL 里带 ``YYYYMMDD`` 是本站独有的强特征：不仅能高精度识别文章，
还能**直接从 URL 取发布日期**（``list_time_text``），比在列表 DOM 里
找时间戳可靠得多 —— 该站列表项的时间有时只在缩略图水印里。
"""

from __future__ import annotations

import re

from newsagg.adapters import register
from newsagg.adapters._common import HeuristicAdapter
from newsagg.adapters.base import DetailParse, ListItem, ListParse

#: ``/xw/zyxw/20261010/286423.html`` → 日期段
_URL_DATE = re.compile(r"/(\d{8})/\d+\.html?$")

#: 本站 robots.txt 的禁止项（EmpireCMS 模板）。全是管理与缓存路径，
#: 新闻栏目页不在其中 —— 适配器自己声明这一点，好过让通用 robots 解析器
#: 按「只要有 Disallow 就整站保守拦截」的方式处理（实测那样会 0 请求）。
_ROBOTS_DENIED = (
    "/d/", "/e/class/", "/e/config/", "/e/data/", "/e/enews/", "/e/update/",
)


def _path_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url).path or ""
    except ValueError:
        return ""


class ChinapowerAdapter(HeuristicAdapter):
    """电力网 —— 电力行业门户。"""

    source_id = "chinapower"
    source_name = "电力网"
    province_code = None
    category = "industry"
    priority = 2
    crawl_interval_hours = 4
    list_url = "http://mm.chinapower.com.cn/"
    column_url = "http://mm.chinapower.com.cn/xw/gnxw/"
    allowed_hosts = ("mm.chinapower.com.cn", "chinapower.com.cn")
    language = "zh-CN"

    #: 文章 URL：``/<栏目>/<子栏目>/<YYYYMMDD>/<数字>.html``
    #:
    #: ⚠️ **不要用 ``^`` 锚定**：``is_article`` 收到的是 ``extract_links`` 拼好的
    #: **绝对 URL**（``http://mm.chinapower.com.cn/xw/zyxw/20261010/286423.html``），
    #: 锚定开头会让正则永远不匹配 —— 实测踩到，表现为「列表解析出 0 条」。
    article_pattern = re.compile(r"/[a-z]{2,8}/[a-z]{2,8}/\d{8}/\d+\.html?$", re.I)

    #: 正文容器。``div.content`` 是实测的纯正文节点（4,846 字）。
    body_selectors = (
        "div.content",
        "div.article-content",
        "div#content",
    )

    _TITLE_SUFFIX = re.compile(r"\s*[-_|｜]\s*电力网\s*$")

    def parse_list(self, html: str, *, base_url: str) -> ListParse:
        """列表解析，并**从 URL 回填发布日期**。

        本站列表项的时间位置不固定（部分栏目只在缩略图上印日期），
        而 URL 里的 ``YYYYMMDD`` 是权威的。用 ``list_time_text`` 带回，
        详情页失败时也能靠它给文章定位时间。
        """
        result = super().parse_list(html, base_url=base_url)
        enriched: list[ListItem] = []
        for item in result.items:
            m = _URL_DATE.search(item.url)
            list_time = item.list_time_text
            if m and not list_time:
                d = m.group(1)
                list_time = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            enriched.append(ListItem(url=item.url, title=item.title, list_time_text=list_time))
        return ListParse(items=enriched, next_url=result.next_url, descending=result.descending)

    def parse_detail(self, html: str, *, url: str) -> DetailParse:
        """解析正文并剥离标题后缀；正文为空时用 URL 日期兜底发布时间。"""
        detail = super().parse_detail(html, url=url)
        if detail.title:
            detail.title = self._TITLE_SUFFIX.sub("", detail.title).strip()
        if not detail.published_text:
            m = _URL_DATE.search(url)
            if m:
                d = m.group(1)
                detail.published_text = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return detail

    def robots_allowed(self, url: str) -> bool:
        """自定义 robots 判定（需在配置里把本源设为 ``custom``）。

        本站 robots 禁止的全是 CMS 管理/缓存路径，**新闻栏目页是允许的**。
        若交给通用 ``RobotFileParser`` 按整站从严处理，实测结果是
        ``request_count=0`` —— 一个请求都没发出去，整站采不到。
        由适配器自己声明，既合规又不误伤。
        """
        path = _path_of(url)
        return not any(path.startswith(p) for p in _ROBOTS_DENIED)


register(ChinapowerAdapter())
