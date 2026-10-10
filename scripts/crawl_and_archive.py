"""真实抓取 + 按「省份 / 站点」分级归档导出。

用途
----
不依赖 MySQL/Redis，直接驱动适配器联网抓取，把结果按

    <输出根>/<省份>（省级大区）/<站点来源>/
        ├── articles.json      本来源全部文章（结构化）
        ├── articles.csv       Excel 可直开（UTF-8 BOM）
        └── index.md           人读目录（标题 / 时间 / 链接）

落盘归档，同时合并产出一份跨来源总表 ``_all_articles.csv``。

设计要点
--------
- **网络环境适配**：沙箱/本机存在 ``http_proxy`` 注入时国网站点 TLS 会失败，
  脚本内主动清理代理环境变量并按来源直连（``trust_env=False``）。
- **失败不中断**：单来源抓取异常只记录到 ``_crawl_report.json``，不影响其余来源。
- **并发控制**：Scrapy 之外用轻量 :class:`httpx.Client` 并发，逐来源限速。

用法::

    python scripts/crawl_and_archive.py                 # 抓全部可用来源
    python scripts/crawl_and_archive.py --source bjx    # 只抓指定来源
    python scripts/crawl_and_archive.py --out data/power_news
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 环境准备：必须在 import httpx 之前清理代理变量
# ---------------------------------------------------------------------------
for _key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
             "all_proxy", "ALL_PROXY"):
    os.environ.pop(_key, None)

#: 项目根目录（本脚本位于 scripts/ 下）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

from newsagg.adapters import available_adapters, ensure_loaded, get_adapter  # noqa: E402
from newsagg.provinces import PROVINCE_BY_CODE  # noqa: E402
from newsagg.sources_seed import CATEGORY_LABELS, SOURCE_BY_ID  # noqa: E402

__all__ = ["Article", "CrawlResult", "crawl_source", "main"]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

#: 列表页候选补充：部分电网站群 HTTPS 被网络策略阻断、仅 HTTP 可达，
#: 或来源清单里的栏目路径在新版站群下已失效。探测时按顺序尝试。
FALLBACK_PATHS: tuple[str, ...] = (
    "/xwzx/",     # 新闻中心（新版站群主入口）
    "/p1/",       # 公司要闻（旧版默认）
    "/",          # 首页兜底
)


def candidate_list_urls(primary: str) -> list[str]:
    """为一个来源构造列表页候选 URL（协议降级 + 路径备选）。

    Args:
        primary: 适配器声明的列表页 URL。

    Returns:
        去重后的候选 URL 列表，首个仍是原始 primary。

    Note:
        - **协议降级**：国网站群 HTTPS 在部分网络下被阻断，HTTP 可通，
          故同路径的 http/https 互为备选。
        - **路径备选**：新版站群把 ``/p1/`` 迁到 ``/xwzx/``，旧来源清单
          未同步，故追加常见栏目路径。
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(primary)
    scheme_flip = "http" if parts.scheme == "https" else "https"
    host = parts.netloc
    base_path = parts.path or "/"

    urls: list[str] = [primary, urlunsplit(
        (scheme_flip, parts.netloc, parts.path, parts.query, parts.fragment)
    )]
    # 路径备选（两种协议都试）
    for scheme in (parts.scheme, scheme_flip):
        for path in FALLBACK_PATHS:
            if path != base_path:
                urls.append(f"{scheme}://{host}{path}")

    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


#: 每个来源最多抓取的文章数（避免一次性把站点抓穿）
DEFAULT_LIMIT = 30
#: 单来源请求间隔（秒），礼貌限速
REQUEST_INTERVAL = 0.4

#: 详情页 <h1> 常见为「栏目名」而非文章标题，命中则回退用列表页标题
NAV_WORDS: frozenset[str] = frozenset({
    "新闻中心", "公司要闻", "一线风采", "媒体聚焦", "通知公告", "首页",
    "关于我们", "商务服务", "联系我们", "更多", "更多>>", "集团动态",
    "公司新闻", "一线传真", "视频新闻", "动态要闻", "要闻动态", "行业动态",
    "能源要闻", "能源动态", "发改动态", "集团要闻", "公司动态", "新闻稿",
})

