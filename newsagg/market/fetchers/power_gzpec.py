"""市场电价采集器：南方区域电力现货市场每周行情（广州电力交易中心）。

数据源
------
``http://www.gzpec.cn/news/scyj/``（市场研究 → 市场行情）

列表页每页 10 条，翻页 ``index_1.html``、``index_2.html``……
文章 URL 形如 ``/news/scyj/202609/t20260911_37051.html``，
标题形如 ``南方区域电力现货市场每周行情一览（2026年8月31日-9月6日）``。
栏目里还混着「绿证交易每周行情一览」「结算月报」，**靠标题关键字过滤**。

一个必须注意的解析陷阱
----------------------
正文是 TRS 编辑器产出的 HTML，数字被 ``<font>`` 切碎::

    加权均价为<font face="Calibri">306</font><font face="宋体">元</font>
    <font face="Calibri">/</font><font face="宋体">兆瓦时

如果按「标签之间的文本块」逐块处理，会得到 ``306`` / ``元`` / ``/`` / ``兆瓦时``
四段互不相邻的碎片，``元/兆瓦时`` 永远匹配不上。
**正确做法是先剥掉全部标签再做正则**——拼回来的文本正是
``加权均价为306元/兆瓦时``。

可提取的指标
------------
一份周报里有三层，本采集器全部提取（都是 ``power_spot`` 数据集）：

1. **区域整体**：``region="南方区域"``，``grade`` = ``日前`` / ``实时``
2. **分省区**：``region`` = 广东/广西/云南/贵州/海南，``grade`` = ``日前`` / ``实时``
3. **分电源**：``region="南方区域"``，``grade`` = ``日前·煤机`` / ``实时·新能源`` 等

三层的 ``grade`` 命名不完全对称（分电源把电源类型编进 grade），原因是唯一键
``(dataset, region, grade, price_date, source)`` 里没有独立的「电源类型」列；
把电源类型并入 grade 比污染 region 或另开数据集都更小代价。电源类型同时写进
``raw``，需要时可直接从 JSON 取。

电量口径
--------
周报同时给出「日均成交电量（亿千瓦时）」与「加权均价（元/兆瓦时）」。
本数据集的口径是**价格**，所以 ``value`` 存元/兆瓦时，电量原样存进 ``raw``。
不另开 ``power_spot_volume`` 数据集，是为了避免数据集数量随维度爆炸。

口径声明
--------
周报原文注明：所有电量、电价均为**发电侧口径的快报数据**，非最终结算数据。
该声明写进每个数据点的 ``raw["口径"]``，避免下游当成结算价使用。

已知缺口
--------
广东电力交易中心的**日度**现货价格发布在微信公众号，需要浏览器渲染才能取。
本采集器只覆盖广州电力交易中心的**周度**行情，日度留作后续增强。
"""

from __future__ import annotations

import re
from datetime import date

import httpx

from newsagg.logutil import get_logger
from newsagg.market.fetchers.base import MarketFetcher, build_client
from newsagg.market.models import MarketPoint

__all__ = [
    "GZPECFetcher",
    "parse_weekly_spot",
]

log = get_logger("market.power")

_BASE = "http://www.gzpec.cn"
_LIST_URL = f"{_BASE}/news/scyj/"
_PAGE_URL = _BASE + "/news/scyj/index_{page}.html"

#: 只认这一种标题。栏目里同页还有「绿证交易每周行情一览」「结算月报」，必须排除。
_TITLE_MARK = "电力现货市场每周行情一览"

#: 站点证书链不完整（``https://`` 握手失败）且 ``gzpec.cn`` 裸域会被本机代理拦成 502，
#: 因此固定走 ``http://www.gzpec.cn``。这是**有意的降级**，不是笔误。
_VERIFY_TLS = False

_UNIT = "元/兆瓦时"
_REGION_TOTAL = "南方区域"

#: ``元/兆瓦时`` 在 HTML 里会被 ``<font>`` 拆开，或因 TRS 导出换行而夹进空白，
#: 因此每个字面片段之间都允许空白。**写死成 ``元/兆瓦时`` 会在换行时静默失配**——
#: 单测 fixture 里就复现过这个形态：``306元/`` 换行后接 ``兆瓦时``。
_UNIT_PATTERN = r"元\s*/\s*兆瓦时"

