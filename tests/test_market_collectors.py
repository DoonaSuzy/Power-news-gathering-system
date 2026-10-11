"""市场行情采集的回归测试：把这次踩到的坑钉住。

全部用例**不需要数据库、不需要网络**——Fixture 是写死的 HTML/文本，
HTTP 用 ``httpx.MockTransport`` 伪造。真实跑库的部分交给
``powernews market-fetch`` 与 ``scripts/e2e_smoke.py``。

每个用例对应一个真实发生过的缺陷，命名里写明「曾经怎么坏的」。
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from newsagg.market.fetchers.ceci_cec import (
    parse_import_index,
    parse_observation_end,
    parse_period,
    parse_pmi,
    parse_weekly_report,
)
from newsagg.market.fetchers.lng_100ppi import (
    PPIFetcher,
    parse_news_list,
    parse_quote_table,
)
from newsagg.market.fetchers.lng_cqpgx import CLPFetcher, parse_clp, parse_release_dates
from newsagg.market.fetchers.lng_shpgx import (
    AGGREGATES,
    RECEIVE_STATIONS,
    SHFGXFetcher,
    fill_change,
    is_receive_station,
    parse_quote_rows,
    summarize_quotes,
)
from newsagg.market.fetchers.power_gzpec import parse_observation_end as gzpec_end
from newsagg.market.fetchers.power_gzpec import parse_weekly_spot
from newsagg.market.models import DATASETS, SIGNED_DATASETS, MarketPoint
from newsagg.market.store import MarketStore

ROOT = Path(__file__).resolve().parent.parent


# ======================================================================
# 0. Fixture
# ======================================================================
#: CLP **日期列表页**：没有 <table>，只有日期 + 链接。
#: 这是 ``/cyl`` 不带 ``dt`` 时的真实形态。
CLP_LISTING = """
<div class="met-news-list">
  <ul class="ulstyle met-pager-ajax imagesize" data-scale="250x440">
    <li class="notice-item">
      <span class="media-time">2026-10-10</span>
      <a href="/cyl?type=1&amp;dt=2026-10-10" title="2026年10月10日中国液化天然气价格（CLP）"
         target="_self" class="media-title"><i class="fa fa-angle-right p-r-5"></i>2026年10月10日中国液化天然气价格（CLP）</a>
    </li>
    <li class="notice-item">
      <span class="media-time">2026-10-09</span>
      <a href="/cyl?type=1&amp;dt=2026-10-09" title="2026年10月9日中国液化天然气价格（CLP）"
         target="_self" class="media-title"><i class="fa fa-angle-right p-r-5"></i>2026年10月9日中国液化天然气价格（CLP）</a>
    </li>
    <li class="notice-item">
      <span class="media-time">2026-10-08</span>
      <a href="/cyl?type=1&amp;dt=2026-10-08" title="2026年10月8日中国液化天然气价格（CLP）"
         target="_self" class="media-title">2026年10月8日中国液化天然气价格（CLP）</a>
    </li>
  </ul>
</div>
"""

#: CLP **价格表页**：带 ``dt`` 时才返回。最后一行是占位符 ``-``。
CLP_PRICE_TABLE = """
<table class="px_tbl" id="priceTbl" data-type="1" data-dt="2026-10-10">
  <thead>
    <tr><th>区域</th><th>价格（元/吨）</th><th>环比涨跌（元/吨）</th><th>幅度</th></tr>
  </thead>
  <tbody>
    <tr><td width="25%" class="cyl-province-cell">全国</td><td>6425</td><td>+35</td><td>+0.55%</td></tr>
    <tr><td>川渝（CYLNG）</td><td>6327</td><td>+66</td><td>+1.05%</td></tr>
    <tr><td>广东</td><td>6532</td><td>-6</td><td>-0.09%</td></tr>
    <tr><td>新疆</td><td>-</td><td>-</td><td>-</td></tr>
  </tbody>
</table>
"""

#: CECI 周报正文（纯文本）。句式取自官网 2026 年第 34 期。
CECI_REPORT = (
    "中国电力企业联合会电煤价格指数（CECI）编制办公室发布的《CECI指数分析周报》"
    "（2026年第34期）显示：本期CECI沿海指数现货价格小幅上涨，"
    "CECI曹妃甸指数小幅回落，CECI进口指数综合价格明显下降，"
    "CECI采购经理人指数连续3期处于收缩区间，CECI发电天然气价格指数小幅上涨。"
    "一、市场综述燃煤电厂生产方面，本周（9月10日至9月17日，下同）呈“日耗继续回落”的总体特征。"
    "二、CECI指数分析CECI沿海指数现货价格小幅上涨，本期5500千卡/千克现货成交价较上期上涨30元/吨，"
    "5000千卡/千克现货成交价较上期上涨35元/吨。"
    "CECI进口指数综合到岸标煤单价1209元/吨，较上期下降43元/吨，环比下降3.4%。"
    "CECI采购经理人指数连续3期处于收缩区间，本期为49.37%，环比上升0.18个百分点。"
)

#: CECI 早期句式：进口指数没有「综合」二字，且环比方向词是「涨幅」而不是「上涨」。
CECI_REPORT_EARLY = (
    "《CECI指数分析周报》（2026年第5期）显示：本期CECI进口指数综合价格小幅上涨。"
    "本周（1月22日至1月29日，下同）电厂发电量环比小幅增加。"
    "CECI进口指数到岸标煤单价809元/吨，较上期上涨1元/吨，环比涨幅0.1%。"
)

#: 南方区域现货周报：**数字与单位被 <font> 切开**，这是 TRS 编辑器的真实产物。
POWER_SPOT = """
<div class="content">
  <h3 class="title">南方区域电力现货市场每周行情一览（2026年8月31日-9月6日）</h3>
  <p class="subTitle">信息来源：广州电力交易中心    发布时间：2026-09-09</p>
  <div class="txt"><div class=TRS_Editor>
    <p align="justify"><span>8<font face="宋体">月</font><font face="Calibri">31</font>
      <font face="宋体">日</font><font face="Calibri">-9</font><font face="宋体">月</font>
      <font face="Calibri">6</font><font face="宋体">日，日前市场日均成交电量</font>
      <font face="Calibri">35.11</font><font face="宋体">亿千瓦时（发电侧，下同），较上周下降</font>
      <font face="Calibri">4.71</font><font face="宋体">亿千瓦时，加权均价为</font>
      <font face="Calibri">306</font><font face="宋体">元</font><font face="Calibri">/</font>
      <font face="宋体">兆瓦时，较上周下降</font><font face="Calibri">78</font>
      <font face="宋体">元</font><font face="Calibri">/</font><font face="宋体">兆瓦时。实时市场日均成交电量</font>
      <font face="Calibri">35.42</font><font face="宋体">亿千瓦时，较日前增加</font>
      <font face="Calibri">0.31</font><font face="宋体">亿千瓦时，加权均价为</font>
      <font face="Calibri">318</font><font face="宋体">元</font><font face="Calibri">/</font>
      <font face="宋体">兆瓦时，较日前上涨</font><font face="Calibri">12</font>
      <font face="宋体">元</font><font face="Calibri">/</font><font face="宋体">兆瓦时。</font></span></p>
    <p><span><font face="宋体">一、分省区情况日前市场，广东、广西、云南、贵州、海南日均成交电量分别为</font>
      <font face="Calibri">14.99</font><font face="宋体">、</font><font face="Calibri">3.80</font>
      <font face="宋体">、</font><font face="Calibri">11.61</font><font face="宋体">、</font>
      <font face="Calibri">3.86</font><font face="宋体">、</font><font face="Calibri">0.86</font>
      <font face="宋体">亿千瓦时，加权均价分别为</font><font face="Calibri">368</font>
      <font face="宋体">、</font><font face="Calibri">170</font><font face="宋体">、</font>
      <font face="Calibri">239</font><font face="宋体">、</font><font face="Calibri">404</font>
      <font face="宋体">、</font><font face="Calibri">297</font><font face="宋体">元/兆瓦时。</font></span></p>
    <p><span><font face="宋体">实时市场，广东、广西、云南、贵州、海南日均成交电量分别为</font>
      <font face="Calibri">15.08</font><font face="宋体">、</font><font face="Calibri">3.85</font>
      <font face="宋体">、</font><font face="Calibri">11.80</font><font face="宋体">、</font>
      <font face="Calibri">3.78</font><font face="宋体">、</font><font face="Calibri">0.90</font>
      <font face="宋体">亿千瓦时，加权均价分别为</font><font face="Calibri">406</font>
      <font face="宋体">、</font><font face="Calibri">149</font><font face="宋体">、</font>
      <font face="Calibri">237</font><font face="宋体">、</font><font face="Calibri">397</font>
      <font face="宋体">、</font><font face="Calibri">292</font><font face="宋体">元/兆瓦时。</font></span></p>
    <p><span><font face="宋体">二、分电源情况日前市场，煤机、气机、水电、新能源日均成交电量分别为</font>
      <font face="Calibri">13.59</font><font face="宋体">、</font><font face="Calibri">2.61</font>
      <font face="宋体">、</font><font face="Calibri">9.42</font><font face="宋体">、</font>
      <font face="Calibri">6.21</font><font face="宋体">亿千瓦时，加权均价分别为</font>
      <font face="Calibri">363</font><font face="宋体">、</font><font face="Calibri">458</font>
      <font face="宋体">、</font><font face="Calibri">274</font><font face="宋体">、</font>
      <font face="Calibri">177</font><font face="宋体">元/兆瓦时。</font></span></p>
    <p><span><font face="宋体">实时市场，煤机、气机、水电、新能源日均成交电量分别为</font>
      <font face="Calibri">13.47</font><font face="宋体">、</font><font face="Calibri">2.74</font>
      <font face="宋体">、</font><font face="Calibri">9.40</font><font face="宋体">、</font>
      <font face="Calibri">6.53</font><font face="宋体">亿千瓦时，加权均价分别为</font>
      <font face="Calibri">394</font><font face="宋体">、</font><font face="Calibri">498</font>
      <font face="宋体">、</font><font face="Calibri">270</font><font face="宋体">、</font>
      <font face="Calibri">165</font><font face="宋体">元/兆瓦时。</font></span></p>
    <p>注：上述所有电量、电价数据均为发电侧口径的快报数据，非最终结算数据。</p>
  </div></div>
