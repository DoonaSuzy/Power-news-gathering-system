#!/usr/bin/env python
"""时间范围筛选（今日 / 近1周 / 近1个月 / 自定义）的真实库核验。

离线单测只能证明「代码算出的窗口和界面标签一致」；这里要证明的是另一件事：
**同一口径下，接口返回的行集与数据库直查的行集完全相同**——既不混进窗口外的行，
也不漏掉窗口内的行。所以核验有三个独立来源：

1. 基准（ground truth）：``SELECT ... FROM articles JOIN sources`` 直接按窗口取 id 集合；
2. HTTP 接口：``GET /api/v1/articles?range=...`` 翻页取回全部 id 集合（走的是
   deps → service → repository 这条真实链路，包括鉴权与分页）；
3. CLI：``newsagg.query.query_articles``（采集层原生 SQL，``powernews query/export`` 用的就是它）。

三者必须两两相等。时间口径一律是**北京时间**：MySQL 服务器时区是 ``+00:00``，
所以基准 SQL 里绝不用 ``CURDATE()``/``NOW()``，窗口全部由
``newsagg.timefilter.resolve_range`` 算好后当参数传进去。

跑法（需要一套真实服务 + 真实库）::

    python -m newsagg serve --port 8123          # 另一个终端
    python scripts/news_time_filter_audit.py     # 令牌默认读 .workbuddy/e2e_token

退出码：0 = 全绿；1 = 有 FAIL。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from newsagg.cleaning import now_cst
from newsagg.db import Database
from newsagg.query import ArticleFilter, query_articles
from newsagg.timefilter import RANGE_KEYS, describe_range, end_of_day, resolve_range, start_of_day

TOKEN_CACHE = Path(__file__).resolve().parents[1] / ".workbuddy" / "e2e_token"

_PASS = 0
_FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


# --------------------------------------------------------------------------
# 基准：数据库直查
# --------------------------------------------------------------------------
def truth_ids(db: Database, window: tuple[date, date] | None) -> dict[str, Any]:
    """按北京时间闭区间直查命中行。

    与接口同一条 JOIN（``articles JOIN sources``），否则「有文章但来源行缺失」
    会让两边数字天然对不上，那种差异不是筛选缺陷。
    """
    sql = (
        "SELECT a.id, a.published_at_cst, a.time_precision, s.category, s.province_code "
        "FROM articles a JOIN sources s ON s.id = a.source_id"
    )
    params: list[Any] = []
    if window is not None:
        sql += " WHERE a.published_at_cst >= %s AND a.published_at_cst <= %s"
        params = [start_of_day(window[0]), end_of_day(window[1])]
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = list(cur.fetchall())
    finally:
        conn.close()
    stamps = [r["published_at_cst"] for r in rows if r["published_at_cst"]]
    return {
        "count": len(rows),
        "ids": {int(r["id"]) for r in rows},
        "min": min(stamps) if stamps else None,
        "max": max(stamps) if stamps else None,
        "rows": rows,
    }


def null_timestamp_ids(db: Database) -> set[int]:
    """``published_at_cst IS NULL`` 的行 id。

    带时间过滤时这些行**必然**被排除（SQL 里 NULL 与任何区间比较都得 unknown）。
    只要它不为 0，「近1个月全量」这种说法就不成立，必须先报出来再说清边界。
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM articles WHERE published_at_cst IS NULL")
            return {int(r["id"]) for r in (cur.fetchall() or [])}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 被测：HTTP 接口
# --------------------------------------------------------------------------
def api_fetch_all(client: httpx.Client, api: str, params: dict[str, Any]) -> tuple[int, list[dict]]:
    """翻页取回筛选结果的全部行（page_size 用接口上限 100）。"""
    items: list[dict] = []
    page = 1
    total = 0
    while True:
        resp = client.get(f"{api}/articles", params={**params, "page": page, "page_size": 100})
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"业务码非 0: {body}")
        data = body["data"]
        total = int(data["total"])
        batch = data["items"]
        items.extend(batch)
        if not batch or len(items) >= total:
            break
        page += 1
        if page > 200:  # 防御：窗口内行数远超预期时不要无界循环
            raise RuntimeError("翻页超过 200 页，疑似分页未推进")
    return total, items


