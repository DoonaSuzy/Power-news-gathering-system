"""把浏览器采集（``data/collected/*.json``）导入 ``articles`` 表。

为什么需要它
------------
浏览器内 ``fetch`` 能拿到**渲染后的真实 DOM** —— 这是 Scrapy 静态抓取拿不到的
（实测同一站点：Scrapy ``discovered=51 / saved=0``，浏览器抓到 19 篇且全部有正文）。

但浏览器采集的数据停在页面内存里，取出来要经过对话通道（MCP 架构限制），
不适合大批量。所以分工是：

- **浏览器 MCP**：验证选择器、抓 JS 渲染 / 反爬页面、小批量补采；
- **本脚本**：把浏览器产出的 JSON 规范化后入库，与 Scrapy 采集走**同一张表、
  同一套去重**，所以两条通道的数据能合在一起用。

数据格式（``data/collected/<adapter_name>.json``）::

    [{"url": "...", "title": "...", "published": "2026-10-10",
      "source": "新华社", "body": "段落\\n段落"}, ...]

用法::

    python scripts/import_collected.py --dry-run     # 只看会导入什么
    python scripts/import_collected.py               # 实际入库
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import re
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from newsagg.cleaning import content_hash, parse_published  # noqa: E402
from newsagg.db import Database  # noqa: E402
from newsagg.repo import Repository  # noqa: E402
from newsagg.sources_seed import ALL_SOURCES  # noqa: E402
from newsagg.urls import url_hash  # noqa: E402

#: 正文过短视为抓取失败（导航/广告片段），不入库
MIN_BODY_CHARS = 80

#: 分片文件名后缀（``chinapower-part1`` / ``china5e-p2`` → 主体名）
_PART_SUFFIX = re.compile(r"[-_](?:part|p)\d+$")


def _load_items(path: pathlib.Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path.name} 顶层应为数组")
    return [d for d in data if isinstance(d, dict) and d.get("url")]


def _resolve_source(name: str | None) -> tuple[int, str] | None:
    """把文件名/host 解析成 (source_id, adapter_name)。

    优先按文件名匹配 adapter_name（采集时就以它命名）；失败则按 URL host
    在来源清单的 base_url 里找。
    """
    if not name:
        return None
    # 浏览器分片导出会带 ``-part1`` / ``-p2`` 之类的后缀，剥掉再匹配
    key = _PART_SUFFIX.sub("", name.strip().lower())
    from urllib.parse import urlparse

    if key.startswith("http"):
        host = urlparse(key).netloc.lower()
        for s in ALL_SOURCES:
            base_host = urlparse(s["base_url"]).netloc.lower()
            if host == base_host or host.endswith("." + base_host):
                return _source_row(s["id"])
        return None
    return _source_row(key)


def _source_row(adapter_name: str) -> tuple[int, str] | None:
    """查 sources 表拿自增 id。

    ⚠️ ``Database.connect()`` 返回的是**裸 pymysql 连接**（不是上下文管理器 ——
    ``with conn`` 在 pymysql 里交给的是 cursor，不是 conn），所以这里显式关闭。
    """
    conn = _DB.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, adapter_name FROM sources WHERE adapter_name=%s",
                (adapter_name,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return int(row["id"]), str(row["adapter_name"])
    finally:
        conn.close()


_DB = Database()
_REPO = Repository()


def main() -> int:
    ap = argparse.ArgumentParser(description="导入浏览器采集结果")
    ap.add_argument("--dir", default="data/collected", help="采集结果目录")
    ap.add_argument("--source", help="只处理指定 adapter_name")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    args = ap.parse_args()

    root = pathlib.Path(args.dir)
    if not root.is_absolute():
        root = PROJECT_ROOT / root

    # 浏览器导出的是 **base64**（不是裸 JSON）—— 因为数据要经过对话通道回传，
    # JSON 里的引号/换行在通道里容易被转义破坏，base64 没有这个问题。
    # 这里统一解码成 .json 再走后面的流程。
    for b64 in sorted(root.glob("*.b64")):
        try:
            payload = json.loads(base64.b64decode(b64.read_text(encoding="utf-8")))
        except (ValueError, UnicodeDecodeError) as exc:
            print(f"[跳过] {b64.name} 解码失败：{exc}")
            continue
        target = b64.with_suffix(".json")
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[解码] {b64.name} → {target.name}（{len(payload)} 条）")

    files = sorted(root.glob("*.json"))
    if args.source:
        files = [f for f in files if f.stem == args.source]
    if not files:
        print(f"没有可导入的 JSON（目录 {root}）")
        return 1

    total_seen = total_ok = total_skip = 0
    for path in files:
        resolved = _resolve_source(path.stem)
        if not resolved:
            print(f"[跳过] {path.name}：无法匹配来源（文件名应为 adapter_name）")
            continue
        source_id, adapter = resolved

        items = _load_items(path)
        ok = skipped = 0
        conn = _DB.connect()
        try:
            for it in items:
                total_seen += 1
                body = (it.get("body") or "").strip()
                title = (it.get("title") or "").strip()
                url = it["url"].strip()
                if len(body) < MIN_BODY_CHARS or not title:
                    skipped += 1
                    continue
                if args.dry_run:
                    ok += 1
                    continue

                paragraphs = [p.strip() for p in body.split("\n") if p.strip()]
                parsed = parse_published(str(it.get("published") or ""))
                _REPO.save_article(
                    conn,
                    source_id=source_id,
                    url_hash=url_hash(url),
                    url=url,
                    title=title,
                    body_text="\n".join(paragraphs),
                    source_name=str(it.get("source") or "").strip(),
                    published_utc=parsed.value,
                    published_cst=parsed.value,
                    time_precision=parsed.precision,
                    content_hash=content_hash(paragraphs),
                    quality_flags=[] if parsed.value else ["no_time"],
                )
                ok += 1
            if not args.dry_run:
                conn.commit()
        finally:
            conn.close()

        total_ok += ok
        total_skip += skipped
        print(f"[{adapter}] 读取 {len(items)} 条，入库 {ok} 条，跳过 {skipped} 条"
              f"{'（dry-run）' if args.dry_run else ''}")

    print(f"\n合计：读取 {total_seen}，入库 {total_ok}，跳过 {total_skip}")
    if args.dry_run:
        print("dry-run：未写库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
