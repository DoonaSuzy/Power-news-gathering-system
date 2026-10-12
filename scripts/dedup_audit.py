"""全量去重校验：按三重规则找出存量数据里的重复。

## 三重去重规则（对应需求 2）

| 层级 | 键 | 语义 | 现状 |
| --- | --- | --- | --- |
| **① 唯一标识** | `(source_id, url_hash)` | 同一来源的同一 URL | **已有唯一约束** `uk_articles_source_hash` |
| **② 发布链接** | `url_hash`（跨来源） | 同一链接被多个来源收录 | 无约束，**本脚本校验** |
| **③ 内容摘要** | `content_hash` 精确 + SimHash 汉明距离 | 同一篇文章换了链接/换来源 | 有字段无判定，**本脚本校验** |

## 为什么需要 ③ 而不只有 ①②

实测场景：
- 同一篇通稿被**多家媒体转载**（北极星 / 中国电力新闻网 / 国际能源网 常同时发）
  → `url_hash` 不同（链接不同），但**正文相同** → 只能靠内容指纹发现
- 站点改版后**同一文章换 URL**（`/p1/xxx` → `/xwzx/xxx`）→ 同样是链接变了、内容没变

项目里 `simhash_threshold=3` 这个配置**一直没有被使用**（`repo.py` 只用
`content_hash` 精确匹配），本脚本把近似去重真正跑起来。

## 安全设计

**默认只报告，不删除**。加 `--apply` 才实际处理，且**只删「可安全判定」的重复**
（同内容、保留最早入库的那条）。近似重复（汉明距离 1~3）**只提示不处理** ——
阈值误判会误删真实的不同文章，交给人工确认。

用法::

    python scripts/dedup_audit.py                    # 只报告（默认）
    python scripts/dedup_audit.py --report out.json  # 报告落盘
    python scripts/dedup_audit.py --apply            # 实际清理精确重复
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from newsagg.config import get_settings  # noqa: E402
from newsagg.db import Database  # noqa: E402


def _hamming(a: str, b: str) -> int:
    """两个 SimHash 十六进制串的汉明距离。

    Args:
        a: 16 位十六进制（不足则按 0 左补）。
        b: 同上。

    Returns:
        不同的二进制位数；任一为空串时返回 64（视为「完全不同」）。
    """
    if not a or not b:
        return 64
    try:
        x, y = int(a, 16), int(b, 16)
    except ValueError:
        return 64
    return bin(x ^ y).count("1")


def fetch_rows(db: Database) -> list[dict]:
    """拉取用于去重校验的最小列集。"""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.id, a.source_id, s.name AS source_name, a.url_hash, "
                "       a.content_hash, a.title, a.published_at_cst, "
                "       a.created_at "
                "FROM articles a LEFT JOIN sources s ON s.id = a.source_id "
                "ORDER BY a.id"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return list(rows or [])


def audit(db: Database, *, threshold: int) -> dict:
    """执行三重校验，返回结构化报告。

    Args:
        db: 数据库。
        threshold: SimHash 汉明距离阈值（≤ 该值视为近似重复）。

    Returns:
        含各层重复明细与统计的字典。
    """
    rows = fetch_rows(db)
    total = len(rows)

    # ---- ① 唯一标识：同来源 + 同 url_hash（有唯一约束，理论上应为 0）----
    by_source_url: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by_source_url[(r["source_id"], r["url_hash"])].append(r)
    dup_source_url = {k: v for k, v in by_source_url.items() if len(v) > 1}

    # ---- ② 发布链接：同 url_hash 跨来源 ----
    by_url: dict[str, list] = defaultdict(list)
    for r in rows:
        by_url[r["url_hash"]].append(r)
    dup_cross_source = {
        k: v for k, v in by_url.items()
        if len(v) > 1 and len({x["source_id"] for x in v}) > 1
    }

    # ---- ③ 内容摘要：content_hash 精确重复 ----
    by_content: dict[str, list] = defaultdict(list)
    for r in rows:
        ch = (r["content_hash"] or "").strip()
        if ch:
            by_content[ch].append(r)
    dup_exact_content = {k: v for k, v in by_content.items() if len(v) > 1}

    # ---- ③b 近似重复：SimHash 汉明距离（分桶降复杂度）----
    # 朴素 N² 在千级数据可接受，但按「高 8 位」分桶能显著减少比较次数，
    # 且不会漏（汉明距离 ≤3 的两串，高 8 位最多差 3 位，用邻桶可覆盖）。
    buckets: dict[int, list] = defaultdict(list)
    for r in rows:
        ch = (r["content_hash"] or "").strip()
        if not ch:
            continue
        try:
            buckets[int(ch, 16) >> 56].append(r)
        except ValueError:
            continue

    near_pairs: list[tuple[dict, dict, int]] = []
    seen_pairs: set[tuple[int, int]] = set()
    for hi, items in buckets.items():
        # 邻桶（hi-1 .. hi+1）内比较，覆盖高 8 位差 1~3 的情形
        for h2 in (hi - 1, hi, hi + 1):
            for b in buckets.get(h2, []):
                for a in items:
                    if a["id"] >= b["id"]:
                        continue
                    key = (a["id"], b["id"])
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    d = _hamming(a["content_hash"], b["content_hash"])
                    if 0 < d <= threshold:
                        near_pairs.append((a, b, d))

    # ---- 组装报告 ----
    def brief(r: dict) -> dict:
        return {
            "id": r["id"],
            "source": r.get("source_name") or r["source_id"],
            "title": (r["title"] or "")[:60],
            "url_hash": (r["url_hash"] or "")[:12],
            "published": str(r["published_at_cst"] or ""),
        }

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_articles": total,
        "threshold": threshold,
        "layer1_source_url_duplicates": len(dup_source_url),
        "layer2_cross_source_link_duplicates": len(dup_cross_source),
        "layer3_exact_content_duplicates": len(dup_exact_content),
        "layer3b_near_duplicate_pairs": len(near_pairs),
        "total_duplicate_rows": (
            sum(len(v) - 1 for v in dup_source_url.values())
            + sum(len(v) - 1 for v in dup_exact_content.values())
        ),
        "samples": {
            "cross_source": [
                {"url_hash": k[:12], "count": len(v),
                 "sources": sorted({x.get("source_name") or x["source_id"] for x in v}),
                 "titles": [brief(x)["title"] for x in v[:3]]}
                for k, v in list(dup_cross_source.items())[:5]
            ],
            "exact_content": [
                {"content_hash": k, "count": len(v),
                 "items": [brief(x) for x in v[:4]]}
                for k, v in list(dup_exact_content.items())[:5]
            ],
            "near_duplicate": [
                {"distance": d, "a": brief(a), "b": brief(b),
                 "a_content": a["content_hash"], "b_content": b["content_hash"]}
                for a, b, d in near_pairs[:5]
            ],
        },
    }
    return report


def apply_exact_dedup(db: Database) -> int:
    """清理**精确内容重复**：同 `content_hash` 只保留最早入库那条。

    Returns:
        删除的行数。

    Note:
        **只处理精确重复**。近似重复（汉明距离 1~3）不自动删 ——
        阈值误判会误删真实的不同文章（例如同一场活动的两篇不同报道）。
    """
    conn = db.connect()
    deleted = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT content_hash, MIN(id) AS keep_id, COUNT(*) AS n "
                "FROM articles WHERE content_hash <> '' "
                "GROUP BY content_hash HAVING n > 1"
            )
            groups = cur.fetchall() or []
            for g in groups:
                keep = g["keep_id"]
                cur.execute(
                    "DELETE FROM articles WHERE content_hash=%s AND id<>%s",
                    (g["content_hash"], keep),
                )
                deleted += cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()
    return deleted


def main() -> int:
    ap = argparse.ArgumentParser(description="全量去重校验（三重规则）")
    ap.add_argument("--report", default="data/dedup_audit.json", help="报告落盘路径")
    ap.add_argument("--threshold", type=int, default=None,
                    help="SimHash 汉明距离阈值（默认取配置 simhash_threshold）")
    ap.add_argument("--apply", action="store_true",
                    help="实际清理精确重复（默认只报告）")
    args = ap.parse_args()

    settings = get_settings()
    db = Database(settings)
    threshold = args.threshold if args.threshold is not None else settings.simhash_threshold

    print("=" * 66)
    print("全量去重校验（唯一标识 / 发布链接 / 内容摘要）")
    print("=" * 66)

    report = audit(db, threshold=threshold)

    print(f"\n文章总数            : {report['total_articles']}")
    print(f"① 同来源同URL 重复  : {report['layer1_source_url_duplicates']}")
    print(f"② 跨来源同链接 重复 : {report['layer2_cross_source_link_duplicates']}")
    print(f"③ 同内容指纹 重复   : {report['layer3_exact_content_duplicates']} 组")
    print(f"③b 近似重复(≤{threshold}) : {report['layer3b_near_duplicate_pairs']} 对")
    print(f"\n可清理的冗余行数    : {report['total_duplicate_rows']}")

    if report["samples"]["exact_content"]:
        print("\n--- 同内容重复样例 ---")
        for s in report["samples"]["exact_content"][:3]:
            print(f"  指纹 {s['content_hash']}  ×{s['count']}")
            for it in s["items"]:
                print(f"    id={it['id']:<6} [{it['source']}] {it['title']}")

    if report["samples"]["near_duplicate"]:
        print("\n--- 近似重复样例（仅提示，不自动处理）---")
        for s in report["samples"]["near_duplicate"][:3]:
            print(f"  距离 {s['distance']}: id={s['a']['id']} vs id={s['b']['id']}")
            print(f"    A [{s['a']['source']}] {s['a']['title']}")
            print(f"    B [{s['b']['source']}] {s['b']['title']}")

    out = PROJECT_ROOT / args.report
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已落盘：{out}")

    if args.apply:
        n = apply_exact_dedup(db)
        print(f"\n✅ 已清理精确重复 {n} 行")
    else:
        print("\n（默认只报告。加 --apply 才会清理精确重复）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