def parse_stamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def verify_null_rows(
    client: httpx.Client, api: str, null_ids: set[int]
) -> None:
    """无发布时间的行：带条件的查询必须一个都不含，全库查询必须一个都不丢。

    这类行的来源通常是站点页上就没有日期（如「北极星学社」的书籍目录页）。
    它们不是筛选缺陷而是数据事实，但**必须**说清边界，否则「近1个月全量」是假话。
    """
    print(f"\n--- 无发布时间的 {len(null_ids)} 行怎么处置 ---")
    for key in RANGE_KEYS[:3]:
        _, items = api_fetch_all(client, api, {"range": key})
        leaked = null_ids & {int(r["id"]) for r in items}
        check(
            f"range={key} 不含无时间戳行",
            not leaked,
            f"泄漏 {sorted(leaked)[:5]}" if leaked else "全部排除",
        )
    _, items = api_fetch_all(client, api, {})
    missing = null_ids - {int(r["id"]) for r in items}
    check(
        "不带时间条件时无时间戳行仍在结果里",
        not missing,
        f"缺 {sorted(missing)[:5]}" if missing else f"样例 {sorted(null_ids)[:3]} 均在",
    )


# --------------------------------------------------------------------------
# 核验主体
# --------------------------------------------------------------------------
def verify_case(
    client: httpx.Client,
    api: str,
    db: Database,
    *,
    label: str,
    params: dict[str, Any],
    window: tuple[date, date] | None,
) -> None:
    print(f"\n--- {label} ---")
    base = truth_ids(db, window)

    total, items = api_fetch_all(client, api, params)
    api_ids = {int(r["id"]) for r in items}

    check(f"{label}·接口 total 与直查一致", total == base["count"], f"{total} vs {base['count']}")
    check(f"{label}·翻页取回行数等于 total", len(items) == total, f"{len(items)} vs {total}")
    check(
        f"{label}·行集合与直查完全相同",
        api_ids == base["ids"],
        f"接口多 {sorted(api_ids - base['ids'])[:5]} / 接口少 {sorted(base['ids'] - api_ids)[:5]}"
        if api_ids != base["ids"]
        else f"{len(api_ids)} 个 id 一一对齐",
    )

    stamps = [parse_stamp(r.get("published_at_cst")) for r in items]
    if window is not None:
        lo, hi = start_of_day(window[0]), end_of_day(window[1])
        outside = [s for s in stamps if s is None or not (lo <= s <= hi)]
        check(
            f"{label}·无窗口外的行混入",
            not outside,
            f"{len(outside)} 行越界，样例 {[str(s) for s in outside[:3]]}" if outside else "",
        )
        check(f"{label}·按发布时间倒序返回", stamps == sorted(stamps, reverse=True))
        in_window_days = {s.date() for s in stamps if s}
        if in_window_days:
            print(
                f"       实际命中日界 {min(in_window_days)} ~ {max(in_window_days)}"
                f"（口径 {window[0]} ~ {window[1]}，空白日不计）"
            )
    else:
        check(
            f"{label}·不带时间条件时返回全库",
            total == base["count"],
            f"{total} vs {base['count']}",
        )

    # CLI 走的是采集层原生 SQL，必须和接口命中同一批行
    cli = query_articles(
        ArticleFilter(
            date_from=start_of_day(window[0]) if window else None,
            date_to=end_of_day(window[1]) if window else None,
            page=1,
            page_size=100,
        ),
        db,
    )
    check(
        f"{label}·CLI 与接口同口径",
        int(cli["total"]) == total,
        f"CLI {cli['total']} vs 接口 {total}",
    )


def verify_same_day_not_split(client: httpx.Client, api: str, db: Database) -> None:
    """单日自定义区间必须含当天**全部**行。

    钉住的老坑：``until=2026-10-10`` 若解析成 ``00:00:00``，当天只有零点整
    （day 精度）那批行能命中，晚间精确到秒的行会被静默丢掉。
    """
    print("\n--- 边界：单日区间（含 day 精度与 second 精度混合的那一天）---")
    base = truth_ids(db, None)
    per_day: Counter[date] = Counter()
    mixed: Counter[date] = Counter()
    for row in base["rows"]:
        stamp = row["published_at_cst"]
        if not stamp:
            continue
        per_day[stamp.date()] += 1
        if row["time_precision"] in ("second", "minute"):
            mixed[stamp.date()] += 1
    # 找「既有当天零点归档、又有精确到秒」的日子，这类日子最容易暴露截断
    candidates = [d for d in mixed if per_day[d] - mixed[d] > 0 and per_day[d] >= 3]
    if not candidates:
        check("单日边界样本存在", False, "库里没有 day/second 混合的日子，换一天再验")
        return
    day = max(candidates)
    day_rows = [
        r for r in base["rows"] if r["published_at_cst"] and r["published_at_cst"].date() == day
    ]
    total, items = api_fetch_all(
        client, api, {"since": str(day), "until": str(day)}
    )
    check(
        f"{day} 单日区间命中数 = 该日实际行数",
        total == len(day_rows),
        f"接口 {total} vs 直查 {len(day_rows)}",
    )
    latest = max(r["published_at_cst"] for r in day_rows)
    check(
        "当天最晚一条（精确到秒）未被截断丢掉",
        any(parse_stamp(r.get("published_at_cst")) == latest for r in items),
        f"当天最晚 {latest}",
    )


