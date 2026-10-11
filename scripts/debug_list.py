"""调试：用 CDP 取列表页 HTML，dump 结构与适配器解析结果。

用法：
    python scripts/debug_list.py sgcc_bj http://www.bj.sgcc.com.cn/p1/
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_k, None)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from newsagg.adapters import ensure_loaded, get_adapter  # noqa: E402
from scripts.cdp_crawl import close_page, new_page  # noqa: E402


def main() -> None:
    sid = sys.argv[1]
    url = sys.argv[2]
    ensure_loaded()
    ad = get_adapter(sid)

    page, tid = new_page()
    try:
        html = page.navigate(url, min_len=2000, timeout=40)
        out = Path("data/_debug")
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{sid}_list.html").write_text(html, encoding="utf-8")
        print(f"HTML 长度: {len(html)}，已存 {out / f'{sid}_list.html'}")

        parsed = ad.parse_list(html, base_url=url)
        print(f"\n适配器解析条目数: {len(parsed.items)}")
        for it in parsed.items[:12]:
            print(f"  - title={it.title!r}")
            print(f"    url={it.url}")

        # 直接正则找文章链接形态
        print("\nHTML 中疑似文章链接（前 15）：")
        for m in re.findall(r'href="([^"]+\.s?html?)"[^>]*>([^<]{2,40})', html)[:15]:
            print(f"  {m[0][:80]}  |  {m[1].strip()[:30]}")
    finally:
        page.close()
        close_page(tid)


if __name__ == "__main__":
    main()
