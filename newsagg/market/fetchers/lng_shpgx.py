"""上海石油天然气交易中心（SHFGX）LNG 挂牌报价采集。

数据源
------
人工可见页面是 ``https://www.shpgx.com/html/LNGbjsj.html``（「LNG报价数据」），
表格本身是空壳，真正的数据由前端 ``/js/charts/lngprice.js`` 经
``/js/jquery.mypagination.js`` 的 ``gridpage()`` 拉取：

.. code-block:: text

    POST /eplatform/lngdata/report/listHis
    body: start / length / companyID / starttime / endtime / channelName / ts

响应是 ``{root: [...], totalProperty: N, start, length, current, code, msg}``，
行字段：``factoryId, startDate, saleTo, price, companyId, companyName, factoryName``。
``root`` 按 ``startDate`` **倒序**（实测 2026-08-01~10-11 共 1674 行为 71 个日期）。

踩过的两个坑
------------
1. **首页那张表是死的**。首页 ``<table id="pngexchangedata-dt"
   data-ajax-url="/exdata/pngexchangedata/pagelist.json">`` 里的路径直接请求
   返回 404（nginx），真正在用的是 ``listHis``。别照着 ``data-ajax-url`` 写。
2. **``listAll`` 不是 ``listHis`` 的别名**。``listAll`` 只返「当前有效报价」，
   而且**包含 ``price=null`` 的「暂不报价」行**——实测 2026-10-11 ``listAll`` 29 行
   vs ``listHis`` 25 行，多出的 4 行正是珠海接收站 4 个销售地尚未报价。
   拿 ``listAll`` 算均价会被 null 和未报价摊位稀释，所以只用 ``listHis``。

分页
----
服务端单页硬上限 **500**（``length`` 给 1000 / 2000 / 5000 都只回 500），
所以按 ``start += len(root)`` 翻页，直到 ``start >= totalProperty``。

两个数据集
----------
- ``lng_quote``：逐条挂牌明细。``region`` = 销售地，``grade`` = **交货点**
  （液厂 / 接收站名，如 ``宁波接收站``）。这里 ``grade`` 承载交货点是为了满足
  唯一键 ``(dataset, region, grade, price_date, source)``——同一天同一销售地会有
  多个交货点报价，不区分就会互相覆盖。
- ``lng_spot``：从当日明细汇总出的口径指标，``region`` 一律 ``CN``，
  ``grade`` 见 :data:`AGGREGATES`。

「接收站」怎么认
----------------
上游**没有**任何字段标明交货点类型，只能按名称判定。坑在于「北燃南港」
（北京燃气天津南港接收站）**不带**「接收站」后缀——用 ``"接收站" in name``
会把它误判成液厂。所以这里用显式名单 :data:`RECEIVE_STATIONS`，
名单取自 2026-08-01 ~ 2026-10-11 全量 1674 条明细的实际取值（共 18 个交货点，
其中接收站 8 个、液厂 10 个）。上游新增交货点时会落到"液厂"一侧，
``store`` 侧不会报错，但 ``RECEIVE_STATIONS`` 需要同步——这一点写在运行手册里。

环比
----
接口**不给**涨跌。当日更环比只能自己算：同一次 ``fetch`` 里若拿到 ≥2 个日期，
就按 ``(dataset, region, grade)`` 分组、按日期升序填 ``change_value`` /
``change_pct``。只抓 1 天时这两个字段留空——**不编造**。
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from newsagg.logutil import get_logger
from newsagg.market.fetchers.base import MarketFetcher, build_client
from newsagg.market.models import MarketPoint

__all__ = [
    "AGGREGATES",
    "RECEIVE_STATIONS",
    "SHFGXFetcher",
    "is_receive_station",
    "parse_quote_rows",
    "summarize_quotes",
]

log = get_logger("market")

#: 接口根地址。https 证书链正常，无需 ``verify=False``。
_BASE = "https://www.shpgx.com"

#: 明细接口。**POST**，参数走表单。
_LIST_HIS = _BASE + "/eplatform/lngdata/report/listHis"

#: 人工可核对页面。入库写进 ``source_url``——接口是 POST，没有可直接打开的 GET 链接，
#: 这一页是人能点开、能看到同一批数字的地方。
_PAGE_URL = _BASE + "/html/LNGbjsj.html"

#: 上游查 ``Referer``；表单里那个 ``channelName`` 也照抄，避免哪天开始校验。
_CHANNEL = "LNG报价数据"

#: 服务端单页硬上限。给更大的值也只回 500，写死避免误以为能一次拉完。
_PAGE_SIZE = 500

#: 翻页上限。2500 条 ≈ 100 个交易日 ≈ 5 个月明细，足够覆盖 ``--periods`` 的合理取值。
_MAX_PAGES = 5

#: 回溯窗口（自然日）。``fetch(days=1)`` 也要在这里面找"最近一个有数据的日期"，
#: 所以取得比 ``days`` 宽：实测 2026-10-10 整天没有挂牌（相邻的 10-09 / 10-11 都有）。
_LOOKBACK_DAYS = 200

#: 单位。挂牌报价统一按元/吨。
_UNIT = "元/吨"

#: ``market_prices.region`` / ``grade`` 都是 ``VARCHAR(64)``。超长说明上游换了
#: 命名方式（比如把公司全称塞进交货点），此时**跳过并告警**而不是截断——
#: 截断后的交货点会与另一个真实交货点撞名，比丢一条更糟。
_MAX_KEY_LEN = 64

#: 接收站名单。改这里前先跑 ``powernews market-fetch --dataset lng_quote --dry-run``
#: 看实际交货点取值；
#: 名单来源见模块 docstring「接收站怎么认」。
RECEIVE_STATIONS: frozenset[str] = frozenset(
    {
        "宁波接收站",
        "滨海接收站",
        "中石油如东接收站",
        "珠海接收站",
        "粤东接收站",
        "国网迭福接收站",
        "国网天津接收站",
        # 北京燃气天津南港接收站——名字里没有「接收站」，最容易漏。
        "北燃南港",
    }
)

#: 汇总口径：(grade, 取样范围, 归约函数)。
#:
#: 分「全部」与「接收站」两套，是因为两者价差很大且各有用途：全部口径含山西
#: 液厂的出厂价（2026-10-09 低至 5400），接收站口径是沿海到岸/槽批价（同日
#: 6460~7250）。混在一起算出来的"均价"没有业务含义。
AGGREGATES: tuple[tuple[str, str, Callable[[list[float]], float]], ...] = (
    ("挂牌·全部均价", "all", statistics.mean),
    ("挂牌·全部最高", "all", max),
    ("挂牌·全部最低", "all", min),
    ("挂牌·接收站均价", "receive", statistics.mean),
    ("挂牌·接收站最高", "receive", max),
    ("挂牌·接收站最低", "receive", min),
)


def is_receive_station(facility: str) -> bool:
    """判断交货点是不是接收站（按显式名单，不是字符串包含）。"""
    return facility in RECEIVE_STATIONS


def _to_float(value: Any) -> float | None:
    """把接口里的价格转成 float。``None`` / 空串 / ``"-"`` 都返回 ``None``。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {"-", "--", "~"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    """``startDate`` 是 ``YYYY-MM-DD``；解析不出来就丢弃该行。"""
    if not value:
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_quote_rows(payload: dict[str, Any]) -> list[MarketPoint]:
    """把 ``listHis`` 的响应体解析成 ``lng_quote`` 明细点。

    故意只吃已解析好的 ``dict``（不是 ``httpx.Response``），这样单测可以直接
    喂一份写死的响应，不需要网络也不用 MockTransport。

    Args:
        payload: 接口响应，形如 ``{"root": [...], "totalProperty": 25}``。

    Returns:
        明细点列表。以下行会被**跳过**（并记 warning）：
        ``price`` 缺失/非正值、``startDate`` 解析失败、``saleTo`` 或
        ``factoryName`` 为空或超过 :data:`_MAX_KEY_LEN`。
    """
    rows = payload.get("root") or []
    points: list[MarketPoint] = []
    skipped = 0

    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        price = _to_float(row.get("price"))
        price_date = _parse_date(row.get("startDate"))
        sale_to = str(row.get("saleTo") or "").strip()
        facility = str(row.get("factoryName") or "").strip()

        if price is None or price <= 0 or price_date is None or not sale_to or not facility:
            skipped += 1
            continue
        if len(sale_to) > _MAX_KEY_LEN or len(facility) > _MAX_KEY_LEN:
            log.warning(
                "交货点名/销售地超过 %d 字符，跳过：%r @ %r", _MAX_KEY_LEN, facility, sale_to
            )
            skipped += 1
            continue

        points.append(
            MarketPoint(
                dataset="lng_quote",
                region=sale_to,
                grade=facility,
                price_date=price_date,
                value=price,
                unit=_UNIT,
                source="shpgx",
                source_url=_PAGE_URL,
                raw={
                    "companyName": row.get("companyName"),
                    "factoryName": facility,
                    "factoryId": row.get("factoryId"),
                    "companyId": row.get("companyId"),
                    "saleTo": sale_to,
                },
            )
        )

    if skipped:
        log.debug("listHis 跳过 %d 行（空价/坏日期/超长键）", skipped)
    return points


def summarize_quotes(quotes: Iterable[MarketPoint]) -> list[MarketPoint]:
    """从明细汇总出 ``lng_spot`` 口径指标（按日期分组）。

    Args:
        quotes: :func:`parse_quote_rows` 产出的 ``lng_quote`` 明细点。
            这里靠 ``point.grade`` 识别交货点，所以**只能传 lng_quote 点**。

    Returns:
        每个有数据的日期、各口径一行；当日该口径无样本则不出行。
    """
    by_date: dict[date, list[MarketPoint]] = defaultdict(list)
    for point in quotes:
        if point.dataset != "lng_quote":
            raise ValueError(f"summarize_quotes 只接受 lng_quote 明细，收到 {point.dataset!r}")
        by_date[point.price_date].append(point)

    out: list[MarketPoint] = []
    for price_date, items in sorted(by_date.items()):
        for grade, scope, reducer in AGGREGATES:
            subset = [
                p
                for p in items
                if p.value is not None and (scope == "all" or is_receive_station(p.grade))
            ]
            if not subset:
                continue
            values = [float(p.value) for p in subset if p.value is not None]
            out.append(
                MarketPoint(
                    dataset="lng_spot",
                    region="CN",
                    grade=grade,
                    price_date=price_date,
                    value=round(float(reducer(values)), 2),
                    unit=_UNIT,
                    source="shpgx",
                    source_url=_PAGE_URL,
                    raw={"样本数": len(values), "口径": scope},
                )
            )
    return out


def fill_change(points: list[MarketPoint]) -> list[MarketPoint]:
    """按 ``(dataset, region, grade)`` 分组、日期升序补环比。

    接口不给涨跌，只能自己算。**只有拿到相邻两个日期才算**——只抓 1 天时
    ``change_value`` / ``change_pct`` 保持 ``None``，不编造。
    """
    groups: dict[tuple[str, str, str], list[MarketPoint]] = defaultdict(list)
    for point in points:
        groups[(point.dataset, point.region, point.grade)].append(point)

    out: list[MarketPoint] = []
    for series in groups.values():
        series.sort(key=lambda p: p.price_date)
        prev: MarketPoint | None = None
        for point in series:
            if prev is not None and point.value is not None and prev.value is not None:
                delta = round(float(point.value) - float(prev.value), 4)
                pct = round(delta / float(prev.value) * 100, 4) if float(prev.value) else None
                point = replace(point, change_value=delta, change_pct=pct)
            out.append(point)
            prev = point
    return out


class SHFGXFetcher(MarketFetcher):
    """上海石油天然气交易中心 LNG 挂牌报价。

    ``fetch()`` 同时产出两个数据集：``lng_quote``（明细）与 ``lng_spot``（汇总）。
    所以它在注册表里的主数据集是 ``lng_quote``，但同时被 ``lng_spot`` 复用。
    """

    dataset = "lng_quote"
    source = "shpgx"

    def _make_client(self) -> Any:
        # 上游对接口请求查 Referer；表单里那个 channelName 也一并带上。
        return build_client(
            headers={
                "Referer": _PAGE_URL,
                "X-Requested-With": "XMLHttpRequest",
            }
        )

    # ------------------------------------------------------------------
    # 单页
    # ------------------------------------------------------------------
    @staticmethod
    def _payload(start: int, length: int, start_date: date, end_date: date) -> dict[str, Any]:
        import time

        return {
            "start": start,
            "length": length,
            "companyID": "",
            "starttime": start_date.isoformat(),
            "endtime": end_date.isoformat(),
            "channelName": _CHANNEL,
            # 浏览器的防缓存参数。上游不校验，但少了它更容易撞 CDN 缓存。
            "ts": int(time.time() * 1000),
        }

    def _request(self, start: int, start_date: date, end_date: date) -> dict[str, Any]:
        response = self.client.post(
            _LIST_HIS,
            data=self._payload(start, _PAGE_SIZE, start_date, end_date),
        )
        response.raise_for_status()
        # 响应头是 application/json，但浏览器侧用的是 eval(text)，别依赖 Content-Type。
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"listHis 响应不是对象：{type(payload).__name__}")
        if payload.get("code") not in (0, None):
            raise ValueError(f"listHis 返回 code={payload.get('code')} msg={payload.get('msg')!r}")
        return payload

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------
    def fetch_quotes(self, start_date: date, end_date: date) -> list[MarketPoint]:
        """拉取 ``[start_date, end_date]``（闭区间）内的挂牌明细（自动翻页）。"""
        if start_date > end_date:
            raise ValueError(f"起止日期反了：{start_date} > {end_date}")

        collected: list[MarketPoint] = []
        offset = 0
        for _ in range(_MAX_PAGES):
            payload = self._request(offset, start_date, end_date)
            page = payload.get("root") or []
            collected.extend(parse_quote_rows({"root": page}))
            total = payload.get("totalProperty") or 0
            if not page:
                break
            offset += len(page)
            if offset >= total:
                break
        else:
            log.warning("listHis 翻页达到上限 %d 页，可能没拉完", _MAX_PAGES)
        return collected

    def fetch(self, *, days: int = 1) -> list[MarketPoint]:
        """抓最近 ``days`` 个**有挂牌数据的日期**。

        不是"最近 days 个自然日"：实测 2026-10-10 一整天没有挂牌记录，按自然日切
        会得到空窗口。做法是先从宽窗口 (:data:`_LOOKBACK_DAYS`) 里翻页，取出现过的
        日期倒序前 ``days`` 个，再只保留这些日期——与 ``CLPFetcher`` 取
        「最近 N 个可交易日」的口径一致。

        Args:
            days: 取几个最近的有数据日期（含当日）。

        Returns:
            明细 + 汇总 + 环比，一个列表。
        """
        window = max(int(days), 1)
        today = date.today()
        start_date = today - timedelta(days=_LOOKBACK_DAYS)

        wanted: set[date] = set()
        collected: list[MarketPoint] = []
        offset = 0
        for _ in range(_MAX_PAGES):
            payload = self._request(offset, start_date, today)
            page = payload.get("root") or []
            collected.extend(parse_quote_rows({"root": page}))
            seen = sorted({p.price_date for p in collected}, reverse=True)
            if len(seen) >= window or not page:
                wanted = set(seen[:window])
                break
            offset += len(page)
            if offset >= (payload.get("totalProperty") or 0):
                wanted = set(seen[:window])
                break

        quotes = [p for p in collected if p.price_date in wanted]
        if not quotes:
            return []
        return fill_change(quotes + summarize_quotes(quotes))