def verify_rejected_combinations(client: httpx.Client, api: str) -> None:
    print("\n--- 非法/歧义输入必须被拒，而不是静默按某一种解释 ---")
    resp = client.get(f"{api}/articles", params={"range": "week", "since": "2026-01-01"})
    check(
        "range 与 since 同时给 → 400",
        resp.status_code == 400,
        f"{resp.status_code} {resp.json().get('detail', '')[:60] if resp.content else ''}",
    )
    resp = client.get(f"{api}/articles", params={"range": "quarter"})
    check("未知档位 → 422（不是「不加过滤」）", resp.status_code == 422, str(resp.status_code))
    resp = client.get(f"{api}/articles/count", params={"range": "month"})
    check("/count 复用同一套档位", resp.status_code == 200, str(resp.status_code))
    resp = client.get(f"{api}/articles", params={"category": "gov"})
    check("category=gov 可用（曾漏在合法值外）", resp.status_code == 200, str(resp.status_code))


def verify_combined(client: httpx.Client, api: str, db: Database, province_code: str) -> None:
    """时间档 + 省份叠加：两类条件同时下推时也不能漏行。

    单独验时间、单独验省份都够，但「先按省过滤再按时间过滤」在仓储层是
    同一个 ``conditions()`` 列表拼出来的——只有叠加用例能发现某一边被覆盖掉。
    """
    print(f"\n--- 叠加：省份 {province_code} × range=month ---")
    date_from, date_to = resolve_range("month")
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.id FROM articles a JOIN sources s ON s.id = a.source_id "
                "WHERE s.province_code = %s AND a.published_at_cst >= %s AND a.published_at_cst <= %s",
                (province_code, date_from, date_to),
            )
            ids = {int(r["id"]) for r in cur.fetchall()}
    finally:
        conn.close()

    total, items = api_fetch_all(client, api, {"province": province_code, "range": "month"})
    check(
        f"省份 × 近1个月命中数 = 直查（{len(ids)} 行）",
        total == len(ids),
        f"接口 {total} vs 直查 {len(ids)}",
    )
    check(
        "省份 × 近1个月行集合一致",
        {int(r["id"]) for r in items} == ids,
        f"接口多 {sorted({int(r['id']) for r in items} - ids)[:5]} / "
        f"接口少 {sorted(ids - {int(r['id']) for r in items})[:5]}",
    )
    outside: list[dict] = []
    for row in items:
        stamp = parse_stamp(row.get("published_at_cst"))
        if stamp is None or not (date_from <= stamp <= date_to):
            outside.append(row)
    check("叠加条件下无窗口外的行", not outside, f"{len(outside)} 行越界")


def verify_export(client: httpx.Client, api: str, db: Database) -> None:
    """``/articles/export`` 必须与列表同口径。

    ``ArticleService.export_csv`` 是**逐字段重建** ``ArticleQuery`` 的（分页游标要
    单独控制），所以给 ``ArticleQuery`` 加字段时忘在这里传一行，就会「列表筛得对、
    导出的 CSV 却没筛」。这里用响应头 ``X-Export-Rows`` 对直查行数。
    """
    print("\n--- 导出与列表同口径（export_csv 逐字段重建 ArticleQuery）---")
    for key in ("week", "month"):
        date_from, date_to = resolve_range(key)
        base = truth_ids(db, (date_from.date(), date_to.date()))
        resp = client.get(f"{api}/articles/export", params={"range": key})
        reported = int(resp.headers.get("X-Export-Rows", "-1"))
        check(
            f"export?range={key} 行数 = 直查（{base['count']}）",
            reported == base["count"] and resp.status_code == 200,
            f"X-Export-Rows={reported} HTTP={resp.status_code}",
        )
        # 不做逐列 CSV 解析：正文里有逗号和换行，得先处理引号转义才有意义，
        # 而「窗口内无越界行」已由列表接口那一侧的行级校验覆盖。


