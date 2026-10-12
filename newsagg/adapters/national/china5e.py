"""中国能源网（china5e.com）—— 专属适配器。

站点结构（2026-10-12 实测）
--------------------------
- 列表页：``https://www.china5e.com/news/``，首页含 100+ 条文章链接；
- 文章 URL 形态：**``/news/news-<数字>-<数字>.html``**，
  如 ``/news/news-1208255-0.html``（末位是分页序号，通常为 0）；
- 详情页正文容器：**``div#newsBox``**（实测 2,585 字，含标题/时间/来源/正文），
  外层 ``div#ch_content`` 含面包屑（「您现在的位置：首页 政策与经济…」），
  所以 ``#newsBox`` 优先。

robots（**本适配器必须遵守的特殊约束**）
----------------------------------------
::

    Disallow: /caches  /phpcms  /install  /phpsso_server  /api  /admin.php
    Disallow: /statics  /templates
    Disallow: /index.php*
    Disallow: /news/08*        ← 2008 年归档，禁止
    Disallow: /news/2014*      ← 2014 年归档，禁止

前几项是 CMS 内部路径，与采集无关；但 **``/news/08*`` 与 ``/news/2014*``
是内容路径**，必须显式排除。本适配器在 :meth:`allows` 里落实这条 ——
否则带 ``--refresh`` 重抓历史时会把禁止的归档页也抓了。

这是「站点专属合规规则」，通用适配器无从知晓，只能写在本站适配器里。
"""

from __future__ import annotations

import re

from newsagg.adapters import register
from newsagg.adapters._common import HeuristicAdapter
from newsagg.adapters.base import DetailParse

#: robots 明确禁止的内容路径前缀（不是 CMS 内部路径，是真实文章归档）。
_ROBOTS_DENIED = (
    "/news/08",
    "/news/2014",
)

#: robots 禁止的 CMS 内部路径（采集天然不会碰，但显式列出便于审计）。
_ROBOTS_DENIED_INTERNAL = (
    "/caches", "/phpcms", "/install", "/phpsso_server",
    "/api", "/admin.php", "/statics", "/templates",
)


class China5eAdapter(HeuristicAdapter):
    """中国能源网 —— 能源行业综合门户。"""

    source_id = "china5e"
    source_name = "中国能源网"
    province_code = None
    category = "industry"
    priority = 2
    crawl_interval_hours = 4
    #: ⚠️ 必须是**首页**：``/news/`` 这个路径返回 404（实测）。
    #: 文章本身在 ``/news/news-N-N.html``，但列表只在首页聚合。
    list_url = "https://www.china5e.com/"
    column_url = "https://www.china5e.com/news/"
    allowed_hosts = ("www.china5e.com", "china5e.com")
    language = "zh-CN"

    #: 文章 URL：``/news/news-<数字>-<数字>.html``。
    #: 同样不能用 ``^`` 锚定 —— ``is_article`` 收到的是绝对 URL。
    article_pattern = re.compile(r"/news/news-\d+-\d+\.html?$", re.I)

    #: 正文容器。``#newsBox`` 比 ``#ch_content`` 干净（后者带面包屑）。
    body_selectors = (
        "div#newsBox",
        "div.w700.fl",
        "div#ch_content",
    )

    _TITLE_SUFFIX = re.compile(r"\s*[-_|｜]\s*(新闻[-－]能源资讯[-－]中国能源网|中国能源网)\s*$")

    def allows(self, url: str) -> bool:
        """在基类基础上叠加**本站 robots 的内容路径限制**。

        基类的 ``allows`` 只校验域名白名单；robots 里 ``/news/08*``、
        ``/news/2014*`` 是内容路径，只能由本站适配器自己排除。
        """
        if not super().allows(url):
            return False
        path = _path_of(url)
        if any(path.startswith(p) for p in _ROBOTS_DENIED):
            return False
        return not any(path.startswith(p) for p in _ROBOTS_DENIED_INTERNAL)

    def robots_allowed(self, url: str) -> bool:
        """自定义 robots 判定（需在配置里把本源设为 ``custom``）。

        本站 robots 的禁止项里**含内容路径**（``/news/08*``、``/news/2014*``
        是 2008/2014 年归档），由通用 ``RobotFileParser`` 处理时，
        实测结果是 ``request_count=0`` —— 整站采不到。

        这里把**完整的** robots 清单显式落地：CMS 内部路径与两段内容归档
        都禁，其余放行。合规规则写在最懂本站规则的适配器里，而不是散落在配置里。
        """
        path = _path_of(url)
        if any(path.startswith(p) for p in _ROBOTS_DENIED):
            return False
        if any(path.startswith(p) for p in _ROBOTS_DENIED_INTERNAL):
            return False
        # ``/index.php*`` 形式的动态入口也不采
        return not path.startswith("/index.php")

    def parse_detail(self, html: str, *, url: str) -> DetailParse:
        detail = super().parse_detail(html, url=url)
        if detail.title:
            detail.title = self._TITLE_SUFFIX.sub("", detail.title).strip()
        # 面包屑若被带进正文（外层容器命中时的常见噪声），逐段剔除
        detail.paragraphs = [
            p for p in detail.paragraphs
            if not p.lstrip().startswith("您现在的位置")
        ]
        return detail

    def _extract_paragraphs(self, selector):  # noqa: ANN001, ANN201
        """本站正文**不在 ``<p>`` 里**，改用文本节点 + ``<br>`` 切分。

        实测踩到：``div#newsBox`` 总文本 2,585 字，但其中 ``<p>`` 只有 72 字
        （一段「责任编辑：」）。基类先取 ``<p>``，于是正文入库只有 72 字 ——
        **看起来成功、实际几乎没内容**，比解析失败更难发现。
        所以这里先看 ``<p>`` 的总量是否够，不够就按 ``<br>``/``</div>`` 切分整个节点。
        """
        import re as _re

        for css in self.body_selectors:
            node = selector.css(css)
            if not node:
                continue
            paras = [
                p.strip() for p in node.css("p::text").getall() if len(p.strip()) >= 8
            ]
            if sum(len(p) for p in paras) >= 300:
                return paras
            # ``<p>`` 含量不足 → 正文在裸文本节点里，按换行标签切
            raw_html = node.get() or ""
            parts = _re.split(r"<br\s*/?>|</div>|</p>", raw_html, flags=_re.I)
            out: list[str] = []
            for part in parts:
                txt = _re.sub(r"<[^>]+>", "", part).replace("&nbsp;", " ").strip()
                if len(txt) >= 15:
                    out.append(txt)
            if out:
                return out
        return self._fallback_paragraphs(selector)


def _path_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url).path or ""
    except ValueError:
        return ""


register(China5eAdapter())