</div>
"""


#: 生意社 LNG 频道动态列表页（节选）。基准价与均差都藏在**新闻标题**里。
PPI_NEWS_LIST = """
<div class="news-list">
  <a href="https://www.100ppi.com/news/detail-20261009-6314517.html" target="_blank">液化天然气商品报价动态（2026-10-09）</a>
  <a href="https://www.100ppi.com/news/detail-20261009-6314502.html" target="_blank">PriceSeek提醒：内蒙古地区液化天然气价格上行</a>
  <a href="https://www.100ppi.com/news/detail-20261009-6313944.html" target="_blank">10月9日生意社液化天然气基准价为6152.00元/吨</a>
  <a href="https://www.100ppi.com/news/detail-20261008-6307699.html" target="_blank">10月8日生意社液化天然气基准价为6006.00元/吨</a>
  <a href="https://www.100ppi.com/news/detail-20261007-6305638.html" target="_blank">10月7日生意社液化天然气基准价为6006.00元/吨</a>
  <a href="https://www.100ppi.com/news/detail-20261006-6305566.html" target="_blank">生意社液化天然气10月6日均差为-127.70元/吨 由负向扩大转为缩小</a>
  <a href="https://www.100ppi.com/news/detail-20261005-6303217.html" target="_blank">生意社液化天然气10月5日均差继续负向扩大为-132.70元/吨</a>
  <a href="https://www.100ppi.com/news/detail-20260927-6276140.html" target="_blank">生意社液化天然气9月27日均线下穿 均差为-16.70元/吨</a>
  <a href="https://www.100ppi.com/news/detail-20260921-6260093.html" target="_blank">生意社液化天然气9月21日均差继续正向缩小为125.40元/吨</a>
  <!-- 标题日期与 URL 日期不一致：应当被丢弃 -->
  <a href="https://www.100ppi.com/news/detail-20261004-6300001.html" target="_blank">10月5日生意社液化天然气基准价为9999.00元/吨</a>
  <!-- 不是基准价也不是均差：应当被忽略 -->
  <a href="https://www.100ppi.com/news/detail-20261009-6314496.html" target="_blank">生意社：10月9日内蒙古液化天然气液价上行</a>
</div>
"""

#: 「液化天然气商品报价动态」正文里的报价表格（``<ul class='pn_text'>``）。
PPI_QUOTE_ARTICLE = """
<div class="news-detail">
  <h1>液化天然气商品报价动态（2026-10-09）</h1>
  <div class="nd-c width588">
    生意社10月09日讯
    <ul class='pn_text'>
      <li class='pn_nav'><span class='title_width200'>交易商</span><span class='title_width140'>品牌/产地</span><span class='title_width140'>交货地</span><span class='title_width100'>最新报价</span></li>
      <ul class='pn_name'>液化天然气　类别:液化天然气</ul>
      <li class='pn_data'><span class='title_w180l20'>内蒙古汇能煤化工有限公司</span><span class='title_w110l30'>内蒙汇能</span><span class='title_w140'>内蒙古</span><span class='title_w100'>6200元/吨</span></li>
      <li class='pn_data'><span class='title_w180l20'>包头市寰达新能源有限责任公司</span><span class='title_w110l30'>内蒙寰达</span><span class='title_w140'>内蒙古</span><span class='title_w100'>6170元/吨</span></li>
      <li class='pn_data'><span class='title_w180l20'>内蒙古鄂托克前旗时泰天然气经营有限责任公司</span><span class='title_w110l30'>内蒙时泰</span><span class='title_w140'>内蒙古</span><span class='title_w100'>6300元/吨</span></li>
      <li class='pn_data'><span class='title_w180l20'>内蒙古森泰天然气有限公司</span><span class='title_w110l30'>内蒙森泰</span><span class='title_w140'>内蒙古</span><span class='title_w100'>6300元/吨</span></li>
      <li class='pn_data'><span class='title_w180l20'>内蒙古鄂尔多斯市星星能源有限公司</span><span class='title_w110l30'>星星能源</span><span class='title_w140'>内蒙古</span><span class='title_w100'>6180元/吨</span></li>
    </ul>
  </div>
