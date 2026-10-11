"""LNG 现价采集器：重庆石油天然气交易中心「中国液化天然气价格(CLP)」。

数据源
------
``https://www.chinacqpgx.com/cyl?type=1&dt=YYYY-MM-DD``

CLP 由重庆石油天然气交易中心编制，以该中心线上交易数据与市场价格数据为依据，
反映**地区 LNG 工厂和接收站销售价格**的变化情况，日度价格每工作日发布，
覆盖全国及 24 个区域（川渝、广东、天津、上海、江苏……）。

为什么选 CLP 作为 LNG 现价的来源
--------------------------------
1. **日度 + 带日期参数**：``dt=`` 允许按日回补历史，不必依赖「只抓当天」这种
   脆弱增量。回补能力决定了数据能不能追溯，而可追溯是行情数据的地基。
2. **静态 HTML**：数据在 ``<table id="priceTbl" data-dt="...">`` 里，无需浏览器，
   采集成本极低（对比 SHPGX 官网是 JS 页面，得开 CDP）。
3. **覆盖分省**：分级口径刚好能和电力现货价的省份维度对齐，便于做
   「LNG vs 电价」「进口煤 vs 电价」这类跨数据集比价。

**注意：这是一个两段式站点。**
同一个 URL ``/cyl``，带不带 ``dt`` 返回的是**两种完全不同的页面**（Thymeleaf
服务端模板在查不到日期时报「列表页」）：

- 不带 ``dt`` → **发布日期列表页**（无 ``<table>``，只有
  ``<span class="media-time">2026-10-10</span><a href="/cyl?type=1&dt=...">``）；
- 带 ``dt``   → **价格表页**（含 ``<table id="priceTbl">``）。

所以 :meth:`CLPFetcher.fetch` 在 ``dt=None`` 时**必须先取列表页拿到最新发布日期**，
再带 ``dt`` 去抓价格表。直接拿列表页喂 :func:`parse_clp` 会抛「未找到价格表」。

已知边界
--------
- SHPGX 的「中国 LNG 出厂价格指数」权威性更高，但发布在微信公众号与 JS 官网，
  需浏览器渲染。本采集器先用 CLP 把链路跑通，SHPGX 留作后续增强。
- 区域名带括号注释（如 ``川渝（CYLNG）``），归一化时要去掉。
- 列表页每页只有 12 条，还不支持翻页参数；因此 ``days > 12`` 会被截断（记警告）。
"""

from __future__ import annotations

import re
from datetime import date

import httpx

from newsagg.logutil import get_logger
from newsagg.market.fetchers.base import MarketFetcher
from newsagg.market.models import MarketPoint

__all__ = ["CLPFetcher", "parse_clp", "parse_release_dates"]

log = get_logger("market.lng")

_BASE_URL = "https://www.chinacqpgx.com/cyl"
_UNIT = "元/吨"

#: 列表页一页最多给出的发布日期条数，用于对 ``days`` 做上限提醒。
LIST_PAGE_CAPACITY = 12

#: 表格定位：``id="priceTbl"``，日期在同一个起始标签的 ``data-dt`` 上。
_TABLE_RE = re.compile(r'<table[^>]*id="priceTbl".*?</table>', re.S)
_DATE_RE = re.compile(r'data-dt="(\d{4}-\d{2}-\d{2})"')

#: 列表页日期项：``<span class="media-time">DATE</span>`` 紧跟同一天的价格表链接。
#: 两者**成对匹配**（而不是只抓 ``media-time``）是刻意的——页面上另有「时间导航」
#: 之类的地方也会出现日期文本，只抓 ``media-time`` 更稳，但要求同一项里两处一致
#: 就能顺带证明「这一条确实是一条可点开的价格公告」。
_RELEASE_RE = re.compile(
    r'<span class="media-time">\s*(\d{4}-\d{2}-\d{2})\s*</span>\s*'
    r'<a[^>]+href="/cyl\?type=1&amp;dt=(\d{4}-\d{2}-\d{2})"',
    re.S,
)

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_BRACKET_RE = re.compile(r"[（(].*?[)）]")
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")

#: 区域名归一化：全国口径统一成 CN，与其它数据集保持同一套 region 约定。
_REGION_ALIAS = {"全国": "CN", "中国": "CN"}


def _text(raw: str) -> str:
    """剥掉标签与空白，拿到单元格文字。"""
    return _TAG_RE.sub("", raw).replace("&nbsp;", " ").strip()


def _num(raw: str) -> float | None:
    """从单元格文字里取数字。

    行情表里的占位符五花八门（``-`` / ``—`` / ``–`` / 空），
    它们表示「本期无数据」而不是 0——**必须返回 None**，
    写成 0 会在后续算涨跌幅时产生假的暴跌。
    """
    text = _text(raw).replace(",", "")
    if not text or text in {"-", "—", "–", "/", "N/A", "n/a"}:
        return None
    match = _NUM_RE.search(text)
    return float(match.group(0)) if match else None


def _region(raw: str) -> str:
    """区域名归一化：去括号注释、映射全国口径。"""
    name = _BRACKET_RE.sub("", _text(raw)).strip()
    return _REGION_ALIAS.get(name, name)


