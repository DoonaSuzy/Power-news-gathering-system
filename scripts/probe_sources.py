"""全来源实时探活：逐条探测 sources_seed 的每个来源，输出结构化 JSON。

用于为「省份攻克日志」提供当前权威状态依据，也用于判断 412 / 可达性。

用法：
    python scripts/probe_sources.py                 # 探测全部
    python scripts/probe_sources.py --category sgcc # 只探测国网
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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

from newsagg.sources_seed import ALL_SOURCES  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}

RIVER_MARKERS = ("$_ts", "r='m'", "yCTxq", "riversecurity")


def classify(status: int, length: int, body: str) -> str:
    """把响应归类为可直接理解的类型。"""
    if status == 412 or (status == 200 and any(m in body for m in RIVER_MARKERS)):
        return "瑞数动态防护(412/挑战)"
    if status == 202:
        return "202-排队/风控"
    if status in (403, 401):
        return "403/401-拒绝"
    if status == 404:
        return "404-路径失效"
    if status == 200 and length < 1500:
        return "SPA外壳(需JS渲染)"
    if status == 200:
        return "200-正常"
    return f"HTTP-{status}"


def probe_once(client: httpx.Client, url: str) -> dict:
    try:
        r = client.get(url, follow_redirects=True)
        return {
            "ok": True,
            "status": r.status_code,
            "length": len(r.text),
            "final_url": str(r.url),
            "kind": classify(r.status_code, len(r.text), r.text),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": 0,
            "length": 0,
            "final_url": url,
            "kind": f"连接失败:{type(exc).__name__}",
        }


def probe_source(source: dict) -> dict:
    base = source["base_url"].rstrip("/")
    path = source.get("news_path") or "/"
    if not path.startswith("/"):
        path = "/" + path
    https_url = base + path
    http_url = "http://" + base.split("://", 1)[-1] + path

    timeout = httpx.Timeout(12.0, connect=10.0)
    out = {
        "id": source["id"],
        "name": source["name"],
        "category": source["category"],
        "province_code": source["province_code"],
        "list_url": https_url,
        "probes": [],
    }
    with httpx.Client(headers=HEADERS, timeout=timeout, verify=False,
                      trust_env=False, http2=False) as client:
        for scheme, url in (("https", https_url), ("http", http_url)):
            res = probe_once(client, url)
            res["scheme"] = scheme
            out["probes"].append(res)
            time.sleep(0.2)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default=None)
    ap.add_argument("--out", default="data/power_news/_probe_report.json")
    args = ap.parse_args()

    sources = ALL_SOURCES
    if args.category:
        sources = [s for s in sources if s["category"] == args.category]

    print(f"探测 {len(sources)} 个来源 ...")
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(probe_source, s): s for s in sources}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                results.append({"id": s["id"], "name": s["name"], "error": str(exc), "probes": []})

    results.sort(key=lambda r: r["id"])
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(results),
        "results": results,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'id':<16}{'https':<26}{'http':<26}")
    print("-" * 70)
    for r in results:
        cells = []
        for p in r["probes"]:
            cells.append(f"{p['status']}/{p['length']} {p['kind'][:10]}")
        while len(cells) < 2:
            cells.append("-")
        print(f"{r['id']:<16}{cells[0]:<26}{cells[1]:<26}")
    print("-" * 70)
    print(f"写入 {out_path}")


if __name__ == "__main__":
    main()
