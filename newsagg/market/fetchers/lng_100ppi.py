"""国内 LNG 现价采集器：生意社液化天然气频道（``trq.100ppi.com``）。

数据源
------
============  ===========================================================
用途            URL
============  ===========================================================
动态列表页      ``https://trq.100ppi.com/news/list---1.html``（``---N`` 翻页）
报价动态正文    ``https://www.100ppi.com/news/detail-YYYYMMDD-NNNNNNN.html``
============  ===========================================================

为什么需要这个源（与重庆 CLP 的分工）
-------------------------------------
``lng_cqpgx`` 抓的 CLP 是**分区域**的出厂价（川渝/广东/天津…），适合看区域价差；
生意社这条线抓的是**全国口径的基准价 + 企业报价区间**，两者互补：

- ``lng_spot`` / ``grade='CLP'``            分区域出厂价（source=cqpgx）
- ``lng_spot`` / ``grade='生意社基准价'``    全国基准价，日度（source=100ppi）
- ``lng_spot`` / ``grade='企业报价·均价'``   当日报价样本的算术均值/最高/最低
- ``lng_signal`` / ``grade='生意社均差'``    均差法信号（10日均线 − 20日均线）

**同一个 ``lng_spot`` 数据集里放两个来源**，正是 ``uk_market_point`` 把 ``source``
并入唯一键的意义所在——同名指标可以来自不同机构，各自独立成行，便于交叉验证。

关于「安全检查」页
------------------
``100ppi.com`` 全部域名会先返回一个 ~660 字节的 JS 挑战页::

    (function(_0x1){ var _0x2 = "56d6cbb6..."; ...
        document.cookie = "HW_CHECK" + "=" + _0x2 + "; ..."
        setTimeout(function(){ location.href = location.href; }, ...)
    })("v_dbf12329");

它只要求**把值写进 cookie 再重放请求**，不含工作量证明、不含指纹校验，
所以不需要浏览器：解析出 cookie 名与值、设进会话、重发一次即可。
:meth:`PPIFetcher._get` 就是这么做的，且**带了「重放后仍是挑战页就报错」的兜底**，
避免在页面逻辑变化时陷入无限重试。

关于数据形态
------------
生意社把每天的基准价与均差**发表在新闻标题里**：

- ``10月9日生意社液化天然气基准价为6152.00元/吨``
- ``生意社液化天然气10月6日均差为-127.70元/吨 由负向扩大转为缩小``

所以**一页列表就能取到十几天的序列，不必逐篇抓正文**。
唯一的例外是企业报价：报价明细在正文的 ``<ul class='pn_text'>`` 表格里，
需要逐篇抓。

已知边界
--------
- **标题只写月日、不写年**。年份一律以 URL 里的 ``detail-YYYYMMDD-`` 为准，
  并校验与标题里的月日一致——不一致说明列表结构变了，该条直接丢弃。
- ``均差`` 是**派生信号**（10 日均线减 20 日均线），不是价格，所以单独放进
  ``lng_signal`` 数据集。混进 ``lng_spot`` 会让 ``AVG(value) GROUP BY dataset``
  这类聚合被负值污染。
- 企业报价样本全部来自**液厂**（国产气），**不含接收站**。
  因此 ``企业报价·均价`` 不等于「全市场均价」，raw 里写明了样本数与交货地。
  接收站均价只在不定期的行情分析正文里出现，没有稳定字段，**故意不抓**——
  它没有独立的观测日，硬抓会把「分析文章发布日」当成「行情日期」，
  犯的正是本模块开头批评的那种日期错位。
- 报价动态每篇正文可能列出若干条交易商报价（当前样本 5 条左右）。
"""

from __future__ import annotations

import re
from datetime import date

import httpx

from newsagg.logutil import get_logger
from newsagg.market.fetchers.base import MarketFetcher, build_client
from newsagg.market.models import MarketPoint

__all__ = ["PPIFetcher", "parse_news_list", "parse_quote_table"]

log = get_logger("market.ppi")

_BASE = "https://trq.100ppi.com"
_LIST_URL = _BASE + "/news/list---{page}.html"

_UNIT = "元/吨"
_REGION = "CN"

#: 一页列表大约给出 40 条新闻，其中约 15 条是基准价。翻页上限用于防呆。
_MAX_PAGES = 5

#: 安全检查页的标记与体积上限。真实频道页 ≥ 20 KB，挑战页约 660 B。
_CHALLENGE_MARKER = "HW_CHECK"
_CHALLENGE_MAX_BYTES = 2500
_CHALLENGE_VALUE_RE = re.compile(r'_0x2\s*=\s*"([0-9a-fA-F]+)"')
_CHALLENGE_COOKIE_RE = re.compile(r'document\.cookie\s*=\s*"([A-Za-z_][A-Za-z0-9_]*)"')

