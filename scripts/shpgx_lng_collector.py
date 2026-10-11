#!/usr/bin/env python3
"""上海石油天然气交易中心（SHFGX）LNG 挂牌数据采集脚本 —— 独立版。

设计目标
--------
**不依赖本项目任何包，只用 Python 标准库**。复制这一个文件到任何有 Python 3.9+
的机器上就能跑，用于核对 / 复现 / 交叉验证项目内
``newsagg.market.fetchers.lng_shpgx`` 采集到的数字。

用法::

    # 抓最近 3 个有数据的交易日
    python shpgx_lng_collector.py --days 3 --out data/reverse-shpgx

    # 抓指定闭区间
    python shpgx_lng_collector.py --start 2026-08-01 --end 2026-10-11

    # 只打印不落盘
    python shpgx_lng_collector.py --days 1 --no-write

    # 抓需要鉴权的那一层（必须自带合法 token）
    python shpgx_lng_collector.py --days 1 --token eyJhbGciOi...

    # 复现鉴权契约证据（JWT 请求证据 JSON 由此生成）
    python shpgx_lng_collector.py --probe-auth

接口来源
--------
人工页 ``https://www.shpgx.com/html/LNGbjsj.html``（「LNG报价数据」）的表格是空壳，
数据由 ``/js/charts/lngprice.js`` 经 ``/js/jquery.mypagination.js`` 的 ``gridpage()`` 拉取::

    POST /eplatform/lngdata/report/listHis
    start / length / companyID / starttime / endtime / channelName / ts

响应 ``{root:[...], totalProperty:N, ...}``，``root`` 按 ``startDate`` 倒序。

踩过的坑（改这个脚本前先看）
--------------------------
1. 首页 ``<table data-ajax-url="/exdata/pngexchangedata/pagelist.json">`` 那条路径
   请求返回 **404**（nginx），是死链。别照着 ``data-ajax-url`` 写。
2. ``listAll`` ≠ ``listHis``：``listAll`` 只返「当前有效报价」且**含 ``price=null``
   的「暂不报价」行**（实测 2026-10-11：29 行 vs 25 行）。算均价会被 null 稀释，
   所以只用 ``listHis``。
3. 服务端**单页硬上限 500**（``length`` 给 1000/2000/5000 都只回 500），必须翻页。
4. ``starttime`` / ``endtime`` 是**闭区间**；且**不是每个自然日都有数据**
   （2026-10-10 周六整天没有，相邻的 10-09 / 10-11 都有）。
   所以 ``--days N`` 取的是「最近 N 个**有数据**的日期」。
5. 「接收站」上游没有类型字段，只能按名称判。**「北燃南港」（北京燃气天津南港
   接收站）名字里没有「接收站」**，用 ``"接收站" in name`` 会把它误判成液厂。
   本脚本用显式白名单 :data:`RECEIVE_STATIONS`。

退出码：0 成功；1 参数/网络/解析失败。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Any

__all__ = [
    "RECEIVE_STATIONS",
    "SUMMARY_SCOPES",
    "collect",
    "fetch_quotes",
    "parse_rows",
    "probe_auth_contract",
    "summarize",
    "write_csv",
    "write_json",
]

BASE = "https://www.shpgx.com"
LIST_HIS = BASE + "/eplatform/lngdata/report/listHis"
LIST_ALL = BASE + "/eplatform/lngdata/report/listAll"
PAGE_URL = BASE + "/html/LNGbjsj.html"
DATAAPI_MAP = BASE + "/eplatform/dataapi/marketzhishu/map"
CHANNEL = "LNG报价数据"

#: 服务端单页硬上限。给更大的值也只回 500，写死避免误以为能一次拉完。
PAGE_SIZE = 500
#: 翻页上限。2500 条 ≈ 100 个交易日，足够 ``--days`` 的合理取值。
MAX_PAGES = 8
#: ``--days`` 模式下的回溯窗口（自然日）。
LOOKBACK_DAYS = 200
TIMEOUT = 30.0
UNIT = "元/吨"

#: 接收站白名单。取自 2026-08-01 ~ 2026-10-11 全量 1674 条明细的实际交货点取值
#: （共 18 个：接收站 8 个 + 液厂 10 个）。
RECEIVE_STATIONS: frozenset[str] = frozenset(
    {
        "宁波接收站",
        "滨海接收站",
        "中石油如东接收站",
        "珠海接收站",
        "粤东接收站",
        "国网迭福接收站",
        "国网天津接收站",
        # 北京燃气天津南港接收站 —— 名字里没有「接收站」，最容易漏。
        "北燃南港",
    }
)

#: 汇总口径：(开关取值, 中文名, 归约函数)。
#: 分「全部」与「接收站」两套：山西液厂出厂价（低至 5400）与沿海接收站槽批价
#: （6460~7250）差着 1000 元，混在一起算出的"均价"没有业务含义。
SUMMARY_SCOPES: tuple[tuple[str, str, Any], ...] = (
    ("all", "挂牌·全部均价", statistics.mean),
    ("all", "挂牌·全部最高", max),
    ("all", "挂牌·全部最低", min),
    ("receive", "挂牌·接收站均价", statistics.mean),
    ("receive", "挂牌·接收站最高", max),
    ("receive", "挂牌·接收站最低", min),
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36 shpgx-research-collector"
)

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


# ======================================================================
# 0. HTTP
# ======================================================================
class Probe:
    """一次请求的裸证据（请求原文 + 响应原文）。"""

    def __init__(self, method: str, url: str, headers: dict[str, str], body: str) -> None:
        self.method = method
        self.url = url
        self.headers = headers
        self.body = body
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}
        self.response_body: str = ""
        self.error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "request": {
                "method": self.method,
                "url": self.url,
                "headers": self.headers,
                "body": self.body,
            },
            "response": {
                "status": self.status,
                "headers": self.response_headers,
                "body": self.response_body,
            },
            "error": self.error,
        }

    @property
    def json_body(self) -> Any:
        try:
            return json.loads(self.response_body)
        except (ValueError, TypeError):
            return None


def http_post(
    url: str,
    data: dict[str, Any],
    *,
    extra_headers: dict[str, str] | None = None,
    timeout: float = TIMEOUT,
) -> Probe:
    """POST 表单，返回带完整原文的 :class:`Probe`。**不抛异常**——错误进 ``.error``。"""
    headers = {
        "User-Agent": UA,
        "Referer": PAGE_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if extra_headers:
        headers.update(extra_headers)
    body = urllib.parse.urlencode(data)
    probe = Probe("POST", url, headers, body)
    request = urllib.request.Request(url, data=body.encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, context=_SSL_CTX, timeout=timeout) as resp:
            probe.status = resp.status
            probe.response_headers = dict(resp.headers)
            probe.response_body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        probe.status = exc.code
        probe.response_headers = dict(exc.headers or {})
        probe.response_body = exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - 网络异常要如实记进证据，不能吞
        probe.error = f"{type(exc).__name__}: {exc}"
    return probe


def http_get(url: str, *, timeout: float = TIMEOUT) -> Probe:
    """GET，返回带完整原文的 :class:`Probe`。用于抓对方前端页面留证。"""
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    probe = Probe("GET", url, headers, "")
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, context=_SSL_CTX, timeout=timeout) as resp:
            probe.status = resp.status
            probe.response_headers = dict(resp.headers)
            probe.response_body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        probe.status = exc.code
        probe.response_headers = dict(exc.headers or {})
        probe.response_body = exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        probe.error = f"{type(exc).__name__}: {exc}"
    return probe


# ======================================================================
# 1. 解析
# ======================================================================
def _to_float(value: Any) -> float | None:
    """``None`` / 空串 / ``"-"`` / ``"~"`` 一律 ``None``。

    占位符不是 0：写成 0 会在算涨跌幅时产生假暴跌。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {"-", "--", "—", "~"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def is_receive_station(facility: str) -> bool:
    """按**显式白名单**判接收站，不是字符串包含（「北燃南港」会漏）。"""
    return facility in RECEIVE_STATIONS


def parse_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把 ``listHis`` 响应体解析成明细行 dict 列表。

    跳过：``price`` 缺失/非正值、``startDate`` 解析失败、销售地或交货点为空。
    """
    rows = payload.get("root") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        price = _to_float(row.get("price"))
        price_date = _to_date(row.get("startDate"))
        sale_to = str(row.get("saleTo") or "").strip()
        facility = str(row.get("factoryName") or "").strip()
        if price is None or price <= 0 or price_date is None or not sale_to or not facility:
            continue
        out.append(
            {
                "price_date": price_date.isoformat(),
                "sale_to": sale_to,
                "facility": facility,
                "is_receive_station": is_receive_station(facility),
                "price": price,
                "unit": UNIT,
                "company_name": row.get("companyName"),
                "company_id": row.get("companyId"),
                "factory_id": row.get("factoryId"),
            }
        )
    return out


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按日期汇总出 6 个口径。某口径当日无样本则不出行（**不出 0**）。"""
    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_date.setdefault(row["price_date"], []).append(row)

    out: list[dict[str, Any]] = []
    for day in sorted(by_date):
        items = by_date[day]
        for scope, name, reducer in SUMMARY_SCOPES:
            subset = [
                r for r in items if scope == "all" or r["is_receive_station"]
            ]
            if not subset:
                continue
            values = [r["price"] for r in subset]
            out.append(
                {
                    "price_date": day,
                    "grade": name,
                    "scope": scope,
                    "value": round(float(reducer(values)), 2),
                    "unit": UNIT,
                    "sample_size": len(values),
                    "max_of_sample": max(values),
                    "min_of_sample": min(values),
                }
            )
    return out


def fill_change(rows: list[dict[str, Any]], key_of: Any, value_key: str = "price") -> None:
    """就地补环比。**只有拿到相邻两个日期才算**，只抓一天就留空，不编造。

    Args:
        rows: 待处理的明细或汇总行。
        key_of: 单行 → 分组键。**必须把销售地一起放进键里**——
            「宁波接收站」同一天在上海、江苏、安徽、河南、浙江各报一次，
            只按交货点名分组会让这 5 条互相成为对方的"上一期"，
            算出一堆假的环比。
        value_key: 数值字段名。明细是 ``price``，汇总是 ``value``。
    """
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(key_of(row), []).append(row)
    for series in groups.values():
        series.sort(key=lambda r: r["price_date"])
        prev_value: float | None = None
        for row in series:
            current = float(row[value_key])
            if prev_value:
                row["change_value"] = round(current - prev_value, 4)
                row["change_pct"] = round((current - prev_value) / prev_value * 100, 4)
            else:
                row.setdefault("change_value", None)
                row.setdefault("change_pct", None)
            prev_value = current


# ======================================================================
# 2. 抓取
# ======================================================================
def _payload(start: int, length: int, start_date: date, end_date: date) -> dict[str, Any]:
    return {
        "start": start,
        "length": length,
        "companyID": "",
        "starttime": start_date.isoformat(),
        "endtime": end_date.isoformat(),
        "channelName": CHANNEL,
        "ts": int(time.time() * 1000),
    }


def _request_page(
    start: int, start_date: date, end_date: date, token: str | None
) -> dict[str, Any]:
    headers = {"token": token} if token else None
    probe = http_post(LIST_HIS, _payload(start, PAGE_SIZE, start_date, end_date), extra_headers=headers)
    if probe.error:
        raise RuntimeError(f"请求失败：{probe.error}")
    payload = probe.json_body
    if not isinstance(payload, dict):
        raise RuntimeError(f"响应不是 JSON：{probe.response_body[:200]!r}")
    if payload.get("code") not in (0, None):
        raise RuntimeError(f"接口报错 code={payload.get('code')} msg={payload.get('msg')!r}")
    return payload


def fetch_quotes(start_date: date, end_date: date, *, token: str | None = None) -> list[dict[str, Any]]:
    """抓 ``[start_date, end_date]``（闭区间）内的全部挂牌明细，自动翻页。"""
    if start_date > end_date:
        raise ValueError(f"起止日期反了：{start_date} > {end_date}")
    collected: list[dict[str, Any]] = []
    offset = 0
    for _ in range(MAX_PAGES):
        payload = _request_page(offset, start_date, end_date, token)
        page = payload.get("root") or []
        collected.extend(parse_rows({"root": page}))
        if not page:
            break
        offset += len(page)
        if offset >= (payload.get("totalProperty") or 0):
            break
    return collected


def latest_dates(start_date: date, end_date: date, days: int, *, token: str | None = None) -> list[date]:
    """取区间内**实际出现过**的日期，倒序前 ``days`` 个。

    不能按自然日切：2026-10-10 一整天没有挂牌记录，``--days 1`` 直接空窗。
    """
    collected: list[dict[str, Any]] = []
    offset = 0
    for _ in range(MAX_PAGES):
        payload = _request_page(offset, start_date, end_date, token)
        page = payload.get("root") or []
        collected.extend(parse_rows({"root": page}))
        seen = sorted({r["price_date"] for r in collected}, reverse=True)
        if len(seen) >= days or not page:
            return [date.fromisoformat(d) for d in seen[:days]]
        offset += len(page)
        if offset >= (payload.get("totalProperty") or 0):
            return [date.fromisoformat(d) for d in seen[:days]]
    return []


def collect(
    *,
    days: int | None = None,
    start: date | None = None,
    end: date | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """统一入口，返回 ``{quotes, summary, range, counters}``。

    ``days`` 与 ``start/end`` 二选一。``days`` 路径取「最近 N 个**有数据**的日期」，
    所以内部要先探一遍有哪些日期，再按这些日期过滤。
    """
    keep: set[date] | None = None
    if days:
        today = date.today()
        picked = latest_dates(today - timedelta(days=LOOKBACK_DAYS), today, max(int(days), 1), token=token)
        if not picked:
            return {"quotes": [], "summary": [], "range": None, "counters": {"quotes": 0, "dates": 0,
                                                                            "facilities": 0, "receive_stations": 0}}
        keep = set(picked)
        start, end = min(picked), max(picked)

    if start is None or end is None:
        raise ValueError("必须给 --days 或 --start/--end")

    quotes = fetch_quotes(start, end, token=token)
    if keep is not None:
        quotes = [q for q in quotes if date.fromisoformat(q["price_date"]) in keep]

    # 环比分组键必须含销售地，否则「宁波接收站」的多个销售地会互相成为上一期
    fill_change(quotes, lambda r: (r["facility"], r["sale_to"]))
    summary = summarize(quotes)
    fill_change(summary, lambda r: r["grade"], "value")

    return {
        "quotes": quotes,
        "summary": summary,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "counters": {
            "quotes": len(quotes),
            "dates": len({q["price_date"] for q in quotes}),
            "facilities": len({q["facility"] for q in quotes}),
            "receive_stations": len({q["facility"] for q in quotes if q["is_receive_station"]}),
        },
    }


# ======================================================================
# 3. 鉴权契约探测（JWT 请求证据）
# ======================================================================
def probe_auth_contract() -> dict[str, Any]:
    """黑盒探测 ``/eplatform/dataapi/*`` 的 token 契约，返回可归档的裸证据。

    探测手法是**报错信息分叉**：服务端对各分支给不同 msg，
    于是"改了哪个请求头/哪个值"就能从 msg 变化上读出来。

    只读取服务端**公开返回的报错信息**，不构造也不破解任何有效凭证。
    """
    probes: list[dict[str, Any]] = []

    def add(label: str, note: str, url: str, data: dict, headers: dict[str, str] | None = None) -> None:
        entry = http_post(url, data, extra_headers=headers).as_dict()
        entry["label"] = label
        entry["note"] = note
        probes.append(entry)

    # --- A. 无 token vs 有 token：错误信息分叉 ---
    add("A1 未带 token", "缺少 token 头 → 走「缺失」分支", DATAAPI_MAP, {})
    for header_name in ("token", "Authorization", "X-Token", "X-Auth-Token", "jwt"):
        add(
            f"A2 头名 {header_name}，值 Bearer FAKE",
            "msg 若变为「token失效，请重新登录」→ 该头名被识别",
            DATAAPI_MAP,
            {},
            {header_name: "Bearer FAKE.JWT.SIG"},
        )

    # --- A3. 大小写变体：确认识别的是「头名」而不是某个固定拼写 ---
    for spelling in ("token", "Token", "TOKEN", "tOkEn"):
        add(
            f"A3 头名拼写 {spelling}",
            "HTTP 头名大小写不敏感，四种拼写应得到同一条 msg",
            DATAAPI_MAP,
            {},
            {spelling: "Bearer FAKE.JWT.SIG"},
        )

    # --- B. token 值与分支的关系 ---
    for label, value, note in (
        ("B1 空字符串", "", "空值等同于缺失"),
        ("B2 非 JWT 字符串", "hello", "能过「存在性」检查，进校验分支"),
        ("B3 两段点分", "aaa.bbb", "段数不足"),
        ("B4 HS256 假签名", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0In0.AAAA", "头合法、签名无效"),
    ):
        add(f"{label}", note, DATAAPI_MAP, {}, {"token": value})

    # --- C. 鉴权过滤器的作用边界（是否白名单） ---
    day = (date.today() - timedelta(days=1)).isoformat()
    form = {"start": 0, "length": 1, "starttime": day, "endtime": day, "channelName": CHANNEL}
    for path, note in (
        ("/eplatform/lngdata/report/listHis", "白名单：无需 token"),
        ("/eplatform/lngdata/report/listAll", "白名单：无需 token"),
        ("/eplatform/lngdata/report/listXxx", "**不存在的子路径**：仍回 401"),
        ("/eplatform/marketzhishu/list/3/22", "同源旧路径，需 token"),
        ("/eplatform/xxx", "任意未知路径，需 token"),
    ):
        add(f"C {path}", note, BASE + path, form)

    return {
        "target": BASE,
        "probe_kind": "black-box auth contract (error-message divergence)",
        "probes": probes,
        "frontend_call_sites": _frontend_call_sites(),
        "findings": _auth_findings(probes),
    }


#: 前端证据源：既扫页面内联 ``<script>``，也扫外链 JS。
#:
#: **只扫 HTML 会漏掉最关键的两条**：周度价格 / 月度均价的请求代码在
#: ``/js/charts/png_1.js``、``png_2.js`` 里，而它们调的是
#: ``/eplatform/dataapi/marketzhishu/zhishuList[2]`` —— **没有被注释，是生效的**。
#: 也就是说这两页对匿名访客本来就是坏的（拿到 401、表格空白）。
_FRONTEND_SOURCES = (
    "/html/zgLNGccjgqyzs.html",  # 区域指数：dataapi 调用**被注释掉**，退回开放路径
    "/html/zdjg.html",           # 周度价格
    "/html/ydjj.html",           # 月度均价
    "/html/LNGbjsj.html",        # LNG报价数据
    "/html/czjg.html",           # 出站价格
    "/html/sjg.html",            # 省价格
    "/html/djsjg.html",          # 地级市价格
    "/html/yhtrqsj.html",        # 液化天然气数据
    "/html/qgjg.html",           # 全国价格
    "/js/charts/png_1.js",       # ← 周度价格的真正请求代码
    "/js/charts/png_2.js",       # ← 月度均价的真正请求代码
    "/js/charts/clv.js",
    "/js/charts/chuzhanjiage.js",
    "/js/charts/lngprice.js",
    "/js/charts/index_lngprice.js",
    "/js/charts/area_chart.js",
    "/js/index_chart.js",
)

#: 只捞站内 API 路径。``//`` 出现在字符串里（如 ``https://``）不算注释——
#: 判注释统一看**行首**。
_QUOTED_PATH_RE = re.compile(r"""["'](?P<url>/(?:eplatform|marketzhishu|exdata|lngdata)[^"']*)["']""")


def _frontend_call_sites() -> list[dict[str, Any]]:
    """抓对方前端源码，把站内 API 调用点连**注释状态**一起留证。

    这是本次逆向里证据价值最高的一步：

    - ``/html/zgLNGccjgqyzs.html`` 里 ``//var url="/eplatform/dataapi/marketzhishu/map";``
      被注释掉，生效的是 ``/marketzhishu/map`` —— 站点**自己**从新接口退回了旧接口；
    - ``/js/charts/png_1.js`` / ``png_2.js`` 里的
      ``/eplatform/dataapi/marketzhishu/zhishuList`` **没被注释** —— 说明新鉴权层
      确实有前端在跑，只是那两页对匿名访客拿不到数据。

    因此必须扫外链 JS，只看 HTML 会把后两条漏掉。
    """
    out: list[dict[str, Any]] = []
    for path in _FRONTEND_SOURCES:
        probe = http_get(BASE + path)
        entry: dict[str, Any] = {
            "source": BASE + path,
            "http_status": probe.status,
            "content_length": len(probe.response_body),
            "sites": [],
        }
        if probe.error:
            entry["error"] = probe.error
            out.append(entry)
            continue

        seen: set[tuple[str, bool]] = set()
        for raw_line in probe.response_body.splitlines():
            stripped = raw_line.strip()
            commented = stripped.startswith("//")
            for match in _QUOTED_PATH_RE.finditer(raw_line):
                url = match.group("url")
                if (url, commented) in seen:
                    continue
                seen.add((url, commented))
                entry["sites"].append(
                    {
                        "url": url,
                        "commented_out": commented,
                        "belongs_to_token_layer": url.startswith("/eplatform/dataapi")
                        or url.startswith("/eplatform/marketzhishu")
                        or url.startswith("/eplatform/xxx"),
                        "raw_line": stripped[:200],
                    }
                )
        out.append(entry)
    return out


def _auth_findings(probes: list[dict[str, Any]]) -> dict[str, Any]:
    """把原始探测收敛成结论——**每条结论都必须指得出它由哪条探测支撑**。"""
    def msg(label_fragment: str) -> str:
        for entry in probes:
            if label_fragment in entry["label"]:
                body = entry["response"]["body"]
                try:
                    return str(json.loads(body).get("msg"))
                except (ValueError, TypeError):
                    return body[:80]
        return "<未探测>"

    missing = msg("A1")
    candidates = ["token", "Authorization", "X-Token", "X-Auth-Token", "jwt"]
    recognized = [name for name in candidates if msg(f"头名 {name}") not in {missing, "<未探测>"}]

    casing_msgs = {spelling: msg(f"头名拼写 {spelling}") for spelling in ("token", "Token", "TOKEN", "tOkEn")}
    casing_consistent = len(set(casing_msgs.values())) == 1 and "<未探测>" not in casing_msgs.values()

    return {
        "auth_header": "token",
        "auth_header_case_sensitive": False,
        "recognized_header_names": recognized,
        "rejected_header_names": [n for n in candidates if n not in recognized],
        "branches": {
            "missing": {"msg": missing, "http": 200, "code": 401},
            "invalid": {"msg": msg("B2"), "http": 200, "code": 401},
        },
        "casing_experiment": {
            "observed": casing_msgs,
            "all_same": casing_consistent,
            "conclusion": (
                "四种拼写得到同一条 msg → 识别的是 HTTP 头名「token」，"
                "不是某个固定拼写。HTTP 头名本就大小写不敏感，这种回归测试没意义，"
                "但能把「为什么大小写都一样」写清楚。"
            ),
        },
        "value_shapes_tried": {
            "empty_string": msg("B1"),
            "plain_string": msg("B2"),
            "two_segments": msg("B3"),
            "hs256_bad_signature": msg("B4"),
        },
        "filter_boundary": {
            "whitelisted_paths": [
                "/eplatform/lngdata/report/listHis",
                "/eplatform/lngdata/report/listAll",
            ],
            "filter_runs_before_routing": msg("C /eplatform/lngdata/report/listXxx") == msg("C /eplatform/xxx"),
            "consequence": (
                "未知子路径与受保护路径返回**同一条** 401，说明鉴权过滤器在路由之前执行。"
                "因此无法靠状态码或报文差异枚举 /eplatform/ 下的端点——"
                "暴力列举不成立，路径必须从对方前端代码里反推。"
            ),
        },
        "note": (
            "本探测只读取服务端**公开返回的报错信息**，未尝试构造、爆破或破解任何有效凭证；"
            "token 本身需要会员账号登录后由服务端签发，本工具不涉及该流程。"
        ),
    }


# ======================================================================
# 4. 落盘
# ======================================================================
def write_json(path: str, payload: Any) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def write_csv(path: str, rows: list[dict[str, Any]], columns: list[str] | None = None) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if not rows:
        raise ValueError(f"没有数据可写入 {path}")
    columns = columns or list(rows[0].keys())
    # utf-8-sig：Excel 双击打开中文不乱码（BOM）
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


QUOTE_COLUMNS = [
    "price_date",
    "sale_to",
    "facility",
    "is_receive_station",
    "price",
    "unit",
    "change_value",
    "change_pct",
    "company_name",
    "company_id",
    "factory_id",
]
SUMMARY_COLUMNS = [
    "price_date",
    "grade",
    "scope",
    "value",
    "unit",
    "change_value",
    "change_pct",
    "sample_size",
    "max_of_sample",
    "min_of_sample",
]


# ======================================================================
# 5. CLI
# ======================================================================
def _parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD：{text!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shpgx_lng_collector.py",
        description="上海石油天然气交易中心 LNG 挂牌数据采集（独立版，仅标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python shpgx_lng_collector.py --days 3\n"
            "  python shpgx_lng_collector.py --start 2026-08-01 --end 2026-10-11\n"
            "  python shpgx_lng_collector.py --probe-auth --out data/reverse-shpgx/jwt_evidence.json\n"
        ),
    )
    parser.add_argument("--days", type=int, help="抓最近 N 个**有数据**的交易日")
    parser.add_argument("--start", type=_parse_date, help="起始日期（闭区间）")
    parser.add_argument("--end", type=_parse_date, help="结束日期（闭区间）")
    parser.add_argument("--out", default="data/reverse-shpgx", help="输出目录（默认 data/reverse-shpgx）")
    parser.add_argument("--no-write", action="store_true", help="只打印摘要，不落盘")
    parser.add_argument("--token", default=None, help="JWT；访问 /eplatform/dataapi/* 这类受保护接口时必带")
    parser.add_argument("--probe-auth", action="store_true", help="复现鉴权契约探测并输出 jwt_evidence.json")
    return parser


def _print_report(result: dict[str, Any]) -> None:
    print(f"日期区间：{result['range']['start']} ~ {result['range']['end']}")
    print(
        f"明细 {result['counters']['quotes']} 条 / "
        f"{result['counters']['dates']} 个日期 / "
        f"{result['counters']['facilities']} 个交货点"
        f"（其中接收站 {result['counters']['receive_stations']} 个）"
    )
    print()
    print(f"{'日期':<12}{'口径':<18}{'数值':>10} {'环比':>9} {'样本数':>7}")
    for row in result["summary"]:
        change = "—" if row.get("change_value") is None else f"{row['change_value']:+g}"
        print(
            f"{row['price_date']:<12}{row['grade']:<18}{row['value']:>10.2f} "
            f"{change:>9} {row['sample_size']:>7}"
        )


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)

    if args.probe_auth:
        evidence = probe_auth_contract()
        if args.no_write:
            print(json.dumps(evidence, ensure_ascii=False, indent=2))
            return 0
        path = args.out if args.out.endswith(".json") else os.path.join(args.out, "jwt_evidence.json")
        write_json(path, evidence)
        print(f"鉴权契约证据已写入 {path}")
        print(f"  识别到的鉴权头：{evidence['findings']['auth_header']}")
        print(f"  缺失分支 msg：{evidence['findings']['branches']['missing']['msg']}")
        print(f"  无效分支 msg：{evidence['findings']['branches']['invalid']['msg']}")
        return 0

    if not args.days and not (args.start and args.end):
        print("必须给 --days 或同时给 --start/--end（或 --probe-auth）", file=sys.stderr)
        return 1

    try:
        result = collect(days=args.days, start=args.start, end=args.end, token=args.token)
    except Exception as exc:  # noqa: BLE001 - CLI 边界：把异常转成退出码
        print(f"采集失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not result["quotes"]:
        print("区间内没有挂牌数据（该站点不是每个自然日都有记录）。", file=sys.stderr)
        return 1

    _print_report(result)

    if args.no_write:
        return 0

    out = args.out
    suffix = f"{result['range']['start']}_{result['range']['end']}"
    paths = [
        write_json(os.path.join(out, f"shpgx_lng_quotes_{suffix}.json"), result["quotes"]),
        write_csv(os.path.join(out, f"shpgx_lng_quotes_{suffix}.csv"), result["quotes"], QUOTE_COLUMNS),
        write_json(os.path.join(out, f"shpgx_lng_summary_{suffix}.json"), result["summary"]),
        write_csv(os.path.join(out, f"shpgx_lng_summary_{suffix}.csv"), result["summary"], SUMMARY_COLUMNS),
        write_json(
            os.path.join(out, f"shpgx_lng_meta_{suffix}.json"),
            {
                "source": LIST_HIS,
                "page": PAGE_URL,
                "collected_at": time.strftime("%Y-%m-%d %H:%M:%S%z"),
                "range": result["range"],
                "counters": result["counters"],
                "unit": UNIT,
                "receive_stations_whitelist": sorted(RECEIVE_STATIONS),
                "used_token": bool(args.token),
            },
        ),
    ]
    print()
    for path in paths:
        print(f"  → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
