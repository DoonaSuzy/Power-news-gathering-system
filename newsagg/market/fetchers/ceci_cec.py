"""CECI 采集器：中国电煤采购价格指数（中电联官方发布）。

数据源
------
中国电力企业联合会电力评价咨询院（``pjzx.cec.org.cn``）的站内接口::

    GET {base}/content/list?id=724&pageNumber=1&pageSize=N   # CECI周报 栏目
    GET {base}/content/detail?id={articleID}                 # 单篇正文（JSON）

``base = https://pjzx.cec.org.cn/ms-mcms/mcms``

为什么不去爬官网页面
--------------------
官网 ``pjzx.cec.org.cn/detail/index.html?3-359342`` 是**纯 JS 壳**（不到 5 KB，
正文靠 ``index.js`` 里的 ``$.get(webUrl + "/content/detail?id=" + id)`` 异步拉取）。
壳里没有数据，抓它等于抓空。好在**它自己用的那个 JSON 接口是公开可访问的**，
所以直接打接口：一次拿到结构化正文 + 发布时间 + 栏目名，比解析 HTML 稳得多。

为什么不用「单期指数」栏目
--------------------------
``CECI指数`` 栏目（id=722）里有 ``CECI进口指数第345期`` 这样的独立发布，
但**正文是一张 JPG 图片**（``<img src=".../进口345.jpg">``），要 OCR 才能取数。
而 ``CECI周报`` 栏目（id=724）的正文是**纯文本**，数值写明为：:

    CECI进口指数综合到岸标煤单价1209元/吨，较上期下降43元/吨，环比下降3.4%。

可正则直取，无需 OCR。所以选周报作为数据源。

产出哪些数据集
--------------
一份周报里同时发布多个指数，本采集器提取其中两个：

- ``ceci_import``：进口指数**综合到岸标煤单价**（元/吨）——核心目标；
- ``ceci_pmi``：采购经理人指数（%）——同一份周报里的电煤景气指标，顺带取。

**因此 ``fetch()`` 的返回值里数据集不只一个。**这与「一个采集器一个 dataset」
的直觉不符，但同源同页、一次请求就能拿全，拆成两个采集器只会把请求数翻倍。
CLI 侧按采集器**类**去重（见 ``market/cli.py``），不会重复抓。

业务日期口径
------------
``price_date`` 取**观测周的结束日**（正文「本周（9月10日至9月17日，下同）」里的
9月17日），而不是周报的发布日（9月21日）。理由是数据描述的是「截至9月17日这一周」
的状态，用发布日会凭空多出 4 天的错位。解析不到时才回落到发布日。

已知边界
--------
- 周报标题是普通新闻标题（如「电煤淡季特征显现  价格整体高位震荡」），
  **标题里看不出是 CECI 周报**；只有正文才有。所以必须按栏目 id 取列表，
  不能靠标题过滤。
- 涉及年报跨年时（如 12月末至次年1月初）按发布月推断年份，见
  :func:`parse_observation_end`。
- 接口未公开文档。改版会导致 :class:`ValueError`，是**响亮的失败**而不是静默空数据。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from newsagg.logutil import get_logger
from newsagg.market.fetchers.base import MarketFetcher, build_client
from newsagg.market.models import MarketPoint

__all__ = [
    "CECIFetcher",
    "parse_import_index",
    "parse_observation_end",
    "parse_period",
    "parse_pmi",
    "parse_weekly_report",
]

log = get_logger("market.ceci")

#: 站内接口基址。``ms-mcms`` 是后端的应用前缀，``mcms`` 是上下文路径。
API_BASE = "https://pjzx.cec.org.cn/ms-mcms/mcms"

#: ``CECI周报`` 栏目 id。取自 ``/clumnList.do`` 返回的栏目树
#: （300 CECI电煤指数 → 724 CECI周报）。
WEEKLY_REPORT_CATEGORY = 724

#: 该站的接口会校验来源页，缺 Referer 会被拒（或返回空列表）。
SITE_REFERER = "https://pjzx.cec.org.cn/"

#: 北京时间。不用 ``ZoneInfo`` 是因为 Windows 上可能缺 tzdata，
#: 而这里的偏移量是恒定的（中国不实行夏令时）。
_CST = timezone(timedelta(hours=8))

_TAG_RE = re.compile(r"<[^>]+>")

#: 期次，如 ``2026年第34期``。
_PERIOD_RE = re.compile(r"[（(]\s*(\d{4}年第\d+期)\s*[）)]")

#: 观测周，如 ``本周（9月10日至9月17日，下同）``。
_WEEK_RE = re.compile(
    r"本周[（(]\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日至\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)

#: ``元/吨`` 在 HTML 里可能被标签或换行拆开，因此斜杠两侧允许空白。
#: 写死成 ``元/吨`` 会在换行时静默失配（见 ``tests/test_market_collectors.py``）。
_UNIT_PATTERN = r"元\s*/\s*吨"

#: 进口指数。两种历史句式都要吃下：
#:   ``CECI进口指数综合到岸标煤单价1209元/吨，较上期下降43元/吨，环比下降3.4%``
#:   ``CECI进口指数到岸标煤单价809元/吨，较上期上涨1元/吨，环比涨幅0.1%``（早期无「综合」）
#: 环比方向词的写法在「上涨/下降」与「涨幅/降幅」之间摇摆，必须都收。
_IMPORT_RE = re.compile(
    r"CECI进口指数(?:综合)?到岸标煤单价\s*(?P<value>[\d.]+)\s*" + _UNIT_PATTERN + r"\s*[，,]\s*"
    r"较上期(?P<dir>上涨|下降|持平)\s*(?P<change>[\d.]+)?\s*" + _UNIT_PATTERN + r"\s*[，,]\s*"
    r"环比(?P<pdir>上涨|下降|涨幅|降幅|持平)\s*(?P<pct>[\d.]+)?\s*%"
)

#: 采购经理人指数。``本期为49.37%，环比上升0.18个百分点``。
#: 中间允许夹一句「连续N期处于XX区间」之类的修饰，所以用非贪婪的 ``[^。]*?``。
_PMI_RE = re.compile(
    r"CECI采购经理人指数[^。]*?本期为\s*(?P<value>[\d.]+)\s*%\s*[，,]\s*"
    r"环比(?P<dir>上升|下降|持平)\s*(?P<change>[\d.]+)?\s*个百分点"
)

#: 方向词 → 符号。
_UP = {"上涨", "涨幅", "上升", "增加", "扩大"}
_DOWN = {"下降", "降幅", "下跌", "减少", "收窄"}


def html_to_text(html: str) -> str:
    """把接口返回的 ``articleContent`` 转成纯文本。

    只做剥标签与实体/全角空白归一——**不压缩所有空白**，
    否则「本周（9月10日至9月17日」这种跨标签拼接处的判断会变脆。
    中文全角空格 ``\\u3000`` 会干扰正则里的 ``\\s*``，必须换掉。
    """
    text = _TAG_RE.sub("", html)
    return (
        text.replace("&nbsp;", " ")
        .replace("\u3000", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def _signed(direction: str, magnitude: float | None) -> float | None:
    """把「上涨/下降 + 数值」合成带符号的数。``持平`` 或无数值 → 0/None。"""
    if magnitude is None:
        return 0.0 if direction == "持平" else None
    if direction in _UP:
        return abs(magnitude)
    if direction in _DOWN:
        return -abs(magnitude)
    return 0.0


def parse_period(text: str) -> str:
    """取周报期次，如 ``2026年第34期``；取不到返回空串。"""
    match = _PERIOD_RE.search(text)
    return match.group(1) if match else ""


def parse_observation_end(text: str, published: date) -> date:
    """取观测周的**结束日**。

    Args:
        text: 周报正文纯文本。
        published: 周报发布日，用来补年份——正文里只写月日，不写年。

    Returns:
        观测周结束日；解析不到时回落为 ``published``。

    Note:
        跨年要单独处理：12 月底发布的周报常写「本周（12月28日至1月3日）」，
        此时结束日的年份应比开始日**大 1**。反过来，1 月初发布的周报若写
        「本周（12月28日至1月3日）」，开始日的年份应比发布年**小 1**。
    """
    match = _WEEK_RE.search(text)
    if not match:
        return published

    start_month, start_day, end_month, end_day = (int(g) for g in match.groups())

    start_year = published.year
    # 发布在 1 月、观测周起点在 12 月（或更早）→ 起点属于上一年。
    if start_month > published.month:
        start_year -= 1

    end_year = start_year + (1 if end_month < start_month else 0)

    try:
        return date(end_year, end_month, end_day)
    except ValueError:
        log.warning("观测周日期非法：%s-%s-%s，回落为发布日", end_year, end_month, end_day)
        return published


def parse_import_index(
    text: str,
    *,
    price_date: date,
    period: str = "",
    source_url: str = "",
) -> MarketPoint | None:
    """提取 CECI 进口指数综合到岸标煤单价。"""
    match = _IMPORT_RE.search(text)
    if not match:
        return None
    change = _signed(match.group("dir"), float(match.group("change")) if match.group("change") else None)
    pct = _signed(match.group("pdir"), float(match.group("pct")) if match.group("pct") else None)
    return MarketPoint(
        dataset="ceci_import",
        region="CN",
        grade="到岸标煤单价",
        price_date=price_date,
        value=float(match.group("value")),
        unit="元/吨",
        change_value=change,
        change_pct=pct,
        period=period,
        source="cec",
        source_url=source_url,
        raw={"原文": match.group(0)},
    )


def parse_pmi(
    text: str,
    *,
    price_date: date,
    period: str = "",
    source_url: str = "",
) -> MarketPoint | None:
    """提取 CECI 采购经理人指数。

    注意口径：``value`` 与 ``change_value`` 单位不同——指数本身是 ``%``，
    而环比是**百分点**（不是百分数）。这里把 ``change_value`` 记成百分点，
    ``change_pct`` 留空，并在 ``raw`` 里写明单位，避免下游把 0.18 当成 0.18% 用。
    """
    match = _PMI_RE.search(text)
    if not match:
        return None
    change = _signed(match.group("dir"), float(match.group("change")) if match.group("change") else None)
    return MarketPoint(
        dataset="ceci_pmi",
        region="CN",
        grade="综合",
        price_date=price_date,
        value=float(match.group("value")),
        unit="%",
        change_value=change,
        change_pct=None,
        period=period,
        source="cec",
        source_url=source_url,
        raw={"原文": match.group(0), "change_unit": "个百分点"},
    )


def parse_weekly_report(
    text: str,
    *,
    published: date,
    source_url: str = "",
) -> list[MarketPoint]:
    """解析一份 CECI 周报正文。

    Args:
        text: 正文。可以是纯文本，也可以是对应的 HTML（内部会先转纯文本）。
        published: 周报发布日。
        source_url: 出处 URL。

    Returns:
        能提取到的数据点（进口指数、采购经理人指数）。

    Raises:
        ValueError: 一条指标都提不出来。这通常意味着句式变了——
            静默返回空列表会让「真的没发布」和「解析器坏了」无法区分。
    """
    plain = html_to_text(text) if "<" in text else text
    price_date = parse_observation_end(plain, published)
    period = parse_period(plain)

    points: list[MarketPoint] = []
    import_point = parse_import_index(
        plain, price_date=price_date, period=period, source_url=source_url
    )
    if import_point is None:
        # 进口指数偶因假期停发（官网有《国庆节期间暂停发布CECI各指数的公告》），
        # 所以这里只告警不抛错；但它是本模块的**主指标**，消失必须留痕。
        log.warning("本期周报未匹配到 CECI 进口指数句式（期次 %s）", period or "未知")
    else:
        points.append(import_point)

    pmi_point = parse_pmi(plain, price_date=price_date, period=period, source_url=source_url)
    if pmi_point is not None:
        points.append(pmi_point)

    if not points:
        raise ValueError(
            "CECI 周报正文解析出 0 条指标——句式可能已改版；"
            "请检查 _IMPORT_RE / _PMI_RE 是否仍匹配官网措辞"
        )
    return points


class CECIFetcher(MarketFetcher):
    """CECI（中国电煤采购价格指数）采集器。

    命名注意：官网同时有 **CECI**（电煤价格指数）与 **CEGI**（发电天然气价格指数）
    两个指数族，本类负责前者。
    """

    dataset = "ceci_import"
    source = "cec"

    def _make_client(self) -> httpx.Client:
        """站点接口会校验来源页，必须带 ``Referer``，否则列表可能返回空。"""
        return build_client(headers={"Referer": SITE_REFERER})

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """打一次接口并校验业务状态。"""
        response = self.client.get(f"{API_BASE}{path}", params=params)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:  # 返回了 HTML（多半被拦）而不是 JSON
            raise ValueError(
                f"CECI 接口 {path} 返回的不是 JSON（HTTP {response.status_code}，"
                f"{len(response.content)} 字节）——可能被风控拦截或接口已迁移"
            ) from exc
        if not payload.get("success"):
            raise ValueError(f"CECI 接口 {path} 业务失败：{payload.get('msg')!r}")
        return payload["data"]

    def list_reports(self, limit: int = 10) -> list[dict[str, Any]]:
        """列出最近的周报（新→旧）。

        Returns:
            每项含 ``articleID`` / ``basicTitle`` / ``published``(date) / ``url``。

        Raises:
            ValueError: 列表为空——栏目 id 变了或接口改版。
        """
        data = self._get_json(
            "/content/list",
            {
                "id": WEEKLY_REPORT_CATEGORY,
                "pageNumber": 1,
                "pageSize": max(1, limit),
            },
        )
        reports: list[dict[str, Any]] = []
        for item in data.get("list") or []:
            article_id = item.get("articleID")
            if article_id is None:
                continue
            published = datetime.fromtimestamp(item["publicTime"] / 1000, tz=_CST).date()
            reports.append(
                {
                    "articleID": article_id,
                    "basicTitle": (item.get("basicTitle") or "").strip(),
                    "published": published,
                    "url": f"https://pjzx.cec.org.cn/detail/index.html?3-{article_id}",
                }
            )
        if not reports:
            raise ValueError(
                f"CECI 周报列表为空（category={WEEKLY_REPORT_CATEGORY}）——"
                "栏目 id 或接口结构可能已变"
            )
        return reports

    def fetch_report(self, article_id: int, *, published: date | None = None) -> list[MarketPoint]:
        """抓并解析单篇周报。"""
        detail = self._get_json("/content/detail", {"id": article_id})
        html = detail.get("articleContent") or ""
        if published is None:
            published = datetime.fromtimestamp(detail["publicTime"] / 1000, tz=_CST).date()
        url = f"https://pjzx.cec.org.cn/detail/index.html?3-{article_id}"
        return parse_weekly_report(html, published=published, source_url=url)

    def fetch(self, *, limit: int = 1) -> list[MarketPoint]:
        """抓最近 ``limit`` 期周报。

        Args:
            limit: 抓几期。默认 ``1``（只要最新一期）；回补历史时调大。

        Returns:
            去重后的数据点列表，按周报由新到旧排列。
        """
        if limit < 1:
            raise ValueError("limit 必须 >= 1")

        points: list[MarketPoint] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for report in self.list_reports(limit=limit):
            try:
                batch = self.fetch_report(
                    report["articleID"], published=report["published"]
                )
            except ValueError as exc:
                # 单期解析失败不该拖垮整轮回补，但必须记下来。
                log.error("周报 %s（%s）解析失败：%s", report["articleID"], report["basicTitle"], exc)
                continue
            for point in batch:
                if point.key in seen:
                    continue
                seen.add(point.key)
                points.append(point)

        if not points:
            raise ValueError(f"最近 {limit} 期 CECI 周报一条指标都没解析出来")

        log.info(
            "CECI 采集完成：%d 期 → %d 条（%s）",
            limit,
            len(points),
            "、".join(sorted({p.dataset for p in points})),
        )
        return points


def iter_datasets() -> Iterable[str]:
    """本采集器会产出的数据集标识。供 CLI 做命令行校验。"""
    return ("ceci_import", "ceci_pmi")