_TAG_RE = re.compile(r"<[^>]+>")

#: 列表项：``<a href="https://www.100ppi.com/news/detail-20261009-6313944.html">标题</a>``
#: 顺便把 URL 里的年月日抠出来当作**权威日期**。
_LIST_ITEM_RE = re.compile(
    r'href="https://www\.100ppi\.com/news/detail-(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})'
    r'-\d+\.html"[^>]*>(?P<title>.*?)</a>',
    re.S,
)

#: ``10月9日生意社液化天然气基准价为6152.00元/吨``
_BENCHMARK_RE = re.compile(
    r"(?P<m>\d{1,2})月(?P<d>\d{1,2})日生意社液化天然气基准价为\s*(?P<v>[\d.]+)\s*元/吨"
)

#: ``生意社液化天然气10月6日均差为-127.70元/吨 由负向扩大转为缩小``
#: ``生意社液化天然气9月21日均差继续正向缩小为125.40元/吨``
#: ``生意社液化天然气9月27日均线下穿 均差为-16.70元/吨``
#: 三种写法中间夹的字各不相同，用 ``均差…为`` 的宽松窗口统一吃掉。
_MEANDIFF_RE = re.compile(
    r"生意社液化天然气(?P<m>\d{1,2})月(?P<d>\d{1,2})日[^，。]*?均差[^，。]*?为\s*"
    r"(?P<v>-?[\d.]+)\s*元/吨"
)

#: 报价动态标题：``液化天然气商品报价动态（2026-10-09）``
_QUOTE_TITLE_RE = re.compile(r"液化天然气商品报价动态")

#: 报价表格：每个 ``<li class='pn_data'>`` 一行，行内 4 个 ``<span>``。
_QUOTE_ROW_RE = re.compile(r"<li\s+class=['\"]pn_data['\"]>(.*?)</li>", re.S)
_SPAN_RE = re.compile(r"<span[^>]*>(.*?)</span>", re.S)
_QUOTE_VALUE_RE = re.compile(r"([\d.]+)\s*元/吨")


def _text(raw: str) -> str:
    """剥标签 + 归一空白（含全角空格，否则 ``\\s*`` 会漏）。"""
    return re.sub(r"\s+", " ", _TAG_RE.sub("", raw).replace("&nbsp;", " ").replace("\u3000", " ")).strip()


def parse_news_list(html: str) -> list[dict[str, object]]:
    """解析 LNG 频道动态列表页。

    只挑出两类条目：**基准价**与**均差**。其余（报价动态、PriceSeek 提醒、
    行情观点等）由调用方按需另行处理。

    Args:
        html: 列表页 HTML。

    Returns:
        每项含 ``kind``（``benchmark`` / ``meandiff``）、``price_date``、
        ``value``、``title``、``url``。按页面顺序（新→旧）排列。
    """
    items: list[dict[str, object]] = []
    for match in _LIST_ITEM_RE.finditer(html):
        title = _text(match.group("title"))
        try:
            url_date = date(
                int(match.group("y")), int(match.group("m")), int(match.group("d"))
            )
        except ValueError:
            continue

        benchmark = _BENCHMARK_RE.search(title)
        if benchmark and int(benchmark.group("m")) == url_date.month and int(
            benchmark.group("d")
        ) == url_date.day:
            items.append(
                {
                    "kind": "benchmark",
                    "price_date": url_date,
                    "value": float(benchmark.group("v")),
                    "title": title,
                    "url": _article_url(match.group(0)),
                }
            )
            continue

        meandiff = _MEANDIFF_RE.search(title)
        if meandiff and int(meandiff.group("m")) == url_date.month and int(
            meandiff.group("d")
        ) == url_date.day:
            items.append(
                {
                    "kind": "meandiff",
                    "price_date": url_date,
                    "value": float(meandiff.group("v")),
                    "title": title,
                    "url": _article_url(match.group(0)),
                }
            )
    return items


def _article_url(anchor_html: str) -> str:
    """从匹配到的 ``<a>`` 片段里取出完整文章 URL。"""
    match = re.search(r'href="(https://[^"]+\.html)"', anchor_html)
    return match.group(1) if match else ""


