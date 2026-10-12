"""修复北极星电力网（bjx）空正文新闻 —— 用真实 Chrome 绕过阿里云 WAF。

根因（2026-10-12 实测，非推测）
------------------------------
bjx 对**详情页**启用了阿里云 WAF 的 JS 挑战（``CF_APP_WAF``）。普通 HTTP 请求
返回的 HTTP 200 页面只有 16,010 字节，内容是挑战脚本，正文关键词零命中、
``<title>`` 为空。因此：

- **标题有** —— 来自列表页的 ``list_title``；
- **正文空** —— 详情页被 WAF 拦，采不到文本；
- **时间/来源也空** —— 详情页才有这两个字段，列表页没有。

所以这不是解析或存储的 bug，重抓列表页也修不好，必须让真实浏览器跑完 JS 挑战。

修复效果（实测同一条 URL）
--------------------------
==================================  ==================  ==================
指标                                 HTTP 抓取            CDP 浏览器
==================================  ==================  ==================
页面字节数                          16,010               58,523
含 ``CF_APP_WAF``                   是                    否
标题关键词命中                      0                     3
去标签纯文本                        388 字               8,898 字
==================================  ==================  ==================

用法::

    python scripts/repair_bjx_bodies.py --limit 5      # 先试 5 条
    python scripts/repair_bjx_bodies.py                # 全量（幂等，已修跳过）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)

import aiomysql  # noqa: E402
import httpx  # noqa: E402

from newsagg.adapters import ensure_loaded, get_adapter  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CDP = "http://127.0.0.1:9222"
PROFILE = PROJECT_ROOT / ".waf_chrome_profile"
#: 目标来源
ADAPTER = "bjx"
#: 正文最短有效长度。短于这个值的正文即使拿到了也当失败处理 ——
#: WAF 挑战页的纯文本有 388 字，比真实正文短但比 0 长，所以阈值不能太低。
MIN_BODY_CHARS = 80


def _dsn() -> dict:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    return dict(
        host=os.getenv("POWERNEWS_MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("POWERNEWS_MYSQL_PORT", "3306")),
        user=os.getenv("POWERNEWS_MYSQL_USER", "powernews"),
        password=os.getenv("POWERNEWS_MYSQL_PASSWORD", ""),
        db=os.getenv("POWERNEWS_MYSQL_DATABASE", "powernews"),
        charset="utf8mb4",
    )


async def fetch_targets(limit: int) -> list[tuple[int, str, str]]:
    """列出需要修复的文章 ``(id, url, title)``，只取该来源正文为空的。"""
    conn = await aiomysql.connect(**_dsn())
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT a.id, a.url, a.title
                FROM articles a JOIN sources s ON s.id = a.source_id
                WHERE s.adapter_name = %s
                  AND (a.body_text IS NULL OR TRIM(a.body_text) = '')
                ORDER BY a.id
                LIMIT %s
                """,
                (ADAPTER, limit),
            )
            return [(int(r[0]), r[1], r[2]) for r in await cur.fetchall()]
    finally:
        conn.close()


async def apply_fix(aid: int, body: str) -> None:
    """补写正文，并清掉因「取不到正文」而误打的质量标记。

    标记清理**从数据库现有值读**而不是从解析结果构造 —— ``DetailParse`` 不带
    quality_flags（那是仓储层的概念），而 ``media_only`` 是适配器对「视频/图集稿」
    的显式判定，不是缺正文的证据，不能一起撤。
    """
    conn = await aiomysql.connect(**_dsn())
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT quality_flags FROM articles WHERE id=%s", (aid,))
            row = await cur.fetchone()
            try:
                flags = json.loads(row[0]) if row and row[0] else []
                if not isinstance(flags, list):
                    flags = []
            except json.JSONDecodeError:
                flags = []
            cleaned = [
                f for f in flags
                if f not in ("empty_body", "no_source", "no_time")
            ]
            await cur.execute(
                "UPDATE articles SET body_text=%s, quality_flags=%s WHERE id=%s",
                (body, json.dumps(cleaned, ensure_ascii=False), aid),
            )
        await conn.commit()
    finally:
        conn.close()