#: 非文章链接后缀（附件/文档）
NON_ARTICLE_EXT: tuple[str, ...] = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".ppt", ".pptx",
)


def pick_title(detail_title: str | None, list_title: str | None) -> str:
    """择优选取标题：详情页标题若是栏目名，则回退列表页标题。

    Args:
        detail_title: 详情页解析出的标题（常为 ``<h1>``，可能是栏目名）。
        list_title: 列表页解析出的标题（更接近真实文章标题）。

    Returns:
        最终采用的标题。
    """
    dt = (detail_title or "").strip()
    lt = (list_title or "").strip()
    if lt and (not dt or dt in NAV_WORDS):
        return lt
    return dt or lt


def is_non_article(url: str) -> bool:
    """判断链接是否为附件（PDF/Office 等）而非文章页。"""
    return url.lower().split("?")[0].endswith(NON_ARTICLE_EXT)


@dataclass(slots=True)
class Article:
    """一条抓取到的新闻。"""

    source_id: str
    source_name: str
    province_code: str
    province_name: str
    category: str
    category_label: str
    title: str
    url: str
    published_text: str
    source_field: str
    paragraphs: list[str] = field(default_factory=list)
    fetched_at: str = ""

    @property
    def body(self) -> str:
        """正文全文（段落以换行拼接）。"""
        return "\n".join(self.paragraphs)

    def to_row(self) -> dict[str, Any]:
        """转为 CSV 行（中文表头）。"""
        return {
            "省份": self.province_name,
            "行政区划代码": self.province_code,
            "来源类别": self.category_label,
            "来源站点": self.source_name,
            "标题": self.title,
            "发布时间": self.published_text,
            "正文": self.body,
            "原文链接": self.url,
            "抓取时间": self.fetched_at,
        }


@dataclass(slots=True)
class CrawlResult:
    """单来源抓取结果。"""

    source_id: str
    source_name: str
    province_name: str
    list_url: str
    ok: bool
    found: int = 0          # 列表页发现的候选数
    saved: int = 0          # 成功解析并入库的文章数
    error: str = ""
    articles: list[Article] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
def _province_name(code: str | None) -> str:
    """省份代码 → 全称；无代码归入「全国 / 行业」。"""
    if not code:
        return "全国_行业"
    prov = PROVINCE_BY_CODE.get(code)
    return prov.name if prov else f"其他_{code}"


def fetch(client: httpx.Client, url: str, *, retries: int = 2) -> str | None:
    """抓取单个 URL，失败重试。"""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = client.get(url, follow_redirects=True)
            if resp.status_code == 200:
                return resp.text
            last = RuntimeError(f"HTTP {resp.status_code}")
            if resp.status_code in (403, 404, 410, 412):
                break
        except Exception as exc:  # noqa: BLE001 - 网络异常统一重试
            last = exc
        time.sleep(REQUEST_INTERVAL * (attempt + 1))
    if last:
        raise last
    return None