def parse_quote_table(html: str) -> list[dict[str, object]]:
    """解析「液化天然气商品报价动态」正文里的报价表格。

    表格结构::

        <li class='pn_text'>
          <li class='pn_nav'>  交易商 | 品牌/产地 | 交货地 | 最新报价
          <ul class='pn_name'> 液化天然气　类别:液化天然气
          <li class='pn_data'><span>内蒙古汇能…</span><span>内蒙汇能</span>
                              <span>内蒙古</span><span>6200元/吨</span></li>
          ...

    Returns:
        每项含 ``trader`` / ``brand`` / ``place`` / ``price``。
    """
    rows: list[dict[str, object]] = []
    for row_html in _QUOTE_ROW_RE.findall(html):
        spans = [_text(s) for s in _SPAN_RE.findall(row_html)]
        if len(spans) < 4:
            continue
        match = _QUOTE_VALUE_RE.search(spans[3])
        if not match:
            continue
        rows.append(
            {
                "trader": spans[0],
                "brand": spans[1],
                "place": spans[2],
                "price": float(match.group(1)),
            }
        )
    return rows


class PPIFetcher(MarketFetcher):
    """生意社液化天然气采集器（国内 LNG 现价）。

    产出两个数据集：

    - ``lng_spot``：``生意社基准价``、``企业报价·最高/最低/均价``
    - ``lng_signal``：``生意社均差``
    """

    dataset = "lng_spot"
    source = "100ppi"

    def _make_client(self) -> httpx.Client:
        """该站对 UA 有基本要求；带浏览器 UA 更容易直接拿到真实页面。"""
        return build_client(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                )
            }
        )

    # ---------------------------------------------------------------- 传输

    @staticmethod
    def _is_challenge(response: httpx.Response, text: str) -> bool:
        return _CHALLENGE_MARKER in text and len(response.content) <= _CHALLENGE_MAX_BYTES

    def _solve_challenge(self, response: httpx.Response, text: str) -> bool:
        """把挑战页里的值写进 cookie。成功返回 True。"""
        value_match = _CHALLENGE_VALUE_RE.search(text)
        cookie_match = _CHALLENGE_COOKIE_RE.search(text)
        if not value_match or not cookie_match:
            return False
        name, value = cookie_match.group(1), value_match.group(1)
        # 按父域写入，让 trq.* 与 www.* 共用同一张通行证，少一次往返。
        host = response.url.host
        parent = "." + ".".join(host.split(".")[-2:])
        self.client.cookies.set(name, value, domain=parent)
        self.client.cookies.set(name, value, domain=host)
        log.debug("生意社安全检查已通过（host=%s, cookie=%s）", host, name)
        return True

    def _get(self, url: str) -> str:
        """带安全检查自动通过的 GET。

        Raises:
            ValueError: 重放后仍是挑战页——说明校验逻辑变了，
                这时**必须报错**而不是返回那段 660 字节的占位 HTML
                （否则下游只会看到「解析出 0 条」这种无信息量的失败）。
        """
        response = self.client.get(url)
        response.raise_for_status()
        text = response.content.decode("utf-8", "ignore")

        if self._is_challenge(response, text):
            if not self._solve_challenge(response, text):
                raise ValueError(
                    f"生意社安全检查页结构已变，无法自动通过：{url}"
                    "（未能从挑战页解析出 cookie 名与值）"
                )
            response = self.client.get(url)
            response.raise_for_status()
            text = response.content.decode("utf-8", "ignore")
            if self._is_challenge(response, text):
                raise ValueError(
                    f"生意社安全检查无法通过（重放后仍是挑战页）：{url}"
                )
        return text

    # ---------------------------------------------------------------- 抓取

    def _scan_lists(self, *, pages: int, quote_limit: int) -> tuple[
        list[dict[str, object]], list[tuple[date, str]]
    ]:
        """**一次**扫完列表页，同时取出指标条目与报价动态文章链接。

        指标与报价文章散落在同一张列表里，分两次请求纯属浪费——而且列表页
        每翻一页都要过一次安全检查，重复请求会让往返次数直接翻倍。
        所以这里做单次扫描，两个结果一起返回。

        Returns:
            ``(items, quote_articles)``：
            items 是基准价/均差条目，quote_articles 是 ``(发布日期, URL)`` 列表。
        """
        items: list[dict[str, object]] = []
        seen_items: set[tuple[str, str]] = set()
        articles: list[tuple[date, str]] = []
        seen_urls: set[str] = set()

        for page in range(1, max(1, min(pages, _MAX_PAGES)) + 1):
            html = self._get(_LIST_URL.format(page=page))
            for item in parse_news_list(html):
                key = (str(item["kind"]), str(item["price_date"]))
                if key in seen_items:
                    continue
                seen_items.add(key)
                items.append(item)

            if len(articles) >= quote_limit:
                continue
            for match in _LIST_ITEM_RE.finditer(html):
                if not _QUOTE_TITLE_RE.search(_text(match.group("title"))):
                    continue
                url = _article_url(match.group(0))
                if not url or url in seen_urls:
                    continue
                try:
                    published = date(
                        int(match.group("y")), int(match.group("m")), int(match.group("d"))
                    )
                except ValueError:
                    continue
                seen_urls.add(url)
                articles.append((published, url))
                if len(articles) >= quote_limit:
                    break

        articles.sort(key=lambda pair: pair[0], reverse=True)
        return items, articles[:quote_limit]

    def fetch_quote_rows(self, url: str) -> list[dict[str, object]]:
        """抓一篇报价动态并返回报价行。"""
        return parse_quote_table(self._get(url))

    # ---------------------------------------------------------------- 公开

    def fetch(self, *, days: int = 1) -> list[MarketPoint]:
        """抓最近的国内 LNG 现价。

        Args:
            days: 基准价取最近多少天；企业报价按同样天数抓对应篇数的报价动态。

        Returns:
            数据点列表。含 ``lng_spot``（基准价 + 企业报价区间）与
            ``lng_signal``（均差）。

        Raises:
            ValueError: 一条基准价都没解析出来——列表页结构可能已变。
        """
        if days < 1:
            raise ValueError("days 必须 >= 1")

        # 一页约 15 条基准价，按天数估页数，再 +1 页冗余。
        pages = max(1, min(_MAX_PAGES, (days // 12) + 1))
        items, quote_articles = self._scan_lists(pages=pages, quote_limit=days)

        benchmarks = [i for i in items if i["kind"] == "benchmark"]
        benchmarks.sort(key=lambda i: i["price_date"], reverse=True)  # type: ignore[arg-type]
        if not benchmarks:
            raise ValueError(
                "生意社 LNG 动态列表未解析出任何「基准价」条目——"
                "标题句式或列表结构可能已变"
            )

        points: list[MarketPoint] = []
        for item in benchmarks[:days]:
            points.append(
                MarketPoint(
                    dataset="lng_spot",
                    region=_REGION,
                    grade="生意社基准价",
                    price_date=item["price_date"],  # type: ignore[arg-type]
                    value=float(item["value"]),  # type: ignore[arg-type]
                    unit=_UNIT,
                    source=self.source,
                    source_url=str(item["url"]),
                    raw={
                        "标题": item["title"],
                        "说明": "基于价格大数据与生意社价格模型产生的交易指导价",
                    },
                )
            )

        for item in items:
            if item["kind"] != "meandiff":
                continue
            points.append(
                MarketPoint(
                    dataset="lng_signal",
                    region=_REGION,
                    grade="生意社均差",
                    price_date=item["price_date"],  # type: ignore[arg-type]
                    value=float(item["value"]),  # type: ignore[arg-type]
                    unit=_UNIT,
                    source=self.source,
                    source_url=str(item["url"]),
                    raw={"标题": item["title"], "口径": "10日均线 − 20日均线"},
                )
            )

        # 企业报价：每篇报价动态产出「最高/最低/均价」三个点。
        for published, url in quote_articles:
            rows = self.fetch_quote_rows(url)
            if not rows:
                log.warning("报价动态正文未解析出报价行：%s", url)
                continue
            prices = [float(r["price"]) for r in rows]
            common_raw = {
                "样本数": len(prices),
                "交货地": sorted({str(r["place"]) for r in rows}),
                "交易商": [str(r["trader"]) for r in rows],
                "口径": "生意社报价动态列出的液厂报价样本，不含接收站",
            }
            breakdown = {
                "企业报价·最高": max(prices),
                "企业报价·最低": min(prices),
                "企业报价·均价": round(sum(prices) / len(prices), 4),
            }
            for grade, value in breakdown.items():
                points.append(
                    MarketPoint(
                        dataset="lng_spot",
                        region=_REGION,
                        grade=grade,
                        price_date=published,
                        value=value,
                        unit=_UNIT,
                        source=self.source,
                        source_url=url,
                        raw=dict(common_raw),
                    )
                )

        dates = sorted({p.price_date.isoformat() for p in points}, reverse=True)
        log.info(
            "生意社采集完成：%d 条（%s）跨 %d 天（最新 %s）",
            len(points),
            "、".join(sorted({p.dataset for p in points})),
            len(dates),
            dates[0] if dates else "-",
        )
        return points
