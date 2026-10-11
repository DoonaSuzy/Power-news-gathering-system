"""基于 CDP（真实 Chrome）的瑞数站点抓取器。

原理
----
瑞数动态防护（412）需要真实浏览器完成 JS 挑战并回传 Cookie。
本脚本驱动一个真实 Chrome：
  1. 打开列表页 → 等挑战通过、DOM 渲染完成 → 取 outerHTML；
  2. 复用项目既有适配器 ``parse_list`` / ``parse_detail`` 解析（浏览器只负责取 HTML）；
  3. 逐条抓详情页，归档到 ``data/power_news/<省份>/<站点>/``。

关键环境（启动 Chrome 时必须带）：
  --user-agent="Mozilla/5.0 ... Chrome/154.0.0.0 Safari/537.36"   ← 去掉 HeadlessChrome
  --disable-blink-features=AutomationControlled                  ← 关闭 navigator.webdriver
  --remote-debugging-port=9222

用法：
    python scripts/cdp_crawl.py --source sgcc_bj --limit 5
    python scripts/cdp_crawl.py --group sgcc412 --limit 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

for _key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
             "all_proxy", "ALL_PROXY"):
    os.environ.pop(_key, None)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import websocket  # type: ignore  # noqa: E402

from newsagg.adapters import ensure_loaded, get_adapter  # noqa: E402
from newsagg.sources_seed import CATEGORY_LABELS, SOURCE_BY_ID  # noqa: E402
from scripts.crawl_and_archive import (  # noqa: E402
    Article,
    CrawlResult,
    _province_name,
    candidate_list_urls,
    is_non_article,
    pick_title,
)

CDP = os.environ.get("CDP_URL", "http://127.0.0.1:9222")

#: 需要浏览器攻克的来源（瑞数 412 / 202 风控 / SPA）
GROUPS: dict[str, list[str]] = {
    "sgcc412": [
        "sgcc_bj", "sgcc_sd", "sgcc_zj", "sgcc_ah", "sgcc_fj", "sgcc_hb",
        "sgcc_jl", "sgcc_hl", "sgcc_gs", "sgcc_qh", "sgcc_nx", "sgcc_xj", "sgcc_md",
    ],
    "sgcc202": ["sgcc_sh", "sgcc_ha"],
    "sgccmisc": ["sgcc_sx", "sgcc_sn", "sgcc_xz"],
    "csg": ["csg_gd", "csg_gx", "csg_gz", "csg_hn", "csg_yn"],
    "hmt": ["cem_macau", "clp_hk"],
}


class CDPPage:
    """一个 CDP 页面会话（含事件收集）。"""

    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self._id = 0
        self._responses: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._stop = False
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self) -> None:
        while not self._stop:
            try:
                raw = self.ws.recv()
            except Exception:  # noqa: BLE001
                break
            try:
                data = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            with self._lock:
                if "id" in data:
                    self._responses[data["id"]] = data

    def send(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if mid in self._responses:
                    return self._responses.pop(mid)
            time.sleep(0.02)
        return {}

    def eval(self, expr: str):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return r.get("result", {}).get("result", {}).get("value")

    def navigate(self, url: str, *, min_len: int = 2000, timeout: float = 35) -> str:
        """导航到 URL 并等待瑞数挑战通过 + DOM 渲染，返回 outerHTML。"""
        self.send("Page.navigate", {"url": url})
        deadline = time.time() + timeout
        best = ""
        stable = 0
        while time.time() < deadline:
            time.sleep(1.0)
            html = self.eval("document.documentElement.outerHTML") or ""
            if len(html) > len(best):
                best = html
            # 判定渲染完成：html 足够长且连续 2 次长度不再增长
            if len(html) >= min_len:
                if len(html) == stable:
                    return html
                stable = len(html)
            else:
                stable = 0
        return best

    def close(self) -> None:
        self._stop = True
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def new_page(url: str = "about:blank") -> tuple[CDPPage, str]:
    """新建标签页，返回 (页面会话, target_id)。"""
    r = httpx.put(f"{CDP}/json/new?{url}", timeout=10, trust_env=False)
    target = r.json()
    return CDPPage(target["webSocketDebuggerUrl"]), target["id"]


def close_page(tid: str) -> None:
    try:
        httpx.get(f"{CDP}/json/close/{tid}", timeout=5, trust_env=False)
    except Exception:  # noqa: BLE001
        pass


def crawl_source_via_browser(source_id: str, *, limit: int = 20) -> CrawlResult:
    """用浏览器抓取单个来源。"""
    adapter = get_adapter(source_id)
    meta = SOURCE_BY_ID.get(source_id, {})
    prov_name = _province_name(meta.get("province_code"))
    primary = adapter.list_url or adapter.column_url

    result = CrawlResult(source_id=source_id, source_name=adapter.source_name or source_id,
                         province_name=prov_name, list_url=primary, ok=False)

    page, tid = new_page()
    try:
        # 1) 找可解析的列表页
        _html, locked_url, items = "", "", []
        last_err = ""
        for cand in candidate_list_urls(primary):
            body = page.navigate(cand, min_len=2000, timeout=30)
            if not body:
                last_err = "空响应"
                continue
            try:
                parsed = adapter.parse_list(body, base_url=cand)
            except Exception as exc:  # noqa: BLE001
                last_err = f"解析{type(exc).__name__}"
                continue
            if parsed.items:
                _html, locked_url, items = body, cand, parsed.items
                break
            last_err = "无候选"

        if not items:
            result.error = f"浏览器列表页失败: {last_err or '所有候选URL均失败'}"
            return result

        result.list_url = locked_url
        result.found = len(items)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        code = meta.get("province_code")

        usable = [it for it in items if not is_non_article(it.url)]
        for item in usable[:limit]:
            detail_html = page.navigate(item.url, min_len=1500, timeout=30)
            if not detail_html:
                continue
            try:
                detail = adapter.parse_detail(detail_html, url=item.url)
            except Exception:  # noqa: BLE001
                continue
            paragraphs = [p.strip() for p in detail.paragraphs if p and p.strip()]
            title = pick_title(detail.title, item.title)
            if not title and not paragraphs:
                continue
            result.articles.append(
                Article(
                    source_id=source_id,
                    source_name=adapter.source_name or source_id,
                    province_code=code or "",
                    province_name=prov_name,
                    category=meta.get("category", adapter.category),
                    category_label=CATEGORY_LABELS.get(
                        meta.get("category", adapter.category), ""),
                    title=title or "(无标题)",
                    url=item.url,
                    published_text=(detail.published_text or item.list_time_text or "").strip(),
                    source_field=detail.source_name or "",
                    paragraphs=paragraphs,
                    fetched_at=now,
                )
            )
            time.sleep(0.4)

        result.saved = len(result.articles)
        result.ok = result.saved > 0
        if not result.ok:
            result.error = "详情页全部解析为空"
        return result
    finally:
        page.close()
        close_page(tid)


def _safe_name(text: str) -> str:
    import re
    return (re.sub(r"[\\/:*?\"<>|\s]+", "_", text.strip()).strip("_") or "unnamed")


def archive_one(res: CrawlResult, out_root: Path) -> dict:
    """归档单个来源（写入 <省份>/<站点>/），返回报告条目。"""
    entry = {"source_id": res.source_id, "source_name": res.source_name,
             "province": res.province_name, "list_url": res.list_url,
             "found": res.found, "saved": res.saved, "ok": res.ok, "error": res.error}
    if not res.ok:
        return entry

    src_dir = out_root / _safe_name(res.province_name) / _safe_name(res.source_name)
    src_dir.mkdir(parents=True, exist_ok=True)
    rows = [a.to_row() for a in res.articles]

    payload = {
        "source_id": res.source_id, "source_name": res.source_name,
        "province": res.province_name, "list_url": res.list_url,
        "article_count": len(res.articles),
        "crawled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "crawler": "cdp-browser",
        "articles": [
            {"title": a.title, "url": a.url, "published": a.published_text,
             "source_field": a.source_field, "body": a.body, "paragraphs": a.paragraphs}
            for a in res.articles
        ],
    }
    (src_dir / "articles.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    import csv
    if rows:
        with (src_dir / "articles.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    lines = [f"# {res.source_name}", "", f"- **省份**：{res.province_name}",
             f"- **来源 id**：`{res.source_id}`", f"- **栏目页**：{res.list_url}",
             f"- **文章数**：{len(res.articles)}",
             f"- **抓取时间**：{payload['crawled_at']}",
             "- **抓取方式**：CDP 真实浏览器（瑞数挑战通过）", "", "## 文章列表", ""]
    for idx, art in enumerate(res.articles, 1):
        lines.append(f"{idx}. [{art.title}]({art.url}) — {art.published_text or '未知时间'}")
    (src_dir / "index.md").write_text("\n".join(lines), encoding="utf-8")

    entry["dir"] = str(src_dir)
    return entry


def main() -> int:
    ap = argparse.ArgumentParser(description="CDP 浏览器抓取瑞数站点")
    ap.add_argument("--source", action="append", metavar="ID")
    ap.add_argument("--group", choices=list(GROUPS), default=None)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out", default="data/power_news")
    ap.add_argument("--workers", type=int, default=2, help="并发标签页数")
    args = ap.parse_args()

    ensure_loaded()
    ids: list[str] = []
    if args.source:
        ids = args.source
    elif args.group:
        ids = GROUPS[args.group]
    else:
        ids = GROUPS["sgcc412"]

    # 连通性自检
    try:
        v = httpx.get(f"{CDP}/json/version", timeout=8, trust_env=False).json()
        print(f"[CDP] {CDP} -> {v.get('Browser')}")
        print(f"[CDP] UA: {v.get('User-Agent')}")
        if "HeadlessChrome" in (v.get("User-Agent") or ""):
            print("[!] 警告：浏览器 UA 仍含 HeadlessChrome，瑞数很可能拦截！")
    except Exception as exc:  # noqa: BLE001
        print(f"[✗] 无法连接 CDP {CDP}: {exc}")
        return 2

    print(f"待抓 {len(ids)} 个来源，并发标签页 {args.workers}，单源上限 {args.limit} 篇")
    print("-" * 64)

    out_root = Path(args.out)
    entries: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(crawl_source_via_browser, sid, limit=args.limit): sid for sid in ids}
        for fut in as_completed(futs):
            sid = futs[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                res = CrawlResult(sid, sid, "-", "", False, error=f"{type(exc).__name__}: {exc}")
            entry = archive_one(res, out_root)
            entries.append(entry)
            flag = "✓" if res.ok else "✗"
            note = f"{res.saved}/{res.found} 篇" if res.ok else res.error[:56]
            print(f"  {flag} {sid:<14} {note}")

    report_path = out_root / "_cdp_report.json"
    # 与既有报告合并，避免后续批次覆盖前一批结果
    merged: dict[str, dict] = {}
    if report_path.exists():
        try:
            old = json.loads(report_path.read_text(encoding="utf-8"))
            for e in old.get("entries", []):
                if e.get("source_id"):
                    merged[e["source_id"]] = e
        except Exception:  # noqa: BLE001
            pass
    for e in entries:
        if e.get("source_id"):
            merged[e["source_id"]] = e
    merged_list = sorted(merged.values(), key=lambda x: x.get("source_id", ""))

    report_path.write_text(
        json.dumps({"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "entries": merged_list,
                    "totals": {"ok": sum(1 for e in merged_list if e.get("ok")),
                               "articles": sum(e.get("saved", 0) for e in merged_list)}},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for e in entries if e["ok"])
    total = sum(e["saved"] for e in entries)
    print("-" * 64)
    print(f"成功 {ok}/{len(entries)} 个来源，共 {total} 篇")
    print(f"报告: {out_root / '_cdp_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
