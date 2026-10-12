"""anysearch 补充数据源：实时搜索电力新闻，补足本地库的覆盖缺口。

为什么需要它
------------
本地采集库有 1,201 篇、45 个来源，但**已证实有硬缺口**：
- 45 个启用源里本机可达仅 12 个、VM 可达 13 个；
- 北极星（bjx）详情页被阿里云 WAF 拦；
- 近 30 天有 5 个空白日（源站当天没发稿）。

这些缺口靠「再跑一遍同样的采集窗口」补不上。anysearch 是独立的检索服务，
能覆盖到本地爬虫够不着的站点，用来做**增量补充**。

与本地库的关系
-------------
- 本地库是主数据源（结构化、有时间戳、可回溯）；
- anysearch 是补充源（覆盖广、时效新，但只有标题+摘要，没有正文）；
- 两者合并后送 LLM 总结，并在响应里标明每条出自哪个来源，
  **不把搜索结果伪装成本地采集的数据**。

API：``POST https://api.anysearch.com/v1/search``（OpenAI 风格 Bearer，可匿名）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from newsagg.logutil import get_logger

log = get_logger("newsplatform.news_search")

API_BASE = (os.getenv("ANYSEARCH_API_BASE_URL") or "https://api.anysearch.com").rstrip("/")

#: 从摘要文本里找日期（``2026-10-10`` / ``2026年10月10日`` / ``2026/10/10``）。
_DATE_IN_TEXT = re.compile(r"(20\d{2})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})")

#: 默认搜索主题。选词原则：
#: 1. **用领域内的稳定名词**，不用同义词（同义词各命中一批，反而让每主题结果数偏少）；
#: 2. **混入时效词**（今日 / 最新 / 要闻）—— 实测纯名词查询会返回几年前的旧文，
#:    而本地库的硬缺口恰恰是「最近几天」，补旧文没有意义。
DEFAULT_QUERIES: tuple[str, ...] = (
    "电力新闻 今日",
    "电网 最新 动态",
    "新能源 政策 最新",
    "特高压 工程 投产 最新",
    "储能 并网 最新",
    "电力市场 电价 最新",
)

#: 单主题最多取几条。anysearch 的 max_results 上界是 10。
DEFAULT_PER_QUERY = 6

#: 相关性白名单：标题或摘要必须命中至少一个，否则丢弃。
#:
#: 实测踩到：查询「电网 最新 消息」返回了一堆**酒店**结果（英文 ``in`` 匹配了
#: ``INN``：Jinjiang Inn / 7 Days Inn / ENERGY INN TAIPEI），还有美股代码
#: ``INN`` 的行情页。不过滤的话这些会直接进 LLM 材料，污染公告内容。
RELEVANCE_TERMS: tuple[str, ...] = (
    "电力", "电网", "能源", "发电", "储能", "光伏", "风电", "水电", "核电", "煤电",
    "火电", "输电", "配电", "特高压", "电价", "用电", "负荷", "装机", "新能源",
    "可再生", "碳中和", "碳达峰", "碳交易", "绿电", "氢能", "充电桩", "虚拟电厂",
    "国家电网", "南方电网", "能源局", "电力市场", "迎峰度夏", "迎峰度冬",
    "power grid", "electricity", "renewable energy", "power plant",
)

#: 域名黑名单：命中即丢弃。收录的是实测出现的噪音源类型。
BLOCKED_HOSTS: tuple[str, ...] = (
    "trip.com", "skyscanner", "booking.com", "agoda", "hotels.com",
    "apps.microsoft.com", "youtube.com", "zhihu.com",
)


@dataclass(slots=True)
class SearchDoc:
    """一条搜索结果（归一后的形状）。"""

    key: str                 # 稳定编号，供 LLM 引用（如 "s1"）
    title: str
    source: str
    url: str
    excerpt: str
    published: str = ""
    origin: str = "anysearch"


@dataclass(slots=True)
class SearchResult:
    docs: list[SearchDoc] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.docs)


def _api_key() -> str:
    return (os.getenv("ANYSEARCH_API_KEY") or "").strip()


def search_electricity_news(
    *,
    queries: tuple[str, ...] | list[str] = DEFAULT_QUERIES,
    per_query: int = DEFAULT_PER_QUERY,
    timeout: float = 30.0,
) -> SearchResult:
    """同步版（脚本/测试用）。异步路径见 :func:`asearch_electricity_news`。"""
    import asyncio

    return asyncio.run(asearch_electricity_news(
        queries=queries, per_query=per_query, timeout=timeout,
    ))


async def asearch_electricity_news(
    *,
    queries: tuple[str, ...] | list[str] = DEFAULT_QUERIES,
    per_query: int = DEFAULT_PER_QUERY,
    timeout: float = 30.0,
) -> SearchResult:
    """并行搜索多个主题，返回合并去重后的结果。

    单个主题失败不影响其它主题 —— 补充数据源不该让整次预生成失败。
    """
    result = SearchResult(queries=list(queries))
    headers = {"Content-Type": "application/json"}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    seen_urls: set[str] = set()
    counter = 0

    # 真并行：6 个主题串行约 15 秒，并发约 3 秒。
    # 预生成在启动时跑，慢会拖住服务就绪。
    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = [
            _aone_query(client, f"{API_BASE}/v1/search", headers, q, per_query)
            for q in queries
        ]
        settled = await asyncio.gather(*tasks, return_exceptions=True)

    for query, outcome in zip(queries, settled, strict=False):
        if isinstance(outcome, BaseException):
            result.errors.append(f"{query}: {outcome}")
            continue
        status, body_text = outcome
        if status != 200:
            result.errors.append(f"{query}: HTTP {status}")
            continue
        try:
            body = json.loads(body_text)
        except ValueError as exc:
            result.errors.append(f"{query}: 响应非 JSON（{exc}）")
            continue

        for item in _extract_items(body):
            url = str(item.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            title = str(item.get("title") or "").strip()
            excerpt = str(item.get("snippet") or item.get("content") or "").strip()
            if not _is_relevant(title, excerpt, url):
                continue
            seen_urls.add(url)
            counter += 1
            published = str(item.get("date") or item.get("published") or "").strip() \
                or _date_from_text(excerpt) or _date_from_text(title)
            result.docs.append(SearchDoc(
                key=f"s{counter}",
                title=title,
                # 搜索结果常常不带 source 字段，只有 URL。从域名兜底推断，
                # 否则响应里每条都是「来源：未知」，失去了「数据来自哪」的信息。
                source=str(item.get("source") or item.get("site") or "").strip()
                or _site_of(url),
                url=url,
                excerpt=excerpt,
                published=published,
            ))

    if not result.docs and result.errors:
        log.warning("anysearch 全部主题失败：%s", result.errors)

    # 有日期的排前面、最新的在前；无日期的沉底。
    # 检索结果本身是按相关度排的，混着新旧；用户要的是「最近有什么」，
    # 所以按日期重排一次。字符串排序对 ``YYYY-MM-DD`` 是正确的。
    result.docs.sort(key=lambda d: d.published or "0000-00-00", reverse=True)
    # key 要在排序后按新顺序重编，否则材料编号与展示顺序不一致
    for i, doc in enumerate(result.docs, 1):
        doc.key = f"s{i}"
    return result


async def _aone_query(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    query: str,
    per_query: int,
) -> tuple[int, str]:
    resp = await client.post(
        url,
        headers=headers,
        json={"query": query, "max_results": max(1, min(10, per_query))},
    )
    return resp.status_code, resp.text


def _date_from_text(text: str) -> str:
    """从摘要文本里提取日期。

    **anysearch 的返回没有日期字段**（实测原始 JSON 只有
    ``title / url / snippet / content``），但发布时间常常嵌在摘要里，
    形如 ``…电价仅涨0.5%. 2026-10-10 ; …``。不提取的话「最近两天有没有新闻」
    这个问题就无法回答 —— 前端只能显示「无日期」。
    """
    m = _DATE_IN_TEXT.search(text or "")
    if not m:
        return ""
    year, month, day = m.group(1), int(m.group(2)), int(m.group(3))
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return ""
    return f"{year}-{month:02d}-{day:02d}"


def _site_of(url: str) -> str:
    """从 URL 取站点名兜底（``www.cpnn.com.cn`` → ``cpnn.com.cn``）。"""
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_relevant(title: str, excerpt: str, url: str) -> bool:
    """这条结果是否与电力/能源领域相关。

    两道闸：域名黑名单（实测噪音源）+ 领域词白名单（标题或摘要至少命中一个）。
    不做过滤的话，英文缩写误匹配（``in`` → ``INN`` 酒店）会直接进 LLM 材料。
    """
    host = _site_of(url)
    if any(bad in host for bad in BLOCKED_HOSTS):
        return False
    text = f"{title} {excerpt}".lower()
    return any(term.lower() in text for term in RELEVANCE_TERMS)


def _extract_items(body: Any) -> list[dict]:
    """从响应里取出结果列表。

    anysearch 的返回外层可能是 ``results`` / ``data`` / ``items``，
    也可能是裸数组。都试一遍，取到第一个非空列表即止 ——
    不猜死一种形状，否则对方一调整字段名这里就静默返回空。
    """
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if not isinstance(body, dict):
        return []
    for key in ("results", "data", "items", "organic"):
        value = body.get(key)
        if isinstance(value, list) and value:
            return [x for x in value if isinstance(x, dict)]
        # 有些实现会再套一层
        if isinstance(value, dict):
            for inner in ("results", "items", "list"):
                if isinstance(value.get(inner), list) and value[inner]:
                    return [x for x in value[inner] if isinstance(x, dict)]
    return []


def build_prompt_block(docs: list[SearchDoc], *, max_chars: int = 24000) -> str:
    """把搜索结果拼成给模型的材料块，带长度截断。"""
    parts: list[str] = []
    used = 0
    for doc in docs:
        block = (
            f"\n[{doc.key}] {doc.title}\n"
            f"来源：{doc.source or '未知'}  时间：{doc.published or '未知'}\n"
            f"{doc.excerpt[:400]}\n"
        )
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "".join(parts)