</div>
"""


# ======================================================================
# 1. CLP 是两段式站点 —— 不带 dt 拿到的是「日期列表页」，不是价格表
# ======================================================================
def test_clp_listing_page_is_not_a_price_table():
    """``/cyl`` 不带 ``dt`` 返回的页面**没有价格表**。

    曾经怎么坏的：``fetch()`` 直接拿不带 ``dt`` 的响应去喂 ``parse_clp``，
    报「未找到 CLP 价格表（id=priceTbl）」。真因不是页面改版，
    而是这个 Thymeleaf 站点会用同一个 URL 返回两种页面——
    查询参数不同、模板不同。所以这条断言的作用是：
    **一旦有人把两段式改回一段式，这里会立刻红。**
    """
    assert "priceTbl" not in CLP_LISTING
    assert "<table" not in CLP_LISTING
    with pytest.raises(ValueError, match="priceTbl"):
        parse_clp(CLP_LISTING)


def test_clp_listing_page_yields_release_dates():
    """列表页能解析出发布日期，且顺序是「最新在前」。"""
    assert parse_release_dates(CLP_LISTING) == [
        date(2026, 10, 10),
        date(2026, 10, 9),
        date(2026, 10, 8),
    ]


def test_clp_release_date_requires_shown_and_linked_to_agree():
    """``media-time`` 与链接里的 ``dt`` 不一致的条目应被丢弃。

    页面上别处也可能出现日期文本（时间导航等），只抓 ``media-time`` 会误收。
    要求两者一致，等于顺带证明「这一条确实是一条可点开的价格公告」。
    """
    broken = (
        '<span class="media-time">2026-10-05</span>'
        '<a href="/cyl?type=1&amp;dt=2026-10-04" class="media-title">x</a>'
    )
    assert parse_release_dates(broken) == []


def test_clp_price_table_maps_national_alias_and_skips_placeholder():
    """价格表解析：``全国`` → ``CN``、去括号注释、占位符 ``-`` 不当成 0。"""
    points = parse_clp(CLP_PRICE_TABLE, source_url="https://example.invalid/x")
    by_region = {p.region: p for p in points}

    assert set(by_region) == {"CN", "川渝", "广东"}  # 新疆那行是占位符，被跳过
    assert by_region["CN"].value == 6425
    assert by_region["CN"].change_value == 35
    assert by_region["CN"].change_pct == 0.55
    assert by_region["川渝"].value == 6327  # 括号注释 (CYLNG) 已去掉
    assert by_region["广东"].change_value == -6  # 跌用负数
    assert all(p.price_date == date(2026, 10, 10) for p in points)
    assert all(p.dataset == "lng_spot" and p.source == "cqpgx" for p in points)


def test_clp_fetch_takes_two_requests_listing_then_price_table():
    """``fetch()`` 必须发两次请求：先列表页取最新日期，再带 ``dt`` 取价格表。

    这是本模块最容易改错的地方。用 MockTransport 把两次请求的 URL 记下来，
    直接断言「第一次没有 dt、第二次带上了第一次查到的 dt」。
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "dt" in request.url.params:
            return httpx.Response(200, text=CLP_PRICE_TABLE)
        return httpx.Response(200, text=CLP_LISTING)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with CLPFetcher(client=client) as fetcher:
        points = fetcher.fetch()

    assert len(seen) == 2, f"应当恰好两次请求，实际 {len(seen)}"
    assert "dt" not in seen[0].url.params
    assert seen[1].url.params["dt"] == "2026-10-10"
    assert {p.region for p in points} == {"CN", "川渝", "广东"}