#: 分省区与分电源的词汇表。用来判定一段平行数组到底属于哪一层——
#: 两者的句式完全一样（``日前市场，X、Y…日均成交电量分别为…``），
#: 只能靠名字判断。
_PROVINCES: tuple[str, ...] = ("广东", "广西", "云南", "贵州", "海南")
_SOURCE_TYPES: tuple[str, ...] = ("煤机", "气机", "水电", "新能源")

_TAG_RE = re.compile(r"<[^>]+>")
_ARTICLE_RE = re.compile(
    r'href="(?P<href>\./\d{6}/t\d{8}_\d+\.html)"[^>]*>(?P<title>.*?)</a>',
    re.S,
)
_PUBLISHED_RE = re.compile(r"发布时间[：:]\s*(\d{4})-(\d{2})-(\d{2})")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")

#: 标题里的观测区间：``（2026年8月31日-9月6日）`` / ``（2026年9月21日-27日）``
#: / ``（2026年9月28日-10月4日）``。结束日的「月」可以省略。
_RANGE_RE = re.compile(
    r"每周行情一览\s*[（(]\s*(?P<year>\d{4})\s*年\s*(?P<m1>\d{1,2})\s*月\s*(?P<d1>\d{1,2})\s*日"
    r"\s*[-–—~～至]\s*(?:(?P<m2>\d{1,2})\s*月)?\s*(?P<d2>\d{1,2})\s*日"
)

#: 区域整体：``日前市场日均成交电量35.11亿千瓦时（发电侧，下同），较上周下降4.71亿千瓦时，
#: 加权均价为306元/兆瓦时，较上周下降78元/兆瓦时。``
_AGG_RE = re.compile(
    r"(?P<market>日前|实时)市场日均成交电量\s*(?P<volume>[\d.]+)\s*亿千瓦时"
    rf"[^。]{{0,80}}?加权均价为\s*(?P<price>[\d.]+)\s*{_UNIT_PATTERN}"
    r"[，,]\s*较(?:上周|日前)(?P<dir>上涨|下降|持平)\s*(?P<change>[\d.]+)?\s*"
    + _UNIT_PATTERN
)

#: 平行数组：``日前市场，广东、广西、云南、贵州、海南日均成交电量分别为14.99、3.80、11.61、
#: 3.86、0.86亿千瓦时，加权均价分别为368、170、239、404、297元/兆瓦时。``
_PARALLEL_RE = re.compile(
    r"(?P<market>日前|实时)市场[，,]\s*(?P<names>[^。]{0,80}?)\s*日均成交电量分别为\s*"
    r"(?P<volumes>[^。]{0,140}?)\s*亿千瓦时\s*[，,]\s*加权均价分别为\s*"
    r"(?P<prices>[^。]{0,140}?)\s*" + _UNIT_PATTERN
)

#: 正文里的口径声明，原样带进 raw。
_DISCLAIMER_RE = re.compile(r"注[：:]\s*(?P<note>[^。]*快报数据[^。]*)。")

_UP = {"上涨", "上升", "增加"}
_DOWN = {"下降", "下跌", "减少"}


def _strip(html: str) -> str:
    """剥掉全部标签、归一空白与实体。

    这是本模块**最关键的一步**：TRS 编辑器把数字与单位拆进了不同的 ``<font>``，
    只有整体剥标签才能把它们拼回 ``306元/兆瓦时``。

    剥完再把连续空白压成单个空格。TRS 导出的 HTML 会在标签之间换行缩进，
    不压缩的话 ``[^。]{0,80}`` 之类的窗口会被缩进字符白白吃掉，
    而且同一个页面在不同段落上的空白形态可能不同——压缩后行为才稳定。
    """
    text = (
        _TAG_RE.sub("", html)
        .replace("&nbsp;", " ")
        .replace("\u3000", " ")
        .replace("&amp;", "&")
    )
    return re.sub(r"\s+", " ", text)


def _signed(direction: str, magnitude: str | None) -> float | None:
    if magnitude is None:
        return None
    value = abs(float(magnitude))
    if direction in _UP:
        return value
    if direction in _DOWN:
        return -value
    return 0.0