def report_coverage(db: Database, days: int = 30) -> None:
    """近 N 天的逐日完整度（需求 2/3 的证据）。"""
    print(f"\n--- 近 {days} 天逐日覆盖（北京时间口径，空白日标 WEEKEND）---")
    today = now_cst().date()
    window = (today - timedelta(days=days - 1), today)
    base = truth_ids(db, window)
    per_day: Counter[date] = Counter()
    for row in base["rows"]:
        stamp = row["published_at_cst"]
        if stamp:
            per_day[stamp.date()] += 1
    d = window[0]
    empty = []
    while d <= window[1]:
        weekday = d.weekday() >= 5
        mark = "空" if per_day[d] == 0 else ""
        tag = " WEEKEND" if weekday else ""
        if per_day[d] == 0:
            empty.append((d, weekday))
        print(f"    {d}  {str(per_day[d]).rjust(4)}{tag} {mark}")
        d += timedelta(days=1)
    covered = sum(1 for k in per_day if per_day[k])
    print(
        f"    合计 {base['count']} 行 / 有产出天数 {covered}/{days} / "
        f"空白天 {len(empty)}（其中周末 {sum(1 for _, w in empty if w)}）"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="时间范围筛选真实库核验")
    ap.add_argument("--base-url", default="http://127.0.0.1:8123")
    ap.add_argument("--token", default="", help="admin Key；留空读 .workbuddy/e2e_token")
    args = ap.parse_args()

    token = args.token or (TOKEN_CACHE.read_text(encoding="utf-8").strip() if TOKEN_CACHE.exists() else "")
    if not token:
        print("没有可用令牌（--token 或 .workbuddy/e2e_token）")
        return 1

    api = f"{args.base_url.rstrip('/')}/api/v1"
    client = httpx.Client(timeout=60.0, headers={"Authorization": f"Bearer {token}"})
    db = Database()

    health = client.get(f"{api}/health")
    if health.status_code != 200:
        print(f"服务不可达：{args.base_url} -> HTTP {health.status_code}")
        return 1

    now = now_cst()
    print(f"北京时间 {now:%Y-%m-%d %H:%M:%S}（宿主机时区不参与计算，档位一律按北京时间算）")
    nulls = null_timestamp_ids(db)
    if nulls:
        verify_null_rows(client, api, nulls)
    else:
        check("库里没有 published_at_cst 为空的文章", True, "0 行")
    all_base = truth_ids(db, None)
    print(
        f"全库 {all_base['count']} 行，发布时间 "
        f"{all_base['min']} ~ {all_base['max']}"
    )

    for key in RANGE_KEYS[:3]:
        date_from, date_to = resolve_range(key)
        print(f"\n档位 {key} -> {describe_range(key)}")
        verify_case(
            client,
            api,
            db,
            label=f"range={key}（{describe_range(key).split('（')[0]}）",
            params={"range": key},
            window=(date_from.date(), date_to.date()),
        )

    custom_start = (now - timedelta(days=19)).date()
    verify_case(
        client,
        api,
        db,
        label=f"自定义 {custom_start} ~ {now.date()}",
        params={"since": str(custom_start), "until": str(now.date())},
        window=(custom_start, now.date()),
    )
    verify_case(
        client, api, db, label="不带时间条件（全库）", params={}, window=None
    )

    verify_same_day_not_split(client, api, db)
    verify_export(client, api, db)

    month_from, month_to = resolve_range("month")
    month_base = truth_ids(db, (month_from.date(), month_to.date()))
    codes = Counter(
        str(r["province_code"]) for r in month_base["rows"] if r.get("province_code")
    )
    if codes:
        top_code, top_n = codes.most_common(1)[0]
        print(f"\n（叠加用例选 {top_code}：近1个月内该省有 {top_n} 行，最容易暴露漏行）")
        verify_combined(client, api, db, top_code)
    else:
        check("近1个月内没有可按省份归类的行，跳过叠加用例", False, "先补采再验")

    verify_rejected_combinations(client, api)
    report_coverage(db)

    print(f"\n结果：PASS {_PASS} / FAIL {_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