def test_clp_fetch_explicit_date_skips_listing_request():
    """给了 ``dt`` 就只发一次请求（回补历史不必先查列表）。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=CLP_PRICE_TABLE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with CLPFetcher(client=client) as fetcher:
        points = fetcher.fetch(date(2026, 10, 10))

    assert len(seen) == 1
    assert seen[0].url.params["dt"] == "2026-10-10"
    assert len(points) == 3


# ======================================================================
# 2. CECI —— 业务日期取观测周末尾，不取发布日；方向词两种写法都要吃
# ======================================================================
def test_ceci_price_date_is_week_end_not_publish_date():
    """``price_date`` 必须是观测周结束日，不是周报发布日。

    第 34 期发布于 9 月 21 日，但正文写的是「本周（9月10日至9月17日）」。
    用发布日会让整条序列凭空错位 4 天，而且错得很隐蔽——日期看着都合理。
    """
    points = parse_weekly_report(CECI_REPORT, published=date(2026, 9, 21))
    assert {p.price_date for p in points} == {date(2026, 9, 17)}


def test_ceci_import_index_value_and_signs():
    """进口指数：绝对值 + 环比绝对值 + 环比率，跌用负号。"""
    point = parse_import_index(
        CECI_REPORT, price_date=date(2026, 9, 17), period="2026年第34期"
    )
    assert point is not None
    assert point.dataset == "ceci_import"
    assert point.region == "CN"
    assert point.grade == "到岸标煤单价"
    assert point.value == 1209
    assert point.change_value == -43
    assert point.change_pct == -3.4
    assert point.unit == "元/吨"
    assert point.period == "2026年第34期"


def test_ceci_accepts_legacy_variant_without_zonghe_and_with_zhangfu():
    """早期句式的两个差异都要兼容：没「综合」二字、方向词是「涨幅」。

    官网在措辞上并不统一：``到岸标煤单价809元/吨，较上期上涨1元/吨，环比涨幅0.1%``。
    只写一种正则会在回补历史时突然断掉，而且断得悄无声息。
    """
    point = parse_import_index(
        CECI_REPORT_EARLY, price_date=date(2026, 1, 29), period="2026年第5期"
    )
    assert point is not None
    assert point.value == 809
    assert point.change_value == 1
    assert point.change_pct == 0.1  # 「涨幅」也是正号


def test_ceci_observation_end_handles_year_rollover():
    """跨年区间：``本周（12月28日至1月3日）`` 的结束日属于**下一年**。

    正文只写月日不写年，年份要从发布日推。1 月初发布的周报观察到的是上一年
    12 月底——如果直接拿发布年份去拼，会得到「2027-01-03」之前那个 2026-12-28
    与 2026-01-03 这种自相矛盾的日期。
    """
    end = parse_observation_end(
        "本周（12月28日至1月3日，下同）电厂发电量环比小幅增加。",
        published=date(2027, 1, 4),
    )
    assert end == date(2027, 1, 3)


def test_ceci_observation_end_falls_back_to_publish_date():
    """正文没写观测周时回落到发布日，不抛错。"""
    published = date(2026, 5, 20)
    assert parse_observation_end("本周电厂发电量环比小幅增加。", published) == published


def test_ceci_pmi_change_is_percentage_points_not_percent():
    """PMI 的环比值单位是**百分点**，不是百分数。

    49.37% 的指数环比 +0.18 是 0.18 个百分点。若把它塞进 ``change_pct``，
    下游会当成「涨了 0.18%」——量级差 100 倍。所以这时只填 ``change_value``，
    并在 ``raw`` 里写明单位。
    """
    point = parse_pmi(CECI_REPORT, price_date=date(2026, 9, 17), period="2026年第34期")
    assert point is not None
    assert point.dataset == "ceci_pmi"
    assert point.value == 49.37
    assert point.change_value == 0.18
    assert point.change_pct is None
    assert point.raw["change_unit"] == "个百分点"


def test_ceci_period_parsing():
    assert parse_period(CECI_REPORT) == "2026年第34期"
    assert parse_period("没有期次的正文") == ""


def test_ceci_rejects_report_without_any_metric():
    """一条指标都提不出来时必须抛错，不能静默返回空列表。

    静默返回空会让「这周真的没发布」和「解析器坏了」变得无法区分，
    而后者会在几个月里悄悄把数据断掉。
    """
    with pytest.raises(ValueError, match="0 条指标"):
        parse_weekly_report("完全无关的一段话。", published=date(2026, 9, 21))


def test_ceci_report_html_is_stripped_before_matching():
    """传入 HTML 时要先剥标签——接口返回的 ``articleContent`` 就是 HTML。"""
    html = f"<p style='text-indent:2em;'>{CECI_REPORT[:60]}</p><p>{CECI_REPORT[60:]}</p>"
    points = parse_weekly_report(html, published=date(2026, 9, 21))
    assert {p.dataset for p in points} == {"ceci_import", "ceci_pmi"}


# ======================================================================
# 3. 南方现货 —— 数字被 <font> 切碎，必须先剥标签
# ======================================================================
def test_power_spot_requires_tag_stripping():
    """不剥标签就永远匹配不到 ``元/兆瓦时``。

    这是本次最隐蔽的坑：TRS 编辑器把 ``306`` / ``元`` / ``/`` / ``兆瓦时``
    放进了**四个不同的** ``<font>`` 标签，任何「按标签块逐段取文本」的写法
    都会得到互不相邻的碎片。下面先证明碎片确实存在，再证明剥标签后能取到值。
    """
    assert "元</font>" in POWER_SPOT  # 单位被切断的直接证据
    assert "306元/兆瓦时" not in POWER_SPOT  # 原文里压根不是连续字符串

    points = parse_weekly_spot(POWER_SPOT)
    total = next(p for p in points if p.region == "南方区域" and p.grade == "日前")
    assert total.value == 306


def test_power_spot_extracts_all_three_layers():
    """一份周报产出三层：区域整体 2 条 + 分省区 10 条 + 分电源 8 条 = 20 条。

    三层句式高度相似，唯一区别是名字：分省区用省名、分电源用电源类型。
    本用例同时钉住「靠词汇表分层」这个判定逻辑没被改坏。
    """
    points = parse_weekly_spot(POWER_SPOT)
    assert len(points) == 20

    by_grade = {(p.region, p.grade): p.value for p in points}
    # 第 1 层：区域整体
    assert by_grade[("南方区域", "日前")] == 306
    assert by_grade[("南方区域", "实时")] == 318
    # 第 2 层：分省区，region 是省名
    assert by_grade[("广东", "日前")] == 368
    assert by_grade[("海南", "日前")] == 297
    assert by_grade[("广东", "实时")] == 406
    assert by_grade[("海南", "实时")] == 292
    # 第 3 层：分电源，电源类型编进 grade，region 保持「南方区域」
    assert by_grade[("南方区域", "日前·煤机")] == 363
    assert by_grade[("南方区域", "日前·新能源")] == 177
    assert by_grade[("南方区域", "实时·煤机")] == 394
    assert by_grade[("南方区域", "实时·新能源")] == 165


def test_power_spot_source_type_and_province_layers_do_not_collide():
    """两层的 ``region`` 不要互相污染：电源类型绝不能出现在 region 里。

    曾经的设计诱惑是把电源类型塞进 region（``南方区域·煤机``），
    这样 ``GROUP BY region`` 会同时混进省名与电源类型，聚合口径就废了。
    """
    points = parse_weekly_spot(POWER_SPOT)
    regions = {p.region for p in points}
    assert regions == {"南方区域", "广东", "广西", "云南", "贵州", "海南"}
    assert not any("·" in r for r in regions)
    assert all("·" not in p.grade or p.region == "南方区域" for p in points)


def test_power_spot_keeps_volume_in_raw_not_value():
    """``value`` 存价格（元/兆瓦时）；电量存 raw，避免口径混用。"""
    points = parse_weekly_spot(POWER_SPOT)
    total = next(p for p in points if p.region == "南方区域" and p.grade == "日前")
    assert total.unit == "元/兆瓦时"
    assert total.value == 306
    assert total.raw["volume_亿千瓦时"] == 35.11


def test_power_spot_aggregate_change_compares_to_correct_baseline():
    """区域整体才有环比：日前比「上周」、实时比「日前」，两者都是带符号的。"""
    points = parse_weekly_spot(POWER_SPOT)
    by_grade = {(p.region, p.grade): p for p in points}
    assert by_grade[("南方区域", "日前")].change_value == -78
    assert by_grade[("南方区域", "实时")].change_value == 12


def test_power_spot_price_date_from_title_range():
    """观测周结束日从标题取（标题带年份，比正文可靠）。"""
    points = parse_weekly_spot(POWER_SPOT)
    assert {p.price_date for p in points} == {date(2026, 9, 6)}


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("…每周行情一览（2026年8月31日-9月6日）", date(2026, 9, 6)),
        # 结束日省略「月」
        ("…每周行情一览（2026年9月21日-27日）", date(2026, 9, 27)),
        # 跨月
        ("…每周行情一览（2026年9月28日-10月4日）", date(2026, 10, 4)),
        # 跨年
        ("…每周行情一览（2026年12月28日-1月3日）", date(2027, 1, 3)),
    ],
)
def test_power_spot_range_variants(title: str, expected: date):
    """标题里的区间写法有四种变体，都要能推出正确的结束日。"""
    assert gzpec_end(title, fallback=date(2000, 1, 1)) == expected


def test_power_spot_rejects_report_without_prices():
    with pytest.raises(ValueError, match="0 条价格"):
        parse_weekly_spot("<html><body>完全无关的一段话。</body></html>")


# ======================================================================
# 4. 生意社 —— 指标藏在新闻标题里；年份只能从 URL 取
# ======================================================================
def test_ppi_news_list_extracts_benchmark_and_meandiff():
    """基准价与均差都在**标题**里，一页列表即可取到整条序列。"""
    items = parse_news_list(PPI_NEWS_LIST)
    by_kind = {(i["kind"], i["price_date"]): i["value"] for i in items}

    assert by_kind[("benchmark", date(2026, 10, 9))] == 6152.0
    assert by_kind[("benchmark", date(2026, 10, 8))] == 6006.0
    assert by_kind[("benchmark", date(2026, 10, 7))] == 6006.0
    # 均差的三种写法都要吃下：`均差为` / `均差继续负向扩大为` / `均线下穿 均差为`
    assert by_kind[("meandiff", date(2026, 10, 6))] == -127.7
    assert by_kind[("meandiff", date(2026, 10, 5))] == -132.7
    assert by_kind[("meandiff", date(2026, 9, 27))] == -16.7
    assert by_kind[("meandiff", date(2026, 9, 21))] == 125.4


def test_ppi_ignores_titles_that_are_neither_benchmark_nor_meandiff():
    """栏目里混着 PriceSeek 提醒、液价上行短讯等，不能被当成指标收进来。"""
    items = parse_news_list(PPI_NEWS_LIST)
    assert len(items) == 7
    assert all(i["kind"] in {"benchmark", "meandiff"} for i in items)


def test_ppi_discards_items_whose_title_date_disagrees_with_url():
    """标题里的月日与 URL 里的日期必须一致，否则丢弃。

    为什么要这条：**标题不写年份**（``10月5日``），年份只能从 URL 取。
    如果两者不一致，说明列表结构变了或者条目是跨年残留；
    这时若默默采信标题，就会把 10 月的数据挂到 10 月的错误年份上，
    或者更糟——把 10 月 4 日发布、描述 10 月 5 日的内容记成 10 月 5 日。
    fixture 里那条 ``detail-20261004`` 配 ``10月5日`` 的正是这种。
    """
    items = parse_news_list(PPI_NEWS_LIST)
    assert date(2026, 10, 4) not in {i["price_date"] for i in items}
    assert 9999.0 not in {i["value"] for i in items}


def test_ppi_quote_table_parses_four_columns():
    """报价表格四列：交易商 / 品牌产地 / 交货地 / 最新报价。"""
    rows = parse_quote_table(PPI_QUOTE_ARTICLE)
    assert len(rows) == 5
    assert rows[0]["trader"] == "内蒙古汇能煤化工有限公司"
    assert rows[0]["brand"] == "内蒙汇能"
    assert rows[0]["place"] == "内蒙古"
    assert rows[0]["price"] == 6200.0
    prices = [r["price"] for r in rows]
    assert min(prices) == 6170.0
    assert max(prices) == 6300.0
    # 生意社 2026-10-09 报价样本均值 —— 这个数字能与外部观察对上
    assert round(sum(prices) / len(prices), 4) == 6230.0


def test_ppi_quote_table_returns_empty_on_unrelated_page():
    """不是报价表格的页面返回空列表，而不是乱解析出脏数据。"""
    assert parse_quote_table("<html><body>今日液价小幅上行。</body></html>") == []


def test_ppi_challenge_page_is_detected_by_size_and_marker():
    """安全检查页的识别条件是「含 HW_CHECK 且体积很小」。

    两个条件都要：真实频道页里也可能出现 ``HW_CHECK`` 这个字符串
    （本站自己的 JS），只有配合体积才能区分挑战页与真实页。
    """
    challenge = httpx.Response(
        200,
        content=b'<script>document.cookie = "HW_CHECK";</script>',
        request=httpx.Request("GET", "https://trq.100ppi.com/"),
    )
    assert PPIFetcher._is_challenge(challenge, challenge.content.decode())

    # 体积超过阈值 → 不再当挑战页
    big = httpx.Response(
        200,
        content=b"x" * 3000 + b"HW_CHECK",
        request=httpx.Request("GET", "https://trq.100ppi.com/"),
    )
    assert not PPIFetcher._is_challenge(big, big.content.decode())


def test_ppi_fetcher_raises_when_challenge_cannot_be_solved():
    """挑战页结构变了必须**报错**，不能把 660 字节的占位页当正文返回。

    否则症状是「解析出 0 条基准价」——把「被风控拦住」伪装成「站点改版」，
    排查方向完全被带偏。

    fixture 是「带 HW_CHECK 标记、但变量名已改」的形态：仍能被识别为挑战页，
    却解不出 cookie 名与值。这正是页面逻辑升级时会出现的状态。
    """
    body = (
        '<html><body><script>'
        'var renamed = "56d6cbb62b867bb96bee477ba6cf3b47";'
        'document.cookie = "HW_CHECK" + "=" + renamed;'
        '</script><p>正在进行安全检查，请稍候...</p></body></html>'
    )
    assert len(body) < 2500, "fixture 必须落在挑战页体积阈值内"
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=body)
        )
    )
    with PPIFetcher(client=client) as fetcher, pytest.raises(ValueError, match="安全检查"):
        fetcher.fetch()


def test_ppi_signal_dataset_is_declared_signed():
    """``lng_signal`` 必须登记在 ``SIGNED_DATASETS`` 里。

    均差 是价差，天然为负。若没登记，任何「价格非负」的通用校验
    都会在某个跌价周期里突然报警，而那时真正的问题（解析错位）反而被淹没。
    """
    assert "lng_signal" in SIGNED_DATASETS
    assert SIGNED_DATASETS <= set(DATASETS)
    # 价格类数据集不应出现在可负集合里
    assert "lng_spot" not in SIGNED_DATASETS
    assert "power_spot" not in SIGNED_DATASETS


# ======================================================================
# 4b. 上海石油天然气交易中心 —— 挂牌明细 + 汇总是两个数据集
# ======================================================================
def _shpgx_row(
    facility: str,
    sale_to: str,
    price: float | None,
    *,
    day: str = "2026-10-09",
    company: str = "中海石油气电集团有限责任公司浙江分公司",
) -> dict:
    """造一行 ``listHis`` 返回体。字段名照抄接口，不要改名。"""
    return {
        "factoryId": 1,
        "startDate": day,
        "saleTo": sale_to,
        "price": price,
        "companyId": 1,
        "companyName": company,
        "factoryName": facility,
    }


#: 一天的真实形态：5 个接收站摊 + 3 个山西液厂，外加一条「暂不报价」。
SHFGX_ROWS_1009: list[dict] = [
    _shpgx_row("宁波接收站", "上海", 7250.0),
    _shpgx_row("宁波接收站", "江苏", 6500.0),
    _shpgx_row("滨海接收站", "江苏", 6600.0),
    _shpgx_row("北燃南港", "不限", 6600.0, company="中海石油气电集团有限责任公司华北分公司"),
    _shpgx_row("中石油如东接收站", "江苏", 6460.0, company="江苏中油昆仑能源投资有限公司"),
    _shpgx_row("华新襄垣液化", "山西", 5770.0, company="山西华新液化天然气集团有限公司销售分公司"),
    _shpgx_row("华新太原液化", "山西", 5410.0, company="山西华新液化天然气集团有限公司销售分公司"),
    _shpgx_row("华新天煜液化", "山西", 5400.0, company="山西华新液化天然气集团有限公司销售分公司"),
    # 珠海接收站当天「暂不报价」——listAll 会返回、listHis 不返回，两条路都要能挡住
    _shpgx_row("珠海接收站", "广东", None, company="中海石油气电集团有限责任公司广东分公司"),
]


def test_shpgx_quote_row_maps_sale_to_to_region_and_facility_to_grade():
    """明细点的键位是 ``region=销售地``、``grade=交货点``。

    这不是随手定的：唯一键 ``(dataset, region, grade, price_date, source)`` 里
    同一天同一销售地会有**多个交货点**报价（宁波接收站在上海/江苏各报一次），
    不把交货点放进 ``grade`` 就会互相覆盖，25 行塌成 14 行。
    """
    points = parse_quote_rows({"root": SHFGX_ROWS_1009})

    # 「暂不报价」被丢掉
    assert len(points) == 8
    assert all(p.dataset == "lng_quote" for p in points)
    assert all(p.source == "shpgx" for p in points)
    assert all(p.price_date == date(2026, 10, 9) for p in points)

    ningbo = {(p.region, p.value) for p in points if p.grade == "宁波接收站"}
    assert ningbo == {("上海", 7250.0), ("江苏", 6500.0)}

    # 键必须唯一，否则入库会撞 uk_market_point
    assert len({p.key for p in points}) == len(points)


def test_shpgx_skips_rows_without_price():
    """``price`` 为 ``None`` 的行必须跳过。

    ``listAll`` 会返回「暂不报价」行（实测 2026-10-11 有 29 行 vs listHis 25 行）。
    把 ``None`` 当成 0 参与均价会把「全部均价」压低几十元，而且没人看得出来。
    """
    rows = SHFGX_ROWS_1009 + [
        _shpgx_row("粤东接收站", "广东(粤东)", None),
        _shpgx_row("华新寿阳液化", "山西", ""),  # 空串
        _shpgx_row("华新运城液化", "山西", "-"),  # 占位符
    ]
    points = parse_quote_rows({"root": rows})
    assert len(points) == 8
    assert {p.grade for p in points}.isdisjoint({"粤东接收站", "华新寿阳液化", "华新运城液化"})


def test_shpgx_beiran_nangang_is_a_receive_station_despite_its_name():
    """「北燃南港」是接收站，但名字里没有「接收站」三个字。

    这是本模块最容易写错的判定：``"接收站" in facility`` 会把它划到液厂一侧，
    于是「接收站均价」会莫名偏低（少了 6600 这一档）。
    所以必须用显式名单，而不是字符串包含。
    """
    assert "北燃南港" in RECEIVE_STATIONS
    assert is_receive_station("北燃南港")
    assert "接收站" not in "北燃南港"  # 说明字符串包含法必然判错
    assert is_receive_station("宁波接收站")
    assert not is_receive_station("华新太原液化")


def test_shpgx_summary_separates_all_scope_from_receive_scope():
    """「全部」与「接收站」两套口径必须各算各的。

    山西液厂出厂价（5410）和沿海接收站槽批价（6460~7250）差着 1000 元，
    混在一起算出的"均价"没有业务含义。fixture 里的期望值是手算出来的：

    - 全部 8 条：7250+6500+6600+6600+6460+5770+5410+5400 = 49990 → 6248.75
    - 接收站 5 条：7250+6500+6600+6600+6460 = 33410 → 6682.0
    """
    quotes = parse_quote_rows({"root": SHFGX_ROWS_1009})
    summary = summarize_quotes(quotes)

    assert all(p.dataset == "lng_spot" for p in summary)
    assert all(p.region == "CN" for p in summary)
    assert all(p.price_date == date(2026, 10, 9) for p in summary)
    # 应该只有 AGGREGATES 里的那 6 个口径
    assert {p.grade for p in summary} == {grade for grade, _, _ in AGGREGATES}

    got = {p.grade: p.value for p in summary}
    assert got["挂牌·全部均价"] == 6248.75
    assert got["挂牌·全部最高"] == 7250.0
    assert got["挂牌·全部最低"] == 5400.0
    assert got["挂牌·接收站均价"] == 6682.0
    assert got["挂牌·接收站最高"] == 7250.0
    assert got["挂牌·接收站最低"] == 6460.0

    # 样本数写进 raw，便于解释「为什么今天均价跳了」——多半是摊位数变了
    counts = {p.grade: p.raw["样本数"] for p in summary}
    assert counts["挂牌·全部均价"] == 8
    assert counts["挂牌·接收站均价"] == 5


def test_shpgx_summary_rejects_points_from_other_datasets():
    """``summarize_quotes`` 靠 ``grade`` 认交货点，混进别家数据会静默算错。

    比如误把 ``lng_spot`` 的 ``生意社基准价`` 当成交货点传进来，
    ``is_receive_station`` 只返回 ``False``，不报错——所以这里显式拒绝。
    """
    bogus = MarketPoint(
        dataset="lng_spot",
        region="CN",
        grade="生意社基准价",
        price_date=date(2026, 10, 9),
        value=6152.0,
        unit="元/吨",
        source="100ppi",
    )
    with pytest.raises(ValueError, match="只接受 lng_quote"):
        summarize_quotes([bogus])


def test_shpgx_summary_omits_scope_with_no_sample():
    """某天只有液厂报价时，「接收站×」三个口径应当**不出行**，而不是出 ``0``。"""
    quotes = parse_quote_rows(
        {"root": [_shpgx_row("华新太原液化", "山西", 5410.0)]}
    )
    grades = {p.grade for p in summarize_quotes(quotes)}
    assert grades == {"挂牌·全部均价", "挂牌·全部最高", "挂牌·全部最低"}


def test_shpgx_skips_overlong_facility_instead_of_truncating():
    """交货点名超过 ``VARCHAR(64)`` 时**跳过并告警**，不截断。

    截断会让两个不同交货点撞成同一个键，比丢一条更糟——库里显示的会是
    错误的值，而且看不出异常。
    """
    long_name = "某" * 65
    points = parse_quote_rows(
        {"root": [_shpgx_row(long_name, "山西", 5000.0), _shpgx_row("华新太原液化", "山西", 5410.0)]}
    )
    assert [p.grade for p in points] == ["华新太原液化"]


def test_shpgx_drops_rows_with_unparsable_date():
    """``startDate`` 解析不了就丢，不能回落到「今天」。

    回落会让历史数据被打上当天的日期，且因为唯一键命中而**静默覆盖**真实值。
    """
    rows = [
        _shpgx_row("宁波接收站", "上海", 7250.0, day="2026/10/09"),
        _shpgx_row("宁波接收站", "上海", 7250.0, day=""),
        _shpgx_row("宁波接收站", "上海", 7250.0, day="2026-10-09"),
    ]
    points = parse_quote_rows({"root": rows})
    assert len(points) == 1
    assert points[0].price_date == date(2026, 10, 9)


def test_shpgx_fill_change_needs_two_adjacent_dates():
    """环比只在拿到相邻两天时才算；只抓一天必须留空，不能编造。"""
    one_day = parse_quote_rows({"root": SHFGX_ROWS_1009})
    assert all(p.change_value is None for p in fill_change(one_day))

    two_days = one_day + parse_quote_rows(
        {"root": [_shpgx_row("宁波接收站", "上海", 7150.0, day="2026-10-10")]}
    )
    filled = {(p.price_date, p.region, p.grade): p for p in fill_change(two_days)}
    later = filled[(date(2026, 10, 10), "上海", "宁波接收站")]
    assert later.value == 7150.0
    assert later.change_value == -100.0
    assert later.change_pct == pytest.approx(-1.3793, abs=1e-4)

    # 10-09 没有更早的一天可比 → 依然留空
    earlier = filled[(date(2026, 10, 9), "上海", "宁波接收站")]
    assert earlier.change_value is None


def test_shpgx_fetch_quotes_paginates_and_stops_at_total():
    """翻页必须推进 ``start``，并在 ``start >= totalProperty`` 时停。

    只发一次请求会静默截断（实测单页硬上限 500 行，约 20 个交易日）；
    忘了停则会对着空页无限循环。两个坑一起钉住。
    """
    page_of = {0: SHFGX_ROWS_1009[:4], 4: SHFGX_ROWS_1009[4:8]}
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import urllib.parse

        form = urllib.parse.parse_qs(request.content.decode())
        start = int(form["start"][0])
        seen.append({"start": start, "url": str(request.url), "form": form})
        return httpx.Response(
            200,
            json={"code": 0, "root": page_of.get(start, []), "totalProperty": 8},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SHFGXFetcher(client=client) as fetcher:
        points = fetcher.fetch_quotes(date(2026, 10, 1), date(2026, 10, 9))

    assert [s["start"] for s in seen] == [0, 4]
    assert seen[0]["url"].endswith("/eplatform/lngdata/report/listHis")
    assert seen[0]["form"]["starttime"][0] == "2026-10-01"
    assert seen[0]["form"]["endtime"][0] == "2026-10-09"
    assert seen[0]["form"]["channelName"][0] == "LNG报价数据"
    assert len(points) == 8


def test_shpgx_default_client_sends_referer_and_ajax_header():
    """默认客户端必须带 ``Referer`` 与 ``X-Requested-With``。

    上游对**接口**请求也查来源页（和中电联同一个毛病）。这两个头写在
    ``_make_client`` 里，注入 MockTransport 的用例覆盖不到——单独钉一条，
    否则哪天重构 ``_make_client`` 会静默漏掉，症状是线上 403 而单测全绿。
    """
    fetcher = SHFGXFetcher()
    try:
        headers = fetcher.client.headers
        assert headers["Referer"] == "https://www.shpgx.com/html/LNGbjsj.html"
        assert headers["X-Requested-With"] == "XMLHttpRequest"
    finally:
        fetcher.close()


def test_shpgx_fetch_quotes_rejects_reversed_range():
    """起止日期写反要立刻报错，不能发一个必然为空的请求装成「今天没数据」。"""
    with SHFGXFetcher(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))) as f:
        with pytest.raises(ValueError, match="起止日期反了"):
            f.fetch_quotes(date(2026, 10, 9), date(2026, 10, 1))


def test_shpgx_raises_on_error_code():
    """接口用 ``code`` 而不是 HTTP 状态报错；不检查会解析出 0 条当成正常。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 401, "msg": "invalid token"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SHFGXFetcher(client=client) as fetcher:
        with pytest.raises(ValueError, match="code=401"):
            fetcher.fetch_quotes(date(2026, 10, 1), date(2026, 10, 9))