def crawl_source(
    source_id: str,
    *,
    limit: int = DEFAULT_LIMIT,
    interval: float = REQUEST_INTERVAL,
) -> CrawlResult:
    """抓取单个来源：列表页 → 逐条详情页。

    Args:
        source_id: 适配器 id。
        limit: 最多抓取的文章数。
        interval: 详情页之间的请求间隔（秒）。

    Returns:
        :class:`CrawlResult`，含抓取到的文章列表。
    """
    adapter = get_adapter(source_id)
    meta = SOURCE_BY_ID.get(source_id, {})
    prov_code = meta.get("province_code")
    prov_name = _province_name(prov_code)
    list_url = adapter.list_url or adapter.column_url

    result = CrawlResult(
        source_id=source_id,
        source_name=adapter.source_name or source_id,
        province_name=prov_name,
        list_url=list_url,
        ok=False,
    )

    timeout = httpx.Timeout(20.0, connect=15.0)
    with httpx.Client(headers=HEADERS, timeout=timeout, verify=False,
                      trust_env=False, http2=False) as client:
        # 逐个候选 URL 探测：直到找到一个能解析出候选条目的列表页
        html: str | None = None
        last_err = ""
        for cand in candidate_list_urls(list_url):
            try:
                body = fetch(client, cand, retries=1)
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}"
                continue
            if not body:
                last_err = "空响应"
                continue
            try:
                trial = adapter.parse_list(body, base_url=cand)
            except Exception as exc:  # noqa: BLE001
                last_err = f"解析{type(exc).__name__}"
                continue
            if trial.items:
                html, list_url = body, cand  # 命中，锁定该 URL 作为 base
                result.list_url = cand
                break
            last_err = "无候选"

        if not html:
            result.error = f"列表页失败: {last_err or '所有候选URL均失败'}"
            return result

        parsed = adapter.parse_list(html, base_url=list_url)
        result.found = len(parsed.items)
        if not parsed.items:
            result.error = "列表页无候选（路径失效或反爬）"
            return result

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        usable = [it for it in parsed.items if not is_non_article(it.url)]
        for item in usable[:limit]:
            try:
                detail_html = fetch(client, item.url)
                if not detail_html:
                    continue
                detail = adapter.parse_detail(detail_html, url=item.url)
            except Exception:  # noqa: BLE001 - 单条失败跳过
                continue

            paragraphs = [p.strip() for p in detail.paragraphs if p and p.strip()]
            title = pick_title(detail.title, item.title)
            if not title and not paragraphs:
                continue

            result.articles.append(
                Article(
                    source_id=source_id,
                    source_name=adapter.source_name or source_id,
                    province_code=prov_code or "",
                    province_name=prov_name,
                    category=meta.get("category", adapter.category),
                    category_label=CATEGORY_LABELS.get(
                        meta.get("category", adapter.category), ""
                    ),
                    title=title or "(无标题)",
                    url=item.url,
                    published_text=(detail.published_text or item.list_time_text or "").strip(),
                    source_field=detail.source_name or "",
                    paragraphs=paragraphs,
                    fetched_at=now,
                )
            )
            time.sleep(interval)

        result.saved = len(result.articles)
        result.ok = result.saved > 0
        if not result.ok:
            result.error = "详情页全部解析为空"
        return result


# ---------------------------------------------------------------------------
# 归档
# ---------------------------------------------------------------------------
def _safe_name(text: str) -> str:
    """把名称规范为安全的目录名。"""
    text = re.sub(r"[\\/:*?\"<>|\s]+", "_", text.strip())
    return text.strip("_") or "unnamed"