class ChromeSession:
    """真实 Chrome 进程 + CDP 页面。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> ChromeSession:
        args = [
            CHROME,
            "--headless=new",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
            "--disable-blink-features=AutomationControlled",
            f"--user-data-dir={PROFILE}",
            "--remote-debugging-port=9222",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "about:blank",
        ]
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(40):
            try:
                httpx.get(f"{CDP}/json/version", timeout=2, trust_env=False)
                return self
            except Exception:
                time.sleep(1)
        raise RuntimeError("Chrome CDP 未在 40 秒内就绪")
    def __exit__(self, *exc: object) -> None:
        if self.proc:
            self.proc.terminate()
            time.sleep(1)
            self.proc.kill()

    def new_page(self):
        import websocket

        r = httpx.put(f"{CDP}/json/new?about:blank", timeout=10, trust_env=False)
        target = r.json()
        ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=60, suppress_origin=True,
        )
        return ws, target["id"]

    @staticmethod
    def close_page(ws, tid: str) -> None:
        try:
            ws.close()
        finally:
            httpx.get(f"{CDP}/json/close/{tid}", timeout=5, trust_env=False)

    @staticmethod
    def _send(ws, method, params=None, timeout=30) -> dict:
        import json as _json

        ws.send(_json.dumps({"id": 1, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = ws.recv()
            data = _json.loads(raw)
            if data.get("id") == 1:
                return data
        return {}

    @classmethod
    def navigate(cls, ws, url: str, *, min_len: int = 2000, timeout: int = 45) -> str:
        """导航并等待 WAF 挑战完成 + DOM 渲染，返回 outerHTML。"""
        cls._send(ws, "Page.navigate", {"url": url})
        deadline = time.time() + timeout
        best = ""
        stable = 0
        while time.time() < deadline:
            time.sleep(1.0)
            r = cls._send(ws, "Runtime.evaluate", {
                "expression": "document.documentElement.outerHTML",
                "returnByValue": True, "awaitPromise": True,
            })
            html = r.get("result", {}).get("result", {}).get("value") or ""
            if len(html) > len(best):
                best = html
            if len(html) >= min_len:
                # 连续两次长度不变即视为渲染完成
                if len(html) == stable:
                    return html
                stable = len(html)
            else:
                stable = 0
        return best


async def main() -> int:
    ap = argparse.ArgumentParser(description="修复 bjx 空正文新闻")
    ap.add_argument("--limit", type=int, default=10_000, help="最多处理几条")
    ap.add_argument("--dry-run", action="store_true", help="只查不写")
    args = ap.parse_args()

    targets = await fetch_targets(args.limit)
    if not targets:
        print("没有需要修复的空正文文章。")
        return 0
    print(f"待修复 {len(targets)} 条（来源 {ADAPTER}，正文为空）")
    if args.dry_run:
        for aid, url, _title in targets[:10]:
            print(f"  [{aid}] {url}")
        print("dry-run：未写入任何数据")
        return 0

    ensure_loaded()
    adapter = get_adapter(ADAPTER)

    ok = failed = skipped = 0
    failures: list[tuple[int, str]] = []
    started = datetime.now()

    with ChromeSession() as browser:
        ws, tid = browser.new_page()
        try:
            for i, (aid, url, _title) in enumerate(targets, 1):
                try:
                    html = ChromeSession.navigate(ws, url)
                    if not html or "CF_APP_WAF" in html:
                        raise RuntimeError("仍拿到 WAF 挑战页")
                    detail = adapter.parse_detail(html, url=url)
                    if detail.media_only:
                        # 适配器明确判定这是视频/图集稿 —— 那是内容形态，不是缺陷。
                        # 记录但不改库：不能把「没有文字正文」伪装成采集失败。
                        skipped += 1
                        print(f"  [{i}/{len(targets)}] [{aid}] 跳过：适配器判定为媒体页")
                        continue
                    body = "\n".join(
                        p.strip() for p in detail.paragraphs if p and p.strip()
                    )
                    if len(body) < MIN_BODY_CHARS:
                        raise RuntimeError(f"正文过短（{len(body)} 字）")
                    await apply_fix(aid, body)
                    ok += 1
                    print(f"  [{i}/{len(targets)}] [{aid}] 补写 {len(body)} 字 ✓")
                except Exception as exc:  # noqa: BLE001 - 单条失败不中断整轮
                    failed += 1
                    failures.append((aid, str(exc)))
                    print(f"  [{i}/{len(targets)}] [{aid}] 失败：{exc}")
        finally:
            ChromeSession.close_page(ws, tid)

    elapsed = (datetime.now() - started).total_seconds()
    print(
        f"\n完成：成功 {ok} · 失败 {failed} · 跳过 {skipped}，"
        f"耗时 {elapsed:.0f}s（{elapsed / max(1, ok + failed):.1f}s/条）"
    )
    if failures:
        print("\n失败明细：")
        for aid, why in failures[:20]:
            print(f"  [{aid}] {why}")
        if len(failures) > 20:
            print(f"  …另有 {len(failures) - 20} 条")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
