"""电力新闻智能总结的真库审计：**直查 SQL** 与 **总结服务** 逐项对照。

为什么要单独一个脚本
--------------------
``tests/test_news_summary.py`` 全部离线（假会话），它保证的是「拼出了什么事」；
本脚本保证「在真实数据上，服务说的和库里有的是一回事」。
两者刻意分开：单测要能秒级跑完且不依赖数据库，而这里要慢、要真。

审计项
------
对 ``today`` / ``week`` / ``month`` / 自定义 四个档位各跑一遍：

1. ``stats.total_matched`` == 直查 ``COUNT(*)``（同一套条件的独立实现）；
2. ``window.start`` / ``window.end`` == ``timefilter.resolve_range`` 的结果；
3. 每条要点/亮点引用的 ``article_ids`` 在库里真实存在（防模型幻觉引用）；
4. ``headline`` 非空；有文章时 ``key_points`` 非空、``articles_used == limit`` 或窗口篇数；
5. 空窗口时必须给出 ``fallback`` 或 ``caveats``，不能静默返回空壳；
6. ``method`` 与 ``llm.ready`` 自洽（没配 LLM 就不能声称走的是 llm）。

用法::

    PY="C:/Users/10911/.workbuddy/binaries/python/envs/powernews/Scripts/python.exe"
    $PY scripts/news_summary_audit.py

退出码：0 = 全部 PASS；1 = 有 FAIL。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from newsagg.timefilter import RANGE_DAYS, resolve_range  # noqa: E402
from newsplatform.models.orm import Article, Source  # noqa: E402
from newsplatform.repositories.articles import ArticleQuery, ArticleRepository  # noqa: E402
from newsplatform.services.extractive import starts_with_reference  # noqa: E402
from newsplatform.services.summary_service import SummaryService  # noqa: E402

PASS = 0
FAIL = 0
NOTES: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}{('  — ' + detail) if detail else ''}")


def note(text: str) -> None:
    NOTES.append(text)
    print(f"        {text}")


async def _count(session, q: ArticleQuery) -> int:  # noqa: ANN001
    """独立实现一遍计数：不经仓储的 conditions，直接在 SQL 里写条件。"""
    conds = []
    if q.province_codes:
        conds.append(Source.province_code.in_(tuple(q.province_codes)))
    if q.category:
        conds.append(Source.category == q.category)
    if q.date_from:
        conds.append(Article.published_at_cst >= q.date_from)
    if q.date_to:
        conds.append(Article.published_at_cst <= q.date_to)
    stmt = (
        select(func.count())
        .select_from(Article.__table__.join(Source.__table__, Source.id == Article.source_id))
        .where(*conds)
    )
    return int((await session.execute(stmt)).scalar() or 0)


async def _exists_ids(session, ids: list[int]) -> set[int]:  # noqa: ANN001
    if not ids:
        return set()
    stmt = select(Article.id).where(Article.id.in_(tuple(ids)))
    return {int(r[0]) for r in (await session.execute(stmt)).all()}


async def audit_case(
    session,
    *,
    range_key: str | None,
    since: str | None,
    until: str | None,
    label: str,
    limit: int = 40,
) -> None:  # noqa: ANN001
    print(f"\n--- {label} ---")

    if range_key and range_key != "custom":
        window = resolve_range(range_key)
    else:
        window = None
    if window is not None:
        date_from, date_to = window
    else:
        date_from = datetime.fromisoformat(f"{since} 00:00:00") if since else None
        date_to = (
            datetime.fromisoformat(f"{until} 23:59:59") if until else None
        )

    q = ArticleQuery(date_from=date_from, date_to=date_to)

    service = SummaryService(session)
    try:
        data = await service.news_summary(
            q,
            window_label=label,
            range_key=(range_key or "custom"),
            limit=limit,
            method="auto",
        )
    finally:
        await service.aclose()

    # 比较基准：窗口若被回退过，直查必须跟着换成**有效窗口** —— 拿回退前的
    # 原始窗口去比，会得出「服务多报了 161 篇」这种完全错误的结论。
    if data["fallback"]:
        eff = data["fallback"]["effective_window"]
        q_cmp = ArticleQuery(
            date_from=datetime.fromisoformat(eff["start"]),
            date_to=datetime.fromisoformat(eff["end"]),
        )
        note(f"窗口已回退，比较基准改用 {eff['start']} ~ {eff['end']}")
    else:
        q_cmp = q
    expected_total = await _count(session, q_cmp)
    stats_days = {"days": await ArticleRepository(session).window_day_count(q_cmp)}

    # 1) 计数一致
    check(
        data["stats"]["total_matched"] == expected_total,
        "total_matched 与直查 COUNT(*) 一致",
        f"服务={data['stats']['total_matched']} 直查={expected_total}",
    )

    # 2) 窗口边界与 timefilter 一致
    if data["fallback"]:
        eff = data["fallback"]["effective_window"]
        check(
            data["window"]["start"] == eff["start"] and data["window"]["end"] == eff["end"],
            "回退后 window 边界 == fallback.effective_window",
            f"{data['window']['start']} ~ {data['window']['end']}",
        )
    elif window is not None:
        check(
            data["window"]["start"] == f"{date_from:%Y-%m-%d %H:%M:%S}"
            and data["window"]["end"] == f"{date_to:%Y-%m-%d %H:%M:%S}",
            "window 边界与 timefilter 一致",
            f"{data['window']['start']} ~ {data['window']['end']}",
        )
    if window is not None:
        check(
            data["window"]["days"] == RANGE_DAYS[range_key],
            "window.days 与档位表一致",
            str(data["window"]["days"]),
        )

    # 3) 引用 id 真实存在
    cited = [
        aid
        for point in list(data["summary"].get("key_points") or [])
        + list(data["summary"].get("highlights") or [])
        for aid in (point.get("article_ids") or [])
    ]
    found = await _exists_ids(session, cited)
    check(
        set(cited) <= found,
        "要点/亮点引用的 article_ids 全部存在于库",
        f"引用 {len(set(cited))} 个、缺失 {sorted(set(cited) - found)[:5]}",
    )

    # 4) 有文章时的基本形状
    if expected_total > 0:
        check(bool(data["summary"]["headline"]), "headline 非空")
        check(bool(data["summary"]["key_points"]), "有文章时 key_points 非空")
        # 分层抽样下 articles_used **允许小于** limit：per_day = ceil(limit/天数)，
        # 某天不足 per_day 篇时总数就凑不满。「<=」是对的上界，「> 0」保证没抽空。
        check(
            0 < data["stats"]["articles_used"] <= min(limit, expected_total),
            "articles_used ∈ (0, min(limit, 窗口篇数)]",
            f"{data['stats']['articles_used']} vs 上界 {min(limit, expected_total)}",
        )
        # 7) 抽样代表性：窗口跨多天时，样本也必须跨多天
        #    回归：曾经按「最新 N 篇」取数，导致今日/近1周/近1个月返回同一批要点
        distinct_days = int(stats_days.get("days") or 0) if stats_days else 0
        if distinct_days > 1:
            check(
                data["stats"]["sampled_days"] > 1,
                "跨多天窗口的样本也跨多天（分层抽样生效）",
                f"窗口 {distinct_days} 天，样本只覆盖 {data['stats']['sampled_days']} 天",
            )
    else:
        # 5) 空窗口必须解释
        explained = bool(data["fallback"]) or bool(data["caveats"])
        check(explained, "空窗口给出了 fallback 或 caveats 说明")
        if data["fallback"]:
            note(f"回退：{data['fallback']['reason']}")
        for c in data["caveats"]:
            note(f"caveat：{c}")

    # 6) method 与实际能力自洽
    check(
        data["method"] in ("llm", "extractive"),
        "method 取值合法",
        str(data["method"]),
    )
    if data["method"] == "llm":
        check(bool(data["llm"]["ready"]), "声称走 llm 时 llm.ready 必须为真")

    # 8) 产出要点必须能独立阅读
    #    回归：实测 `?range=week` 的 9 条产出里有 4 条以「该项目 / 该公司 / 其中」
    #    开头，脱离上文不知指谁。抽取式不改写，所以只能把上文接回来。
    all_points = (
        list(data["summary"].get("key_points") or [])
        + list(data["summary"].get("highlights") or [])
    )
    dangling = [p["text"][:34] for p in all_points if starts_with_reference(p["text"])]
    check(
        not dangling,
        "要点/亮点不以指代或连接词开头（补上文生效）",
        f"仍有 {len(dangling)} 条：{dangling[:3]}",
    )

    note(
        f"规模：窗口 {expected_total} 篇 / 参与摘要 {data['stats']['articles_used']} 篇"
        f" / 要点 {len(data['summary']['key_points'])} 条"
        f" / 亮点 {len(data['summary']['highlights'])} 条"
        f" / 关键词 {len(data['summary']['keywords'])} 个"
    )
    if expected_total > 0:
        note(f"headline：{data['summary']['headline']}")
        for i, p in enumerate(data["summary"]["key_points"][:3], 1):
            note(f"要点{i}：{p['text'][:80]}")


async def main() -> int:
    print("=" * 78)
    print("电力新闻智能总结 · 真库审计")
    print("=" * 78)

    async for session in _session_factory():
        await audit_case(session, range_key="today", since=None, until=None, label="今日")
        await audit_case(session, range_key="week", since=None, until=None, label="近1周")
        await audit_case(session, range_key="month", since=None, until=None, label="近1个月")
        await audit_case(
            session, range_key="custom", since="2026-09-23", until="2026-10-12",
            label="自定义 20 天",
        )
        # 一个必然为空的远古窗口：检验「不回退 + 给线索」这条路径
        await audit_case(
            session, range_key="custom", since="2020-01-01", until="2020-01-31",
            label="自定义空窗口（2020-01）",
        )

    print("\n" + "=" * 78)
    print(f"合计：PASS {PASS} / FAIL {FAIL}")
    print("=" * 78)
    return 0 if FAIL == 0 else 1


async def _session_factory():  # noqa: ANN202
    """产出采集库只读会话（只产出一次，供 main 的单层循环使用）。"""
    from newsplatform.core.database import get_collector_session

    async for session in get_collector_session():
        yield session


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