def archive(results: Iterable[CrawlResult], out_root: Path) -> dict[str, Any]:
    """把抓取结果按「省份 / 站点」两级目录归档。

    Args:
        results: 各来源的抓取结果。
        out_root: 输出根目录。

    Returns:
        归档摘要（用于写 ``_crawl_report.json``）。
    """
    out_root.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "output_root": str(out_root),
        "sources": [],
        "totals": {"sources_ok": 0, "sources_failed": 0, "articles": 0},
    }

    for res in results:
        entry: dict[str, Any] = {
            "source_id": res.source_id,
            "source_name": res.source_name,
            "province": res.province_name,
            "list_url": res.list_url,
            "found": res.found,
            "saved": res.saved,
            "ok": res.ok,
            "error": res.error,
        }

        if not res.ok:
            report["sources"].append(entry)
            report["totals"]["sources_failed"] += 1
            continue

        # 目录：<输出根>/<省份>/<站点来源>/
        prov_dir = out_root / _safe_name(res.province_name)
        src_dir = prov_dir / _safe_name(res.source_name)
        src_dir.mkdir(parents=True, exist_ok=True)

        rows = [a.to_row() for a in res.articles]
        # ---- JSON ----
        payload = {
            "source_id": res.source_id,
            "source_name": res.source_name,
            "province": res.province_name,
            "list_url": res.list_url,
            "article_count": len(res.articles),
            "crawled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "articles": [
                {
                    "title": a.title,
                    "url": a.url,
                    "published": a.published_text,
                    "source_field": a.source_field,
                    "body": a.body,
                    "paragraphs": a.paragraphs,
                }
                for a in res.articles
            ],
        }
        (src_dir / "articles.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # ---- CSV（UTF-8 BOM，Excel 直开）----
        if rows:
            with open(src_dir / "articles.csv", "w", encoding="utf-8-sig",
                      newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)

        # ---- index.md（人读目录）----
        lines = [
            f"# {res.source_name}",
            "",
            f"- **省份**：{res.province_name}",
            f"- **来源 id**：`{res.source_id}`",
            f"- **栏目页**：{res.list_url}",
            f"- **文章数**：{len(res.articles)}",
            f"- **抓取时间**：{payload['crawled_at']}",
            "",
            "## 文章列表",
            "",
        ]
        for idx, art in enumerate(res.articles, 1):
            when = art.published_text or "未知时间"
            lines.append(f"{idx}. [{art.title}]({art.url}) — {when}")
        (src_dir / "index.md").write_text("\n".join(lines), encoding="utf-8")

        all_rows.extend(rows)
        entry["dir"] = str(src_dir)
        report["sources"].append(entry)
        report["totals"]["sources_ok"] += 1
        report["totals"]["articles"] += len(res.articles)

    # ---- 跨来源总表 ----
    if all_rows:
        with open(out_root / "_all_articles.csv", "w", encoding="utf-8-sig",
                  newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
            writer.writeheader()
            writer.writerows(all_rows)

    # ---- 报告合并写：保留其它批次的来源结果，避免相互覆盖 ----
    report_path = out_root / "_crawl_report.json"
    merged_sources: dict[str, dict[str, Any]] = {}
    if report_path.exists():
        try:
            old = json.loads(report_path.read_text(encoding="utf-8"))
            for e in old.get("sources", []):
                if e.get("source_id"):
                    merged_sources[e["source_id"]] = e
        except Exception:  # noqa: BLE001
            pass
    for e in report["sources"]:
        if e.get("source_id"):
            merged_sources[e["source_id"]] = e
    merged_list = sorted(merged_sources.values(), key=lambda x: x.get("source_id", ""))
    report["sources"] = merged_list
    report["totals"] = {
        "sources_ok": sum(1 for e in merged_list if e.get("ok")),
        "sources_failed": sum(1 for e in merged_list if not e.get("ok")),
        "articles": sum(e.get("saved", 0) for e in merged_list),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _available_or_all(source_ids: list[str] | None) -> list[str]:
    """确定要抓的来源列表。"""
    ensure_loaded()
    if source_ids:
        return source_ids
    return available_adapters()


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="真实抓取电力新闻并按「省份 / 站点」分级归档",
    )
    parser.add_argument("--source", action="append", metavar="ID",
                        help="只抓指定来源（可重复）；默认全部")
    parser.add_argument("--out", default="data/power_news", metavar="DIR",
                        help="输出根目录（默认 data/power_news）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"每个来源最多抓取文章数（默认 {DEFAULT_LIMIT}）")
    parser.add_argument("--interval", type=float, default=REQUEST_INTERVAL,
                        help="详情页请求间隔秒数")
    parser.add_argument("--workers", type=int, default=6, help="并发来源数")
    args = parser.parse_args(argv)

    ids = _available_or_all(args.source)
    print(f"待抓来源 {len(ids)} 个，并发 {args.workers}，单源上限 {args.limit} 篇")
    print("-" * 64)

    results: list[CrawlResult] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(crawl_source, sid, limit=args.limit, interval=args.interval): sid
            for sid in ids
        }
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                res = CrawlResult(sid, sid, "-", "", False, error=f"{type(exc).__name__}: {exc}")
            results.append(res)
            flag = "✓" if res.ok else "✗"
            note = f"{res.saved}/{res.found} 篇" if res.ok else res.error[:48]
            print(f"  {flag} {sid:<16} {note}")

    results.sort(key=lambda r: (-r.saved, r.source_id))
    out_root = Path(args.out)
    report = archive(results, out_root)

    print("-" * 64)
    print(f"成功来源 {report['totals']['sources_ok']} 个，"
          f"失败 {report['totals']['sources_failed']} 个，"
          f"共归档 {report['totals']['articles']} 篇文章")
    print(f"输出目录: {out_root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