def _numbers(raw: str) -> list[float]:
    return [float(m) for m in _NUM_RE.findall(raw)]


def parse_observation_end(title: str, fallback: date) -> date:
    """从标题解析观测周结束日。

    Args:
        title: 文章标题，如 ``…每周行情一览（2026年8月31日-9月6日）``。
        fallback: 解析不到时的兜底日期（通常是发布时间）。

    Returns:
        区间结束日。跨月/跨年按下面的规则归一：

        - 结束日的「月」省略（``9月21日-27日``）→ 视作与开始日同月；
          若结束日小于开始日（``9月28日-4日`` 这种写法不存在，但 ``12月28日-3日``
          会出现）则月份 +1；
        - 结束日的「月」显式给出且小于开始月（``12月28日-1月3日``）→ 年份 +1。
    """
    match = _RANGE_RE.search(title)
    if not match:
        return fallback

    year = int(match.group("year"))
    m1, d1 = int(match.group("m1")), int(match.group("d1"))
    m2_raw, d2 = match.group("m2"), int(match.group("d2"))

    if m2_raw is None:
        m2 = m1 + (1 if d2 < d1 else 0)
    else:
        m2 = int(m2_raw)
        if m2 < m1:
            year += 1
    if m2 > 12:  # 12 月 + 1 的进位
        m2 -= 12
        year += 1

    try:
        return date(year, m2, d2)
    except ValueError:
        log.warning("观测区间日期非法：%s-%s-%s，回落为 %s", year, m2, d2, fallback)
        return fallback


def parse_weekly_spot(html: str, *, source_url: str = "") -> list[MarketPoint]:
    """解析一份「南方区域电力现货市场每周行情一览」。

    Args:
        html: 文章页完整 HTML。
        source_url: 出处 URL。

    Returns:
        数据点列表：区域整体 2 条（日前/实时）+ 分省区 10 条 + 分电源 8 条，
        实际条数取决于该期周报发布了哪几层。

    Raises:
        ValueError: 价格一条都没解析出来。**故意抛错**——周报的第 2 周起
            必定含价格，取不到就是页面结构变了，静默返回空会掩盖故障。
    """
    text = _strip(html)

    published_match = _PUBLISHED_RE.search(text)
    published = (
        date(int(published_match.group(1)), int(published_match.group(2)), int(published_match.group(3)))
        if published_match
        else date.today()
    )
    price_date = parse_observation_end(text, published)
    if price_date == published:
        log.warning("未从标题解析出观测区间，%s 的 price_date 回落为发布日", source_url or "该期")

    note_match = _DISCLAIMER_RE.search(text)
    disclaimer = note_match.group("note").strip() if note_match else ""

    def _mk(region: str, grade: str, value: float, change: float | None, volume: float | None) -> MarketPoint:
        raw: dict[str, object] = {"口径": disclaimer or "原文未注明"}
        if volume is not None:
            raw["volume_亿千瓦时"] = volume
        return MarketPoint(
            dataset="power_spot",
            region=region,
            grade=grade,
            price_date=price_date,
            value=value,
            unit=_UNIT,
            change_value=change,
            change_pct=None,  # 原文只给元/兆瓦时的绝对涨跌，不给百分比
            source="gzpec",
            source_url=source_url,
            raw=raw,
        )

    points: list[MarketPoint] = []
    seen: set[tuple[str, str, str, str, str]] = set()

    def _add(point: MarketPoint) -> None:
        if point.key not in seen:
            seen.add(point.key)
            points.append(point)

    # 第 1 层：区域整体
    for match in _AGG_RE.finditer(text):
        _add(
            _mk(
                _REGION_TOTAL,
                match.group("market"),
                float(match.group("price")),
                _signed(match.group("dir"), match.group("change")),
                float(match.group("volume")),
            )
        )

    # 第 2、3 层：平行数组（分省区 / 分电源），靠词汇表判定层级
    for match in _PARALLEL_RE.finditer(text):
        names = match.group("names")
        market = match.group("market")
        prices = _numbers(match.group("prices"))
        volumes = _numbers(match.group("volumes"))

        provinces = [p for p in _PROVINCES if p in names]
        if provinces and len(provinces) == len(prices):
            for index, province in enumerate(provinces):
                _add(
                    _mk(
                        province,
                        market,
                        prices[index],
                        None,
                        volumes[index] if index < len(volumes) else None,
                    )
                )
            continue

        source_types = [s for s in _SOURCE_TYPES if s in names]
        if source_types and len(source_types) == len(prices):
            for index, source_type in enumerate(source_types):
                _add(
                    _mk(
                        _REGION_TOTAL,
                        f"{market}·{source_type}",
                        prices[index],
                        None,
                        volumes[index] if index < len(volumes) else None,
                    )
                )
            continue

        # 名字数量与价格数量不一致 → 句式变了。不猜，记下来。
        log.warning(
            "分项平行数组对不上：names=%r prices=%d volumes=%d，已跳过",
            names,
            len(prices),
            len(volumes),
        )

    if not points:
        raise ValueError(
            "南方区域现货周报解析出 0 条价格——正文句式可能已改版；"
            "请检查 _AGG_RE / _PARALLEL_RE 是否仍匹配官网措辞"
        )
    return points


