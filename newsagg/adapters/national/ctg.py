"""中国长江三峡集团（ctg.com.cn）—— 专属适配器。

站点结构（2026-10-12 实测）
--------------------------
- 新闻中心有 5 个并列栏目（均为 ``/sxjt/xwzx55/<栏目>/index.html``）：
  ``ttxw15`` 头条新闻 ｜ ``jtyw44`` 集团要闻 ｜ ``zhxw23`` 综合新闻
  ｜ ``tpxw49`` 图片新闻 ｜ ``ztbd68`` 专题报道
- 文章 URL：**``/sxjt/xwzx55/<栏目>/<数字ID>/index.html``**，
  如 ``/sxjt/xwzx55/zhxw23/2026101214572991850/index.html``
  —— **ID 前 8 位是日期** ``YYYYMMDD``，可从 URL 取发布时间；
- 详情页正文容器：**``div#content_dsh``**（实测 9,034 字、44 个 ``<p>``），
  比外层 ``div.wrap``（3,584 字）更完整。

robots：**无 robots.txt**。

两个本站特有的坑（都已处理）
--------------------------
1. **列表页标题被包在 ``document.write()`` 里**：

   .. code-block:: text

      document.write("".length>0?"":"三峡集团举办2026年职工运动会")

   这是模板用 JS 做兜底渲染的写法。直接取 ``<a>`` 的文本会得到整段 JS 代码，
   **必须把引号内的真实标题提取出来**，否则入库的标题全是垃圾。

2. **详情页 URL 以 ``/index.html`` 结尾**：容易被通用的「跳过栏目页」规则
   （``SKIP_HINT`` 里匹配 ``index.html`` 结尾的那条）误杀。这里靠 ``article_pattern``
   精确到 ``/<19位数字ID>/index.html`` 来区分 —— 栏目页是 ``/<栏目名>/index.html``，
   两者形态不同。
"""

from __future__ import annotations

import re

from newsagg.adapters import register
from newsagg.adapters._common import HeuristicAdapter
from newsagg.adapters.base import DetailParse, ListItem, ListParse

#: ``/sxjt/xwzx55/zhxw23/2026101214572991850/index.html`` → (2026, 10, 12)
#: 文章 ID 的前 8 位即 ``YYYYMMDD``。
_URL_ID = re.compile(r"/sxjt/xwzx55/[a-z]+\d+/(\d{4})(\d{2})(\d{2})\d*/index\.html?$", re.I)

#: 列表页标题的 JS 包装：``document.write("".length>0?"":"真实标题")``。
_DOCWRITE = re.compile(
    r'document\.write\(\s*""\.length\s*>\s*0\s*\?\s*""\s*:\s*"([^"]*)"\s*\)'
)


def _clean_title(raw: str) -> str:
    """从 ``document.write`` 包装里取出真实标题。"""
    m = _DOCWRITE.search(raw or "")
    if m:
        return m.group(1).strip()
    return (raw or "").strip()


class CtgAdapter(HeuristicAdapter):
    """中国长江三峡集团 —— 集团新闻。"""

    source_id = "ctg"
    source_name = "中国长江三峡集团"
    province_code = None
    category = "independent"
    priority = 1
    crawl_interval_hours = 4
    list_url = "https://www.ctg.com.cn/sxjt/xwzx55/zhxw23/index.html"
    column_url = "https://www.ctg.com.cn/sxjt/xwzx55/jtyw44/index.html"
    allowed_hosts = ("www.ctg.com.cn", "ctg.com.cn")
    language = "zh-CN"

    #: 文章 URL 精确到 ``/<数字ID>/index.html``（区别于 ``/<栏目名>/index.html``
    #: 的栏目页）。**不要用 ``^`` 锚定** —— ``is_article`` 收到的是绝对 URL。
    article_pattern = re.compile(
        r"/sxjt/xwzx55/[a-z]+\d+/\d{10,}/index\.html?$", re.I
    )

    #: 正文容器。``div#content_dsh`` 实测最完整（9,034 字 / 44 段）。
    body_selectors = (
        "div#content_dsh",
        "div.content_skdb",
        "div.wrap",
    )

    #: 详情页标题节点（``<title>`` 只有「集团新闻」这类站点名，不能用）。
    title_selectors = (
        "div#content_dsh h1",
        "div.content_skdb h1",
        "h1.article-title",
        "div.article-title",
        "h1",
    )

    def is_article(self, url: str) -> bool:
        """**必须覆写**：本站文章 URL 以 ``/index.html`` 结尾。

        基类 ``is_article`` 会先过 ``SKIP_HINT``，而它的 ``/index.html``
        规则正是用来「跳过栏目页」的 —— 于是本站**所有**文章都被误杀，
        实测表现为「列表解析出 0 条」。这里改成只认 ``article_pattern``：
        栏目页是 ``/<栏目名>/index.html``、文章页是 ``/<19位数字ID>/index.html``，
        形态本就不同，精确匹配即可区分。
        """
        if not self.allows(url):
            return False
        return bool(self.article_pattern.search(url))

    def parse_list(self, html: str, *, base_url: str) -> ListParse:
        """列表解析：清洗 ``document.write`` 标题 + 从 URL 回填日期。"""
        result = super().parse_list(html, base_url=base_url)
        enriched: list[ListItem] = []
        for item in result.items:
            title = _clean_title(item.title)
            if len(title) < 6:
                # 清洗后过短说明不是有效标题（可能是页脚 JS），丢弃
                continue
            m = _URL_ID.search(item.url)
            list_time = (
                f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                if m else item.list_time_text
            )
            enriched.append(
                ListItem(url=item.url, title=title, list_time_text=list_time)
            )
        return ListParse(items=enriched, next_url=result.next_url, descending=result.descending)

    def parse_detail(self, html: str, *, url: str) -> DetailParse:
        """解析正文；**发布时间一律以 URL 为准**。

        URL 的 ID 前 8 位就是 ``YYYYMMDD``，是权威值；而页面文本里散落着
        页脚、版权、正文中的各种日期，通用时间扫描会取到**完全错误的年份**
        （实测把 2026-10 的稿件识别成「1999年4月9日」）。所以这里不走
        「页面时间优先」，直接由 URL 覆盖。
        """
        detail = super().parse_detail(html, url=url)
        if detail.title:
            detail.title = _clean_title(detail.title)
        m = _URL_ID.search(url)
        if m:
            detail.published_text = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return detail


register(CtgAdapter())
