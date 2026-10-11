"""数据中台端到端冒烟测试（36 个端点全覆盖）。

为什么单独写这个脚本，而不是只用 pytest？
------------------------------------------
1. 它对着**真实运行的服务 + 真实两个库**跑，验证的是「上线形态」而不是
   fake session——`pool_pre_ping` 那类只在真实连接池里才暴露的坑，
   mock 永远测不出来（本项目已因此踩过一次）。
2. 它是**只读优先**的：默认不写任何业务数据；写接口部分创建的资源会在
   同一轮里删干净，跑完不留痕。
3. 输出是给人看的清单（PASS/FAIL + 关键数字），可直接贴进验收记录。

用法::

    # 1) 先起服务
    python -m uvicorn newsplatform.main:app --port 8123

    # 2) 再跑冒烟
    python scripts/e2e_smoke.py --base-url http://127.0.0.1:8123

    # 写接口专项（会创建并清理一个测试来源）
    python scripts/e2e_smoke.py --write

    # 已有一把 admin Key 时直接复用，不再走引导令牌
    python scripts/e2e_smoke.py --token pn_xxxxxxxx

退出码：0 = 全绿；1 = 有 FAIL。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

#: 令牌缓存路径（``.workbuddy/`` 已在 .gitignore 中，不会误提交）
TOKEN_CACHE = Path(__file__).resolve().parents[1] / ".workbuddy" / "e2e_token"

# ----------------------------------------------------------------------
# 输出小工具
# ----------------------------------------------------------------------
_PASS = 0
_FAIL = 0
_DETAIL: list[tuple[str, str]] = []


def _emit(ok: bool, name: str, extra: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))
        _DETAIL.append((name, extra))


def _check(name: str, cond: bool, extra: str = "") -> bool:
    _emit(bool(cond), name, extra)
    return bool(cond)


def _body(resp: httpx.Response) -> dict[str, Any]:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


def _data(resp: httpx.Response) -> Any:
    return _body(resp).get("data")


def _section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="数据中台端到端冒烟测试")
    ap.add_argument("--base-url", default="http://127.0.0.1:8123")
    ap.add_argument("--token", default="",
                    help="已有 admin Key；留空则先读本地缓存，再退到引导令牌")
    ap.add_argument("--bootstrap-token", default="bootstrap-dev-token")
    ap.add_argument("--write", action="store_true", help="额外验证写接口（创建后自动清理）")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    api = f"{base}/api/v1"
    client = httpx.Client(timeout=30.0)

    # 令牌解析顺序：命令行 > 本地缓存 > 引导令牌
    # 引导令牌在库内已有 Key 后会自动失效（这是设计如此），所以把首次创建的
    # Key 落盘缓存，冒烟脚本才能反复跑而不用手工传参。
    token = args.token
    if not token and TOKEN_CACHE.exists():
        token = TOKEN_CACHE.read_text(encoding="utf-8").strip()
        if token:
            print(f"  (使用缓存令牌 {TOKEN_CACHE.relative_to(TOKEN_CACHE.parents[1])})")

    # ============ 0. 指路牌 & 文档 ============
    _section("0. 服务可达性")
    r = client.get(f"{base}/")
    _check("GET / 指路牌", r.status_code == 200, f"HTTP {r.status_code}")
    r = client.get(f"{api}/openapi.json")
    if _check("GET /openapi.json 文档可用", r.status_code == 200, f"HTTP {r.status_code}"):
        paths = _body(r).get("paths", {})
        n_ep = sum(len([m for m in v if m in {"get", "post", "patch", "put", "delete"}])
                   for v in paths.values())
        _check("OpenAPI 端点数 >= 30", n_ep >= 30, f"实际 {n_ep} 个端点 / {len(paths)} 条路径")

    # ============ 1. 健康检查（免鉴权）============
    _section("1. 健康检查（免鉴权）")
    r = client.get(f"{api}/health")
    d = _data(r) or {}
    _check("GET /health -> HTTP 200", r.status_code == 200, f"HTTP {r.status_code}")
    _check("GET /health -> status=ok", d.get("status") == "ok", f"status={d.get('status')}")
    deps = {x["name"]: x["ok"] for x in d.get("dependencies", [])}
    _check("依赖全绿", all(deps.values()), json.dumps(deps, ensure_ascii=False))
    _check("latest_article_at 非空", bool(d.get("latest_article_at")),
           str(d.get("latest_article_at")))

    # ============ 2. 鉴权 ============
    _section("2. 鉴权与错误码")
    r = client.get(f"{api}/articles")
    _check("无 Token -> 401", r.status_code == 401, f"HTTP {r.status_code}")
    _check("无 Token -> 业务码 2001", _body(r).get("code") == 2001,
           f"code={_body(r).get('code')}")
    r = client.get(f"{api}/articles", headers={"Authorization": "Bearer pn_not_a_real_key"})
    _check("错误 Token -> 401", r.status_code == 401, f"HTTP {r.status_code}")

    # ---- 拿 admin Token ----
    # 先验证手上的 token（可能来自缓存）是否还能用
    if token:
        probe = client.get(f"{api}/articles", headers={"Authorization": f"Bearer {token}"},
                           params={"page_size": 1})
        if probe.status_code != 200:
            print(f"  (令牌不可用 HTTP {probe.status_code}，改用引导令牌重新申请)")
            token = ""

    if not token:
        r = client.post(
            f"{api}/admin/api-keys",
            headers={"Authorization": f"Bearer {args.bootstrap_token}"},
            json={"name": "e2e-smoke-admin", "scopes": "admin"},
        )
        if r.status_code in (200, 201):
            created = _data(r) or {}
            token = created.get("key", "")
            _check("引导令牌创建 admin Key", bool(token),
                   f"id={created.get('id')} scopes={created.get('scopes')}")
            if token:
                TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
                TOKEN_CACHE.write_text(token, encoding="utf-8")
                print(f"  (已缓存令牌到 {TOKEN_CACHE.name}，后续可免参数重跑)")
        else:
            _emit(False, "引导令牌创建 admin Key",
                  f"HTTP {r.status_code} {_body(r).get('detail') or _body(r).get('message')}")
            print("   引导令牌仅在「库内没有任何 API Key」时可用。")
            print("   恢复方式：DELETE FROM powernews_platform.api_keys; 然后重跑。")

    if not token:
        print("\n!! 拿不到有效 Token，后续需鉴权的用例全部跳过。")
        print("   提示：若库内已有 Key，请用 --token 传入；或清空 api_keys 表后重跑。")
        return 1

    H = {"Authorization": f"Bearer {token}"}

    # ============ 3. 文章接口 ============
    _section("3. 文章接口 /articles")
    r = client.get(f"{api}/articles", headers=H, params={"page": 1, "page_size": 5})
    d = _data(r) or {}
    total = d.get("total")
    items = d.get("items") or []
    _check("GET /articles 分页列表", r.status_code == 200 and len(items) == 5,
           f"HTTP {r.status_code} 返回 {len(items)} 条")
    _check("文章总数 > 0", (total or 0) > 0, f"total={total}")
    if items:
        a0 = items[0]
        _check("列表字段完整", all(k in a0 for k in ("id", "title", "url")),
               f"样例 id={a0.get('id')} 标题={str(a0.get('title'))[:24]}")

    r = client.get(f"{api}/articles/count", headers=H)
    cnt_data = _data(r) or {}
    cnt = cnt_data.get("total") if isinstance(cnt_data, dict) else cnt_data
    _check("GET /articles/count", r.status_code == 200 and isinstance(cnt, int),
           f"total={cnt}")
    if isinstance(cnt, int) and isinstance(total, int):
        _check("count 与 list.total 一致", cnt == total, f"{cnt} vs {total}")

    if items:
        aid = items[0]["id"]
        r = client.get(f"{api}/articles/{aid}", headers=H)
        det = _data(r) or {}
        body = det.get("body_text") or ""
        _check(f"GET /articles/{{id}} 详情 id={aid}",
               r.status_code == 200 and det.get("id") == aid,
               f"HTTP {r.status_code} 正文 {len(body)} 字符")
        r = client.get(f"{api}/articles/999999999", headers=H)
        _check("不存在 id -> 404", r.status_code == 404, f"HTTP {r.status_code}")

    r = client.get(f"{api}/articles/export", headers=H, params={"limit": 50})
    ct = r.headers.get("content-type", "")
    _check("GET /articles/export 返回 CSV", r.status_code == 200 and "csv" in ct,
           f"HTTP {r.status_code} rows={r.headers.get('X-Export-Rows')} "
           f"truncated={r.headers.get('X-Export-Truncated')} bytes={len(r.content)}")

    # 查询过滤
    r = client.get(f"{api}/articles", headers=H, params={"q": "电网", "page_size": 3})
    t_q = (_data(r) or {}).get("total")
    _check("关键词过滤 q=电网", r.status_code == 200 and 0 < (t_q or 0) < cnt,
           f"命中 {t_q} 条 / 总数 {cnt}")
    r = client.get(f"{api}/articles", headers=H, params={"page_size": 500})
    _check("page_size 超上限 -> 422", r.status_code == 422, f"HTTP {r.status_code}")

    # ============ 4. 省份接口 ============
    _section("4. 省份接口 /provinces")
    r = client.get(f"{api}/provinces", headers=H)
    prov_data = _data(r) or {}
    provs = prov_data.get("items") if isinstance(prov_data, dict) else prov_data
    provs = provs or []
    _check("GET /provinces", r.status_code == 200 and len(provs) > 0, f"共 {len(provs)} 个省份")
    covered = [p for p in provs if (p.get("article_count") or 0) > 0]

    r = client.get(f"{api}/provinces/coverage", headers=H)
    cov = _data(r) or {}
    cov_items = cov.get("items") if isinstance(cov, dict) else cov
    _check("GET /provinces/coverage", r.status_code == 200 and cov_items is not None,
           f"HTTP {r.status_code} total={cov.get('total')} covered={cov.get('covered')} "
           f"missing={cov.get('missing')}")
    if cov_items:
        covered = [p for p in cov_items if (p.get("article_count") or 0) > 0]
    _check("有文章覆盖的省份 > 0", len(covered) > 0, f"{len(covered)} 个省份有数据")

    if covered:
        code = covered[0].get("code") or covered[0].get("province_code")
        r = client.get(f"{api}/provinces/{code}", headers=H)
        _check(f"GET /provinces/{{code}} code={code}", r.status_code == 200,
               f"HTTP {r.status_code}")
        r = client.get(f"{api}/provinces/{code}/articles", headers=H,
                       params={"page_size": 3})
        pgs = _data(r) or {}
        _check(f"GET /provinces/{{code}}/articles code={code}", r.status_code == 200,
               f"命中 {pgs.get('total')} 条")

    # ---------- 4b. 按名称查询（不逼用户记 6 位代码）----------
    # 起因：接口原先只收 440000 这种代码；/sources?province= 更是完全不校验，
    # 传「广东」会**静默返回 0 条**——看起来像「这个省确实没数据」，比报错难查。
    # 下面这组断言把「名称 == 代码」钉死，并把错误路径也覆盖上。
    _section("4b. 按省份名称 / 大区 / 全国 查询")
    if covered:
        first = covered[0]
        code = str(first.get("code") or first.get("province_code") or "")
        name = str(first.get("short_name") or first.get("name") or "")
        if code and name:
            r = client.get(f"{api}/provinces/{quote(name)}", headers=H)
            d = _data(r) or {}
            _check(f"GET /provinces/{name}（名称）-> 200", r.status_code == 200,
                   f"HTTP {r.status_code} 解析为 {d.get('code')} {d.get('name')}")
            _check("名称与代码指向同一省份", d.get("code") == code,
                   f"{d.get('code')} vs {code}")

            rc = client.get(f"{api}/provinces/{code}/articles", headers=H,
                            params={"page_size": 1})
            rn = client.get(f"{api}/provinces/{quote(name)}/articles", headers=H,
                            params={"page_size": 1})
            tc = (_data(rc) or {}).get("total")
            tn = (_data(rn) or {}).get("total")
            _check(f"GET /provinces/{name}/articles 与代码等价",
                   rn.status_code == 200 and tn == tc,
                   f"名称 total={tn} / 代码 total={tc}")

            ra = client.get(f"{api}/articles", headers=H,
                            params={"province": name, "page_size": 1})
            rb = client.get(f"{api}/articles", headers=H,
                            params={"province": code, "page_size": 1})
            ta = (_data(ra) or {}).get("total")
            tb = (_data(rb) or {}).get("total")
            _check(f"GET /articles?province={name} 与代码等价",
                   ra.status_code == 200 and ta == tb, f"名称 {ta} / 代码 {tb}")

            rs = client.get(f"{api}/sources", headers=H, params={"province": name})
            sd = _data(rs) or {}
            _check(f"GET /sources?province={name} 不静默返空",
                   rs.status_code == 200 and isinstance(sd.get("total"), int),
                   f"total={sd.get('total')}")

    # 大区名落到**单值**接口 -> 400，且提示里要列出候选代码
    r = client.get(f"{api}/provinces/{quote('华南')}", headers=H)
    detail = str(_body(r).get("detail") or "")
    _check("GET /provinces/华南 -> 400（大区是一对多）", r.status_code == 400,
           f"HTTP {r.status_code}")
    _check("一对多提示列出候选代码", "440000" in detail and "450000" in detail, detail[:60])

    # 大区名落到**列表**接口 -> 200，天然是「一次查多省」
    r = client.get(f"{api}/articles", headers=H, params={"province": "华南", "page_size": 1})
    _check("GET /articles?province=华南 -> 200", r.status_code == 200,
           f"total={(_data(r) or {}).get('total')}")

    # 全国 / all -> 等价于不加过滤
    r = client.get(f"{api}/articles", headers=H, params={"province": "全国", "page_size": 1})
    _check("?province=全国 等价于不过滤",
           r.status_code == 200 and (_data(r) or {}).get("total") == cnt,
           f"全国 {(_data(r) or {}).get('total')} vs 总数 {cnt}")
    r = client.get(f"{api}/articles", headers=H, params={"province": "all", "page_size": 1})
    _check("?province=all 等价于不过滤",
           r.status_code == 200 and (_data(r) or {}).get("total") == cnt,
           f"all {(_data(r) or {}).get('total')} vs 总数 {cnt}")

    # 认不出来 -> 400 且带上可用取值（而不是静默 0 条）
    r = client.get(f"{api}/articles", headers=H, params={"province": "米国"})
    md = str(_body(r).get("detail") or "")
    _check("?province=米国 -> 400", r.status_code == 400, f"HTTP {r.status_code}")
    _check("错误消息含可用取值提示", "可用取值" in md, md[:70])

    # 关键词里的 LIKE 通配符必须被转义（否则 % 会变成「匹配任意字符」）
    r = client.get(f"{api}/articles", headers=H,
                   params={"keyword": "zzzznotexist", "page_size": 1})
    _check("keyword=不存在的词 -> 0 条（证明参数真的接上了）",
           r.status_code == 200 and (_data(r) or {}).get("total") == 0,
           f"total={(_data(r) or {}).get('total')}")

    r = client.get(f"{api}/articles", headers=H, params={"keyword": "%", "page_size": 1})
    t_pct = (_data(r) or {}).get("total")
    _check("keyword=% 被当字面量（若当通配符会返回全表）",
           r.status_code == 200 and t_pct != cnt, f"total={t_pct} / 总数 {cnt}")

    # q 与 keyword 互为别名（历史上 /articles 只认 keyword，用 q 会被静默忽略）
    rq = client.get(f"{api}/articles", headers=H, params={"q": "电网", "page_size": 1})
    rk = client.get(f"{api}/articles", headers=H, params={"keyword": "电网", "page_size": 1})
    tq = (_data(rq) or {}).get("total")
    tk = (_data(rk) or {}).get("total")
    _check("/articles 的 q 与 keyword 等价", tq == tk and (tq or 0) > 0,
           f"q={tq} / keyword={tk}")

    # 站名模糊匹配（人可读的 Source.name，区别于程序用的 adapter_name）
    r = client.get(f"{api}/articles", headers=H,
                   params={"source_name": "电网", "page_size": 1})
    _check("GET /articles?source_name=电网",
           r.status_code == 200 and ((_data(r) or {}).get("total") or 0) > 0,
           f"total={(_data(r) or {}).get('total')}")

    r = client.get(f"{api}/sources", headers=H, params={"q": "电力"})
    t_sq = (_data(r) or {}).get("total")
    _check("GET /sources?q=电力 模糊匹配站名", r.status_code == 200 and (t_sq or 0) > 0,
           f"total={t_sq}")

    # /sources 的 keyword 别名
    r = client.get(f"{api}/sources", headers=H, params={"keyword": "电力"})
    _check("GET /sources?keyword=电力 与 q 等价",
           r.status_code == 200 and (_data(r) or {}).get("total") == t_sq,
           f"total={(_data(r) or {}).get('total')} vs q={t_sq}")

    # ============ 5. 统计接口 ============
    _section("5. 统计接口 /stats")
    r = client.get(f"{api}/stats/overview", headers=H)
    ov = _data(r) or {}
    _check("GET /stats/overview", r.status_code == 200 and bool(ov),
           f"HTTP {r.status_code} keys={sorted(ov)[:6]}")
    if "articles" in ov:
        _check("overview.articles > 0", (ov["articles"] or 0) > 0, f"articles={ov['articles']}")

    for path in ("quality", "provinces", "sources", "jobs", "runs", "funnel"):
        r = client.get(f"{api}/stats/{path}", headers=H)
        _check(f"GET /stats/{path}", r.status_code == 200 and _data(r) is not None,
               f"HTTP {r.status_code}")

    r = client.post(f"{api}/stats/snapshot", headers=H)
    _check("POST /stats/snapshot 落快照", r.status_code in (200, 201),
           f"HTTP {r.status_code}")

    # ============ 6. 来源接口 ============
    _section("6. 来源接口 /sources")
    r = client.get(f"{api}/sources", headers=H, params={"page_size": 5})
    d = _data(r) or {}
    src_total = d.get("total")
    src_items = d.get("items") or []
    _check("GET /sources 列表", r.status_code == 200, f"total={src_total}")
    if src_items:
        sid = src_items[0]["id"]
        r = client.get(f"{api}/sources/{sid}", headers=H)
        _check(f"GET /sources/{{id}} id={sid}", r.status_code == 200, f"HTTP {r.status_code}")

    if args.write:
        _section("6b. 来源写接口（--write）")
        # 字段名对齐 SourceCreate：是 adapter_name 不是 adapter；
        # priority 上限 9；crawl_interval_hours 上限 127（DDL 是 TINYINT）。
        # enabled=False + 最大 interval：这个临时来源不会被协调器挑走。
        payload = {
            "adapter_name": "heuristic",
            "name": "e2e-smoke-temp-source",
            "category": "sgcc",
            "province_code": "110000",
            "base_url": "https://example.com",
            "news_path": "/e2e-smoke",
            "list_url": "https://example.com/e2e-smoke",
            "priority": 9,
            "crawl_interval_hours": 120,
            "enabled": False,
            "note": "e2e-smoke 自动创建，跑完即删",
        }
        r = client.post(f"{api}/sources", headers=H, json=payload)
        new_src = _data(r) or {}
        new_id = new_src.get("id")
        ok_create = _check("POST /sources 创建", r.status_code in (200, 201) and new_id,
                           f"HTTP {r.status_code} id={new_id} "
                           f"{_body(r).get('errors') or ''}")

        # 越界值必须被挡在 4xx：DDL 的 TINYINT 上限是 127
        oversize = dict(payload, crawl_interval_hours=200)
        r = client.post(f"{api}/sources", headers=H, json=oversize)
        _check("crawl_interval_hours 越界 -> 4xx（不是 500）",
               400 <= r.status_code < 500, f"HTTP {r.status_code} code={_body(r).get('code')}")

        if ok_create:
            r = client.patch(f"{api}/sources/{new_id}", headers=H,
                             json={"note": "e2e-smoke", "priority": 8})
            _check("PATCH /sources/{id}", r.status_code == 200, f"HTTP {r.status_code}")
            r = client.post(f"{api}/sources/{new_id}/disable", headers=H)
            _check("POST /sources/{id}/disable", r.status_code == 200, f"HTTP {r.status_code}")
            r = client.post(f"{api}/sources/{new_id}/enable", headers=H)
            _check("POST /sources/{id}/enable", r.status_code == 200, f"HTTP {r.status_code}")
            r = client.delete(f"{api}/sources/{new_id}", headers=H)
            _check("DELETE /sources/{id} 清理", r.status_code in (200, 204),
                   f"HTTP {r.status_code}")

    # ============ 7. 管理接口 ============
    _section("7. 管理接口 /admin")
    r = client.get(f"{api}/admin/api-keys", headers=H)
    keys_data = _data(r) or {}
    keys = keys_data.get("items") if isinstance(keys_data, dict) else keys_data
    _check("GET /admin/api-keys", r.status_code == 200,
           f"HTTP {r.status_code} 共 {len(keys or [])} 把 Key")

    r = client.get(f"{api}/admin/audit-logs", headers=H, params={"page_size": 5})
    _check("GET /admin/audit-logs", r.status_code == 200,
           f"HTTP {r.status_code} total={(_data(r) or {}).get('total')}")

    # 只读 Key 越权检查
    r = client.post(f"{api}/admin/api-keys", headers=H,
                    json={"name": "e2e-smoke-readonly", "scopes": "articles:read"})
    ro_key = (_data(r) or {}).get("key")
    ro_id = (_data(r) or {}).get("id")
    if ro_key:
        r = client.get(f"{api}/articles", headers={"Authorization": f"Bearer {ro_key}"})
        _check("只读 Key 可访问 /articles", r.status_code == 200, f"HTTP {r.status_code}")
        r = client.post(f"{api}/sources", headers={"Authorization": f"Bearer {ro_key}"},
                        json={"name": "x", "category": "sgcc", "list_url": "https://x.test"})
        _check("只读 Key 调写接口 -> 403", r.status_code == 403, f"HTTP {r.status_code}")
        r = client.delete(f"{api}/admin/api-keys/{ro_id}", headers=H)
        _check("DELETE 只读 Key 清理", r.status_code in (200, 204), f"HTTP {r.status_code}")

    # ============ 汇总 ============
    _section("汇总")
    print(f"  PASS {_PASS} / FAIL {_FAIL}")
    if _FAIL:
        print("\n  失败明细：")
        for name, extra in _DETAIL:
            print(f"    - {name}: {extra}")
    print()
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