# ======================================================================
# 5. 数据模型与 DDL 的契约
# ======================================================================
def _market_prices_columns() -> list[str]:
    """从 ``sql/schema.sql`` 里抠出 market_prices 的列名。"""
    sql = (ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
    block = re.search(
        r"CREATE TABLE IF NOT EXISTS market_prices\s*\((.*?)\n\)\s*ENGINE", sql, re.S
    )
    assert block, "sql/schema.sql 里找不到 market_prices 建表语句"
    columns: list[str] = []
    for raw_line in block.group(1).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        # 列名小写开头 + 类型大写开头；PRIMARY KEY / UNIQUE KEY 因此被排除
        match = re.match(r"^([a-z_][a-z0-9_]*)\s+[A-Z]", line)
        if match:
            columns.append(match.group(1))
    return columns


def test_market_point_key_matches_unique_index():
    """``MarketPoint.key`` 必须与 ``uk_market_point`` 的列**顺序完全一致**。

    ``key`` 被用来在入库前查重。如果它和唯一索引的列或顺序对不上，
    内存查重与 MySQL 查重就会得出不同结论——表现是「日志说去重了，库里却冲突」。
    """
    sql = (ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
    match = re.search(r"UNIQUE KEY uk_market_point\s*\(([^)]+)\)", sql)
    assert match, "找不到 uk_market_point 唯一索引定义"
    index_columns = [c.strip() for c in match.group(1).split(",")]

    point = MarketPoint(
        dataset="lng_spot",
        region="CN",
        grade="CLP",
        price_date=date(2026, 10, 10),
        value=6425.0,
        unit="元/吨",
        source="cqpgx",
    )
    assert len(point.key) == len(index_columns)
    assert index_columns == ["dataset", "region", "grade", "price_date", "source"]
    assert point.key == ("lng_spot", "CN", "CLP", "2026-10-10", "cqpgx")


def test_market_point_fields_all_exist_in_schema():
    """模型字段必须都能在 DDL 里找到同名列——防止两边悄悄漂移。"""
    columns = set(_market_prices_columns())
    # 主键与时间戳由数据库维护，模型里没有对应字段，属预期
    model_fields = {
        "dataset", "region", "grade", "price_date", "value", "unit",
        "change_value", "change_pct", "period", "source", "source_url", "raw",
    }
    assert model_fields <= columns, f"DDL 缺列：{sorted(model_fields - columns)}"
    assert {"id", "created_at", "updated_at"} <= columns


def test_market_point_rejects_bad_values():
    """三个构造期约束：未知数据集 / 空 region / 空 source。"""
    base = dict(
        region="CN",
        grade="CLP",
        price_date=date(2026, 10, 10),
        value=1.0,
        unit="元/吨",
        source="cqpgx",
    )
    with pytest.raises(ValueError, match="未知数据集"):
        MarketPoint(dataset="not_a_dataset", **base)
    with pytest.raises(ValueError, match="region 不能为空"):
        MarketPoint(dataset="lng_spot", **{**base, "region": ""})
    with pytest.raises(ValueError, match="source 不能为空"):
        MarketPoint(dataset="lng_spot", **{**base, "source": ""})


def test_schema_dataset_comment_lists_every_dataset():
    """``DATASETS`` 里每个键都要在 DDL 的列注释里出现。

    注释是给未来改表的人看的。新加数据集却忘了更新注释，
    下一个人就会以为表里只有三类数据。
    """
    sql = (ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
    block = re.search(
        r"CREATE TABLE IF NOT EXISTS market_prices\s*\((.*?)\n\)\s*ENGINE", sql, re.S
    )
    assert block
    comment = re.search(r"dataset\s+VARCHAR\(\d+\)\s+NOT NULL COMMENT '([^']*)'", block.group(1))
    assert comment, "dataset 列缺少 COMMENT"
    missing = [name for name in DATASETS if name not in comment.group(1)]
    assert not missing, f"DDL 的 dataset 列注释漏了：{missing}"


# ======================================================================
# 6. 入库：唯一键去重、空值跳过、MySQL rowcount 语义
# ======================================================================
class _FakeCursor:
    """记录执行过的 SQL，并按预设序列回报 ``rowcount``。"""

    def __init__(self, log: list[tuple[str, tuple]], rowcounts: list[int]) -> None:
        self._log = log
        self._rowcounts = rowcounts

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self._log.append((sql, params or ()))

    @property
    def rowcount(self) -> int:
        """取一个预设值；用完后一直返回最后一个。"""
        if len(self._rowcounts) > 1:
            return self._rowcounts.pop(0)
        return self._rowcounts[0] if self._rowcounts else 0


class _FakeConn:
    def __init__(self, rowcounts: list[int]) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self._rowcounts = rowcounts

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.executed, list(self._rowcounts))

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class _FakeDb:
    def __init__(self, rowcounts: list[int] | None = None) -> None:
        self.conn = _FakeConn(rowcounts or [1])
        self.connect_calls = 0

    def connect(self, **_kwargs: object) -> _FakeConn:
        self.connect_calls += 1
        return self.conn


def _point(**overrides: object) -> MarketPoint:
    payload: dict[str, object] = dict(
        dataset="lng_spot",
        region="CN",
        grade="CLP",
        price_date=date(2026, 10, 10),
        value=6425.0,
        unit="元/吨",
        source="cqpgx",
    )
    payload.update(overrides)
    return MarketPoint(**payload)  # type: ignore[arg-type]


def test_store_deduplicates_within_one_batch():
    """同一批内的重复唯一键只写一次，并计入 skipped。

    如果放任重复进入 INSERT，第二条会以「已存在」的身份命中唯一键，
    ``rowcount`` 变成 2 —— 统计里就会凭空多出一次「更新」，
    而真实原因只是采集器自己吐了重复数据。
    """
    db = _FakeDb(rowcounts=[1])
    result = MarketStore(db=db).upsert([_point(), _point()])
    assert len(db.conn.executed) == 1
    assert result.inserted == 1
    assert result.skipped == 1
    assert db.conn.committed


def test_store_skips_points_with_no_value_and_no_change():
    """既无绝对值也无涨跌的数据点不承载信息，跳过而不是写 NULL 行。"""
    db = _FakeDb(rowcounts=[1])
    result = MarketStore(db=db).upsert(
        [_point(value=None, change_value=None), _point(region="广东", value=6532.0)]
    )
    assert len(db.conn.executed) == 1
    assert result.inserted == 1
    assert result.skipped == 1


def test_store_maps_mysql_rowcount_to_inserted_updated_unchanged():
    """MySQL 对 ``ON DUPLICATE KEY UPDATE`` 的 rowcount 是 1/2/0 三态。

    1 = 新插入，2 = 命中有变化（UPDATE），0 = 命中但值没变。
    把这三态映射对，幂等报告才可信——「未变」正是「重复采集没有产生垃圾行」的证据。
    """
    points = [
        _point(region="CN"),
        _point(region="广东"),
        _point(region="川渝"),
    ]
    db = _FakeDb(rowcounts=[1, 2, 0])
    result = MarketStore(db=db).upsert(points)
    assert result.inserted == 1
    assert result.updated == 1
    assert result.unchanged == 1
    assert result.total == 3
    assert len(db.conn.executed) == 3


def test_store_serializes_raw_and_truncates_long_source_url():
    """``raw`` 以 JSON 写入，``source_url`` 截到列宽以内。

    列宽是 ``VARCHAR(768)``。不截断的话，严格模式下 MySQL 会直接报
    「Data too long」，把一条本可入库的数据整条拒掉。
    """
    db = _FakeDb(rowcounts=[1])
    point = _point(raw={"区域原文": "全国", "涨跌原文": "+35"}, source_url="u" * 1000)
    MarketStore(db=db).upsert([point])

    _sql, params = db.conn.executed[0]
    # 列顺序与 _INSERT_SQL 一致：source_url 倒数第 2、raw 最后
    assert len(params[10]) == 768
    assert len(params[10]) < 1000
    payload = json.loads(params[11])
    assert payload["区域原文"] == "全国"


def test_store_empty_batch_does_not_touch_database():
    db = _FakeDb()
    result = MarketStore(db=db).upsert([])
    assert db.connect_calls == 0
    assert result.total == 0


# ======================================================================
# 7. 注册表完整性
# ======================================================================
def test_every_dataset_has_a_registered_fetcher():
    """``DATASETS`` 里每个键都必须能在 ``FETCHERS`` 找到采集器。

    反过来的漏洞很典型：加了数据集常量与 DDL 列，却忘了登记采集器，
    于是 ``market-fetch`` 报「尚未注册采集器」，而库里躺着一列永远为空的取值。
    """
    from newsagg.market.fetchers import FETCHERS

    missing = sorted(set(DATASETS) - set(FETCHERS))
    assert not missing, f"以下数据集没有采集器：{missing}"
    extra = sorted(set(FETCHERS) - set(DATASETS))
    assert not extra, f"以下采集器登记了未定义的数据集：{extra}"


def test_shared_fetcher_is_registered_under_its_primary_dataset():
    """一个类产出多个数据集时，它自己的 ``dataset`` 属性必须是**主数据集**。

    这条防的是「登记错位」：``PPIFetcher`` 既产 ``lng_spot`` 又产 ``lng_signal``，
    如果把它的 ``dataset`` 属性写成 ``lng_signal``，那么按 dataset 反查采集器时
    会出现两个键都指向同一类、但类自称属于其中之一的混乱局面。
    """
    from newsagg.market.fetchers import FETCHERS, PRIMARY_DATASET

    for name, cls in FETCHERS.items():
        expected = PRIMARY_DATASET[cls.__name__]
        assert cls.dataset == expected, (
            f"{name} → {cls.__name__}.dataset={cls.dataset!r}，应为 {expected!r}"
        )
        assert expected in DATASETS


def test_registry_covers_every_fetcher_class():
    """反向覆盖：定义了采集器类却没登记，会让它永远不被调度。"""
    from newsagg.market.fetchers import FETCHERS, PRIMARY_DATASET

    registered = {cls.__name__ for cls in FETCHERS.values()}
    assert registered == set(PRIMARY_DATASET), (
        f"未登记：{sorted(set(PRIMARY_DATASET) - registered)}；"
        f"多余：{sorted(registered - set(PRIMARY_DATASET))}"
    )


def test_shpgx_registry_and_signed_datasets_consistency():
    """SHFGX 一次产出 ``lng_quote`` + ``lng_spot``，两个键都要能被反查到。

    ``lng_quote`` 是它的主数据集（类属性），``lng_spot`` 是搭车产出的汇总；
    后者已经有 ``CLPFetcher`` 登记，不能因此把 ``lng_spot → SHFGXFetcher``
    改掉——否则按 dataset 抓取时 CLP 的分区域 25 点会直接消失。
    """
    from newsagg.market.fetchers import FETCHERS

    assert FETCHERS["lng_quote"] is SHFGXFetcher
    assert SHFGXFetcher.dataset == "lng_quote"
    assert FETCHERS["lng_spot"] is not SHFGXFetcher
    assert "lng_quote" not in SIGNED_DATASETS


def test_produced_datasets_covers_every_fetcher_and_is_within_datasets():
    """``PRODUCED_DATASETS`` 必须覆盖每个采集器类，且只写已登记的数据集。

    这张表是 ``--dataset`` 选采集器的依据。漏一个类 → 那个采集器永远不会被
    ``--dataset all`` 之外的调用选中；写进未登记的 dataset → 入库时才炸。
    """
    from newsagg.market.fetchers import PRIMARY_DATASET, PRODUCED_DATASETS

    assert set(PRODUCED_DATASETS) == set(PRIMARY_DATASET)
    for name, produced in PRODUCED_DATASETS.items():
        assert PRIMARY_DATASET[name] in produced, f"{name} 的产出里没有它的主数据集"
        assert set(produced) <= set(DATASETS), f"{name} 产出了未登记的数据集：{produced}"
        assert len(set(produced)) == len(produced), f"{name} 的产出有重复：{produced}"


def test_target_classes_selects_every_producer_of_a_dataset():
    """``--dataset lng_spot`` 必须同时选中三家产它的采集器。

    这是「只查 dataset → 类 的单值登记表」会踩的坑：那张表里 ``lng_spot``
    只登记了 ``CLPFetcher``，照它跑会静默漏掉生意社与上海挂牌的现价——
    命令返回 0、日志正常，只有翻库才发现少了两家。
    """
    from newsagg.market.cli import _target_classes

    assert {c.__name__ for c in _target_classes(["lng_spot"])} == {
        "CLPFetcher",
        "PPIFetcher",
        "SHFGXFetcher",
    }
    assert {c.__name__ for c in _target_classes(["lng_quote"])} == {"SHFGXFetcher"}
    assert {c.__name__ for c in _target_classes(["ceci_pmi"])} == {"CECIFetcher"}


def test_target_classes_deduplicates_shared_fetcher():
    """两个数据集指向同一个类时只抓一次，否则同一份周报会抓两遍。"""
    from newsagg.market.cli import _target_classes

    assert [c.__name__ for c in _target_classes(["ceci_import", "ceci_pmi"])] == ["CECIFetcher"]
    # 顺带把「产出 lng_spot 的三家」全部选中，且不重复
    classes = _target_classes(sorted(DATASETS))
    assert len(classes) == len(set(classes)) == 5