def parse_release_dates(html: str) -> list[date]:
    """从**列表页**解析可用发布日期（保持页面顺序，即倒序=最新在前）。

    解析时校验 ``media-time`` 与链接里的 ``dt`` 是否一致：不一致说明页面结构
    变了或者某项是残缺的，这种项直接丢弃，比默默采用其中一个更安全。

    Args:
        html: 列表页原始 HTML。

    Returns:
        去重后的发布日期列表；页面结构不符时返回空列表（由调用方决定如何报错）。
    """
    dates: list[date] = []
    seen: set[str] = set()
    for shown, linked in _RELEASE_RE.findall(html):
        if shown != linked or shown in seen:
            continue
        try:
            dates.append(date.fromisoformat(shown))
        except ValueError:  # 形如 2026-13-45 的脏数据
            continue
        seen.add(shown)
    return dates


def parse_clp(html: str, *, source_url: str = "") -> list[MarketPoint]:
    """解析 **CLP 价格表页**。

    Args:
        html: 价格表页原始 HTML（必须带过 ``dt`` 参数，见模块文档）。
        source_url: 数据出处 URL，写入数据点便于回溯。

    Returns:
        每个区域一个 :class:`MarketPoint`。

    Raises:
        ValueError: 页面上找不到价格表，或表格里没有任何有效行。
            这里**故意抛错而不是返回空列表**——静默返回空会让
            「页面改版」和「今天真的没发布」变得无法区分。
    """
    table = _TABLE_RE.search(html)
    if not table:
        raise ValueError(
            "未找到 CLP 价格表（id=priceTbl）——"
            "请确认抓的是价格表页（带 dt 参数）而不是日期列表页；页面也可能已改版"
        )

    table_html = table.group(0)
    date_match = _DATE_RE.search(table_html)
    if not date_match:
        raise ValueError("CLP 价格表缺少 data-dt 日期属性")
    price_date = date.fromisoformat(date_match.group(1))

    body = re.search(r"<tbody>(.*?)</tbody>", table_html, re.S)
    if not body:
        raise ValueError("CLP 价格表缺少 tbody")

    points: list[MarketPoint] = []
    for row in _ROW_RE.findall(body.group(1)):
        cells = _CELL_RE.findall(row)
        if len(cells) < 4:
            continue  # 表头残留或空行
        region = _region(cells[0])
        if not region:
            continue
        value = _num(cells[1])
        if value is None:
            continue
        points.append(
            MarketPoint(
                dataset="lng_spot",
                region=region,
                grade="CLP",
                price_date=price_date,
                value=value,
                unit=_UNIT,
                change_value=_num(cells[2]),
                change_pct=_num(cells[3]),
                source="cqpgx",
                source_url=source_url,
                raw={
                    "区域原文": _text(cells[0]),
                    "涨跌原文": _text(cells[2]),
                    "幅度原文": _text(cells[3]),
                },
            )
        )

    if not points:
        raise ValueError("CLP 价格表解析出 0 行有效数据")
    return points


class CLPFetcher(MarketFetcher):
    """LNG 现价（中国液化天然气价格 CLP）采集器。"""

    dataset = "lng_spot"
    source = "cqpgx"

    # ---------------------------------------------------------------- 内部

    def _get(self, params: dict[str, str]) -> httpx.Response:
        """发一次请求并统一处理编码/状态码。"""
        response = self.client.get(_BASE_URL, params=params)
        response.raise_for_status()
        # 服务端返回 UTF-8 但没在 header 里声明时，httpx 会猜成 ISO-8859-1，
        # 中文区域名会变成乱码。显式兜底成 utf-8。
        if not response.encoding or response.encoding.lower() in {"iso-8859-1", "ascii"}:
            response.encoding = "utf-8"
        return response

    def _fetch_one(self, dt: date) -> list[MarketPoint]:
        """抓某个确定日期的价格表。"""
        response = self._get({"type": "1", "dt": dt.isoformat()})
        return parse_clp(response.text, source_url=str(response.url))

    # ---------------------------------------------------------------- 公开

    def available_dates(self) -> list[date]:
        """取列表页上的可用发布日期（最新在前）。

        Raises:
            ValueError: 列表页一条日期都解析不出来。
        """
        response = self._get({"type": "1"})
        dates = parse_release_dates(response.text)
        if not dates:
            raise ValueError("CLP 日期列表页解析出 0 条发布日期——页面结构可能已改版")
        return dates

    def fetch(self, dt: date | None = None, *, days: int = 1) -> list[MarketPoint]:
        """抓取 CLP 价格。

        Args:
            dt: 指定业务日期。给了就只抓这一天（回补历史用）。
            days: ``dt`` 为 ``None`` 时抓最近多少个发布日；默认 ``1``（只要最新）。
                实际条数受列表页容量 :data:`LIST_PAGE_CAPACITY` 限制。

        Returns:
            各区域的数据点列表，按日期从新到旧、区域按页面顺序排列。
            跨天采集时按唯一键去重（同一天同一区域只保留一条）。

        Raises:
            ValueError: 列表页/价格表解析失败；或指定的 ``dt`` 当天没有发布。
        """
        if days < 1:
            raise ValueError("days 必须 >= 1")

        if dt is not None:
            targets = [dt]
        else:
            targets = self.available_dates()[:days]
            if days > LIST_PAGE_CAPACITY:
                log.warning(
                    "请求最近 %d 天，但列表页只给出 %d 条，已截断",
                    days,
                    LIST_PAGE_CAPACITY,
                )

        points: list[MarketPoint] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for target in targets:
            for point in self._fetch_one(target):
                if point.key in seen:  # 理论上不会撞，但去重成本极低，防上游重复
                    continue
                seen.add(point.key)
                points.append(point)

        dates = sorted({p.price_date for p in points})
        log.info(
            "CLP 采集完成：%s 共 %d 个区域 %d 条",
            "/".join(d.isoformat() for d in dates),
            len({p.region for p in points}),
            len(points),
        )
        return points