class GZPECFetcher(MarketFetcher):
    """南方区域电力现货市场周度行情采集器（广州电力交易中心）。"""

    dataset = "power_spot"
    source = "gzpec"

    def _make_client(self) -> httpx.Client:
        """固定 ``verify=False``：该站证书链不完整，见模块 docstring。"""
        return build_client(verify=_VERIFY_TLS)

    def list_reports(self, *, limit: int = 5, max_pages: int = 3) -> list[dict[str, str]]:
        """列出最近的现货周报（新→旧）。

        Args:
            limit: 最多返回多少条。
            max_pages: 最多翻几页列表（每页 10 条）。栏目里混着绿证/月报，
                真正是现货周报的大约每页 2-3 条，所以要翻页才够取。

        Returns:
            每项含 ``title`` / ``url``。

        Raises:
            ValueError: 一条现货周报都没找到——栏目路径或标题格式可能已变。
        """
        found: list[dict[str, str]] = []
        seen_url: set[str] = set()
        for page in range(max_pages):
            url = _LIST_URL if page == 0 else _PAGE_URL.format(page=page)
            response = self.client.get(url)
            response.raise_for_status()
            response.encoding = response.encoding or "utf-8"
            text = response.text
            for match in _ARTICLE_RE.finditer(text):
                title = _strip(match.group("title")).strip()
                if _TITLE_MARK not in title:
                    continue
                href = match.group("href").removeprefix("./")
                article_url = f"{_BASE}/news/scyj/{href}"
                if article_url in seen_url:
                    continue
                seen_url.add(article_url)
                found.append({"title": title, "url": article_url})
                if len(found) >= limit:
                    return found
        if not found:
            raise ValueError(
                f"在 {_LIST_URL} 前 {max_pages} 页未找到标题含「{_TITLE_MARK}」的文章——"
                "栏目路径或标题格式可能已变"
            )
        return found

    def fetch_report(self, url: str) -> list[MarketPoint]:
        """抓并解析单篇周报。"""
        response = self.client.get(url)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        return parse_weekly_spot(response.text, source_url=url)

    def fetch(self, *, limit: int = 1) -> list[MarketPoint]:
        """抓最近 ``limit`` 期现货周报。

        Args:
            limit: 抓几期。默认 ``1``（只要最新一期）。

        Returns:
            去重后的数据点列表，按周报由新到旧排列。
        """
        if limit < 1:
            raise ValueError("limit 必须 >= 1")

        points: list[MarketPoint] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for report in self.list_reports(limit=limit):
            try:
                batch = self.fetch_report(report["url"])
            except ValueError as exc:
                log.error("现货周报 %s 解析失败：%s", report["title"], exc)
                continue
            for point in batch:
                if point.key in seen:
                    continue
                seen.add(point.key)
                points.append(point)

        if not points:
            raise ValueError(f"最近 {limit} 期南方区域现货周报一条价格都没解析出来")

        log.info(
            "南方现货采集完成：%d 期 → %d 条（%s）",
            limit,
            len(points),
            "、".join(sorted({p.price_date.isoformat() for p in points})),
        )
        return points
