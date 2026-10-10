"""政府能源主管部门通用适配器（gov 类别）。

各级能源局 / 发改委官网普遍采用**国家站群 CMS**（与国网省级公司同源）：
栏目页为 ``/xwzx/<栏目>/``，文章为 ``/xwzx/<栏目>/YYYYMM/tYYYYMMDD_ID.html``。
因此列表与详情解析可直接复用 :class:`HeuristicAdapter` 的启发式规则。

本模块提供 :class:`GovGenericAdapter` 基类 + :func:`build_gov_adapter`，
各省级政府来源只需声明 id / 名称 / 省份 / 栏目路径即可。
"""

from __future__ import annotations

import re
from typing import Any

from newsagg.adapters._common import HeuristicAdapter
from newsagg.sources_seed import SOURCE_BY_ID

__all__ = ["GovGenericAdapter", "build_gov_adapter", "GOV_ARTICLE_HINT"]

#: 政府站群文章 URL 形态：
#: - ``/xwzx/gzdt/202610/t20261010_90975253.html``（能源局站群）
#: - ``/xwzx/2026-10/09/content_xxx.html``（部分发改委站群）
#: - ``/art/2026/10/9/art_xxx.html``
GOV_ARTICLE_HINT = re.compile(
    r"(/t\d{8}_\d+\.s?html?$"
    r"|content_\d+\.s?html?$"
    r"|/\d{4}-\d{2}/\d{2}/"
    r"|/art_\d+\.s?html?$"
    r"|/\d{4}/\d{1,2}/\d{1,2}/art_"
    r")",
    re.I,
)


class GovGenericAdapter(HeuristicAdapter):
    """政府能源主管部门通用适配器。

    详情页解析完全复用启发式规则（政府站群正文多为 ``div.TRS_Editor``
    或 ``div#zoom``，已在父类选择器池中覆盖）。
    """

    category = "gov"
    article_pattern = GOV_ARTICLE_HINT
    request_interval = 1.0  # 政府站相对敏感，放慢到 1 秒/请求

    #: 政府站正文容器补充（放在启发式默认池之后）
    body_selectors = (
        "div.TRS_Editor", "div#zoom", "div.article-content", "div.content",
        "div.article", "div#content", "div.xl-content", "div.view", "div.detail",
    )

    #: 时间容器（**不含** ``div.time``：政府站该容器是「今天是：」实时时钟，
    #: 非发布日期，实测会污染字段）。
    time_selectors = (
        "span.fbsj", "div#NewsArticlePubDay", "div.pubtime", "span.date",
        "div.article-info", "p.source", "span.time",
    )

    #: 日期提取（政府站群标准形如 ``2026-10-10 19:13:33``）
    _DATE_RE = re.compile(r"(\d{4}-\d{1,2}-\d{1,2})")
    _META_PUBDATE = re.compile(
        r'name=["\']PubDate["\'][^>]*content=["\'](\d{4}-\d{1,2}-\d{1,2})', re.I
    )
    _PUBDAY_DIV = re.compile(
        r'id=["\']NewsArticlePubDay["\'][^>]*>\s*(\d{4}-\d{1,2}-\d{1,2})', re.I
    )

    def parse_detail(self, html: str, *, url: str):  # noqa: ANN201
        """详情页解析：修正政府站群「实时时钟」污染发布日期的问题。"""
        parsed = super().parse_detail(html, url=url)
        txt = parsed.published_text or ""

        # 命中「今天是」或压根没有日期 → 走 meta / 隐藏字段 / 全局兜底
        if "今天是" in txt or not self._DATE_RE.search(txt):
            for rx in (self._META_PUBDATE, self._PUBDAY_DIV):
                m = rx.search(html)
                if m:
                    parsed.published_text = m.group(1)
                    return parsed
            # 全局兜底：剥标签后取第一个日期
            m2 = self._DATE_RE.search(re.sub(r"<[^>]+>", " ", html))
            parsed.published_text = m2.group(1) if m2 else ""
        else:
            # 规范化：``发布时间：2026-10-10 19:13:33`` → ``2026-10-10``
            m = self._DATE_RE.search(txt)
            if m:
                parsed.published_text = m.group(1)
        return parsed


# --------------------------------------------------------------------------
def build_gov_adapter(source_id: str, **overrides: Any) -> type[GovGenericAdapter]:
    """按来源清单记录动态生成政府来源适配器类。

    Args:
        source_id: 来源 id，须存在于 ``sources_seed.ALL_SOURCES``。
        **overrides: 覆盖默认元信息。

    Returns:
        生成的适配器类。

    Raises:
        KeyError: ``source_id`` 不在来源清单中。
    """
    from urllib.parse import urlsplit

    source = SOURCE_BY_ID[source_id]
    base_url = source["base_url"]
    list_url = base_url.rstrip("/") + (source.get("news_path") or "/")
    host = urlsplit(base_url).hostname or ""

    attrs: dict[str, Any] = {
        "source_id": source_id,
        "source_name": source["name"],
        "province_code": source["province_code"],
        "priority": source["priority"],
        "crawl_interval_hours": source["interval_hours"],
        "list_url": list_url,
        "column_url": list_url,
        "allowed_hosts": (host,) if host else (),
    }
    attrs.update(overrides)

    class_name = "".join(part.capitalize() for part in source_id.split("_")) + "Adapter"
    return type(class_name, (GovGenericAdapter,), attrs)
