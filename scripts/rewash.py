"""重洗工具：用**当前解析规则**重新处理已落盘的原始响应。

## 它是原始层（``newsagg/rawstore.py``）的价值兑现处

没有原始层时，改进解析规则只能**重新抓取**：

- 有 WAF 的站点要再过一次 JS 挑战（`repair_bjx_bodies.py` 的处境）
- 站点已改版/下线的，历史数据**永久无法改进**
- 重抓会覆盖当时的快照，无法保证"修的是当时那份数据"

有原始层后，改规则只需**重跑解析**：

    data/raw/<站点>/<日期>/<url_hash>.html.gz
         ↓ 用当前适配器重新 parse_detail
         ↓ 对比 content_hash
         ↓ 有变化才更新库

**不联网、不重抓、可反复跑**。

## 用法

先开原始层开关采集一轮，把原文存下来：

    POWERNEWS_KEEP_RAW=true python -m newsagg crawl-once --source cpnn --days 30 --exec

之后随便改解析规则，然后：

    python scripts/rewash.py --dry-run              # 只看会改哪些
    python scripts/rewash.py --source cpnn          # 实际重洗
    python scripts/rewash.py --day 2026-10-12       # 只洗某天
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from newsagg.adapters import ensure_loaded, get_adapter  # noqa: E402
from newsagg.cleaning import (  # noqa: E402
    clean_paragraphs,
    clean_source,
    clean_title,
    content_hash,
    parse_published,
    quality_flags,
    to_utc_naive,
)
from newsagg.db import Database  # noqa: E402
from newsagg.logutil import get_logger  # noqa: E402
from newsagg.rawstore import iter_raw, raw_store_for  # noqa: E402
from newsagg.repo import Repository  # noqa: E402
from newsagg.urls import normalize_url, url_hash  # noqa: E402

log = get_logger("rewash")

DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "raw"


def _val(row, key: str = "content_hash"):
    """从 fetchone 结果里取值，兼容 DictCursor 与普通游标。

    ⚠️ 必须定义在 ``main()`` **之前**：若写在 ``__main__`` 块之后，
    模块级 `raise SystemExit(main())` 执行时它还没被定义（NameError）。
    """
    if row is None:
        return None
    return row[key] if isinstance(row, dict) else row[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="用当前解析规则重洗已落盘的原始响应")
    ap.add_argument("--source", help="只重洗该站点（adapter_name）")
    ap.add_argument("--day", help="只重洗该日期（YYYY-MM-DD）")
    ap.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="原始响应根目录")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=不限）")
    ap.add_argument("--dry-run", action="store_true", help="只比对，不写库")
    args = ap.parse_args()

    ensure_loaded()
    records = list(iter_raw(args.raw_dir, source_id=args.source, day=args.day))
    if args.limit:
        records = records[: args.limit]
    if not records:
        print(f"没有可重洗的原始响应（目录 {args.raw_dir}）")
        print("提示：先用 POWERNEWS_KEEP_RAW=true 跑一轮采集，把原文存下来。")
        return 1

    print(f"待重洗 {len(records)} 条原始响应\n")

    db = Database()
    repo = Repository()
    stat = {"checked": 0, "changed": 0, "same": 0, "empty": 0, "error": 0}
    samples: list[str] = []

    # 按站点分组：适配器要按站点取，避免每条约一次 get_adapter
    from collections import defaultdict

    by_source: dict[str, list] = defaultdict(list)
    for rec in records:
        by_source[rec.source_id].append(rec)

    for source_id, recs in sorted(by_source.items()):
        try:
            adapter = get_adapter(source_id)
        except Exception as exc:  # noqa: BLE001 - 适配器缺失时跳过该站点
            print(f"[跳过] {source_id}: 没有对应适配器（{exc}）")
            continue

        # 拿 source_id 主键。
        # ⚠️ ``Database.connect()`` 返回的是 **DictCursor** 连接，
        # ``fetchone()`` 给的是 dict —— 用 ``row[0]`` 会 KeyError: 0。
        # 这里同时兼容两种游标，避免换连接方式时再次踩坑。
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM sources WHERE adapter_name=%s", (source_id,))
                row = cur.fetchone()
            if not row:
                print(f"[跳过] {source_id}: sources 表里没有这个来源")
                continue
            db_source_id = int(row["id"] if isinstance(row, dict) else row[0])
        finally:
            conn.close()

        store = raw_store_for(source_id, base_dir=args.raw_dir)
        n_changed = 0
        for rec in recs:
            stat["checked"] += 1
            try:
                html = store.load(rec.path).decode("utf-8", errors="replace")
            except OSError as exc:
                stat["error"] += 1
                log.warning("读取失败 %s：%s", rec.path.name, exc)
                continue

            url = rec.url or ""
            if not url:
                stat["error"] += 1
                continue

            # 用**当前**适配器重新解析
            detail = adapter.parse_detail(html, url=url)
            paragraphs = clean_paragraphs(detail.paragraphs)
            if not paragraphs:
                stat["empty"] += 1
                if len(samples) < 5:
                    samples.append(f"{source_id}/{rec.url_hash} 重洗后正文仍为空")
                continue

            new_hash = content_hash(paragraphs)
            nh = url_hash(url)

            # 与库里现有的 content_hash 比对：只有真变了才写
            conn = db.connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT content_hash FROM articles "
                        "WHERE source_id=%s AND url_hash=%s",
                        (db_source_id, nh),
                    )
                    existing = cur.fetchone()
                if existing and _val(existing) == new_hash:
                    stat["same"] += 1
                    continue

                if args.dry_run:
                    stat["changed"] += 1
                    if len(samples) < 5:
                        samples.append(
                            f"{source_id}/{rec.url_hash}: "
                            f"{'正文变化' if existing else '新记录'} "
                            f"({len(''.join(paragraphs))} 字)"
                        )
                    continue

                parsed = parse_published(
                    detail.published_text, default_year=datetime.now().year
                )
                cst = parsed.value
                if cst is not None and (cst.year < 2000 or cst > datetime.now()):
                    cst = None

                repo.save_article(
                    conn,
                    source_id=db_source_id,
                    url_hash=nh,
                    url=normalize_url(url),
                    title=clean_title(detail.title),
                    body_text="\n".join(paragraphs),
                    source_name=clean_source(detail.source_name),
                    published_cst=cst,
                    published_utc=to_utc_naive(cst) if cst else None,
                    time_precision=(parsed.precision if cst else "unknown"),
                    content_hash=new_hash,
                    quality_flags=quality_flags(paragraphs, is_media_only=detail.media_only),
                )
                conn.commit()
                stat["changed"] += 1
                n_changed += 1
            finally:
                conn.close()

        print(f"  {source_id}: 处理 {len(recs)} 条，更新 {n_changed} 条")

    print("\n汇总：")
    print(f"  检查 {stat['checked']} · 更新 {stat['changed']} · "
          f"未变 {stat['same']} · 正文空 {stat['empty']} · 失败 {stat['error']}")
    if samples:
        print("\n样例：")
        for s in samples:
            print(f"  - {s}")
    if args.dry_run:
        print("\ndry-run：未写库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

