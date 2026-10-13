"""对比 45 个采集站点在 Windows 与 84（Ubuntu）上的网络连通性。

## 为什么要做这个对比

实测中发现「同一站点在两个环境表现不同」，例如：

| 站点 | Windows | 84 |
| --- | --- | --- |
| 国网天津 | ✅ TCP 可达 :80 | ❌ 连接被拒（0.13s 快速失败） |
| 国网江苏 | ✅ 可达 | ✅ 可达（但 200/404 路径不同） |
| 北极星 | ✅ | ✅ |

若不做对比，很容易把「**本机网络策略**」误判成「**站点反爬**」而白改代码
（本项目已踩过：一度以为是 WAF 检测 UA，实际是出口 IP 段差异）。

## 判定口径

**只测 TCP 层**（能否建连），不测 HTTP 语义：
- TCP 通 = 网络可达，后续 403/412 属**站点防护**（代码/浏览器可解）
- TCP 不通 = **网络层不可达**，代码层无解（需换出口）

**端口顺序 443 → 80**：国网站群常见「80 通、443 不通」，
所以两个都试，记录**实际可用的那个**。

用法::

    python scripts/net_matrix.py                      # 全量对比
    python scripts/net_matrix.py --report out.json    # 结果落盘
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

__all__ = ["probe_local", "probe_remote", "TARGETS_FALLBACK"]

#: 兜底目标（数据库不可用时的静态清单，来自 sources 表）
#: 结构：域名 → 站点名
TARGETS_FALLBACK = {
    "www.tj.sgcc.com.cn": "国网天津", "www.he.sgcc.com.cn": "国网河北",
    "www.js.sgcc.com.cn": "国网江苏", "www.zj.sgcc.com.cn": "国网浙江",
    "www.bjx.com.cn": "北极星电力网", "www.cpnn.com.cn": "中国电力新闻网",
}

PROBE_SCRIPT = r"""
import socket, sys, json
hosts = json.loads(sys.argv[1])
out = {}
for h in hosts:
    ok, port = False, 0
    for p in (443, 80):
        try:
            s = socket.create_connection((h, p), timeout=4); s.close()
            ok, port = True, p; break
        except Exception:
            continue
    out[h] = [ok, port]
print(json.dumps(out))
"""


def probe_local(hosts: list[str], *, timeout: float = 4.0) -> dict[str, tuple[bool, int]]:
    """在**当前机器**（Windows）上并发探测 TCP 连通性。

    Args:
        hosts: 域名列表。
        timeout: 单次连接超时（秒）。

    Returns:
        ``{域名: (是否可达, 可用端口)}``。
    """
    def one(host: str) -> tuple[str, tuple[bool, int]]:
        for port in (443, 80):
            try:
                s = socket.create_connection((host, port), timeout=timeout)
                s.close()
                return host, (True, port)
            except OSError:
                continue
        return host, (False, 0)

    with ThreadPoolExecutor(max_workers=20) as ex:
        return dict(ex.map(one, hosts))


def probe_remote(
    hosts: list[str], *, host: str = "192.168.101.84", password: str = "",
    user: str = "zhu",
) -> dict[str, tuple[bool, int]]:
    """在 **84（Ubuntu）** 上探测同一批域名。

    通过 SSH 执行一段内联 Python —— 用 `socket` 而非 `curl`，
    因为要区分「连不上」与「连上但返回 4xx/5xx」，且要拿端口号。

    Args:
        hosts: 域名列表。
        host: 目标机地址。
        password: SSH 密码。
        user: SSH 用户名。

    Returns:
        ``{域名: (是否可达, 可用端口)}``；SSH 不可用时返回全 False。
    """
    import paramiko

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=user, password=password, timeout=15,
              look_for_keys=False, allow_agent=False)
    try:
        # 用 base64 传脚本与参数，避免引号/中文经 SSH 转义出错
        import base64
        b64_script = base64.b64encode(PROBE_SCRIPT.encode()).decode()
        b64_hosts = base64.b64encode(json.dumps(hosts, ensure_ascii=False).encode()).decode()
        cmd = (
            f"echo {b64_script} | base64 -d > /tmp/_probe.py && "
            f"python3 /tmp/_probe.py \"$(echo {b64_hosts} | base64 -d)\" 2>/dev/null || "
            "echo '{}'"   # 非 f-string：{} 是字面量，表示「探测失败返回空结果」
        )
        _i, o, _e = c.exec_command(cmd, timeout=300)
        raw = o.read().decode("utf-8", "replace").strip()
        data = json.loads(raw) if raw else {}
        return {k: (bool(v[0]), int(v[1])) for k, v in data.items()}
    finally:
        c.close()


def load_targets(from_db: bool = True) -> dict[str, str]:
    """取「域名 → 站点名」映射。

    优先读采集库（权威），失败则回退静态清单 ——
    这样脚本在数据库不可用时也能跑，不至于完全失效。
    """
    if not from_db:
        return dict(TARGETS_FALLBACK)
    try:
        from newsagg.config import get_settings
        from newsagg.db import Database
        conn = Database(get_settings()).connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT name, list_url FROM sources WHERE list_url <> ''")
                rows = cur.fetchall()
        finally:
            conn.close()
        out: dict[str, str] = {}
        for r in rows:
            netloc = urlparse(r["list_url"]).netloc
            if netloc:
                out[netloc] = r["name"]
        return out or dict(TARGETS_FALLBACK)
    except Exception as exc:  # noqa: BLE001
        print(f"(读数据库失败，用静态清单: {exc})", file=sys.stderr)
        return dict(TARGETS_FALLBACK)


def main() -> int:
    ap = argparse.ArgumentParser(description="Windows vs 84 连通性对比")
    ap.add_argument("--report", default="data/net_matrix.json")
    ap.add_argument("--password", default="", help="84 的 SSH 密码（或用 SSHPASS_84 环境变量）")
    ap.add_argument("--no-db", action="store_true", help="用静态清单而非数据库")
    args = ap.parse_args()

    import os
    password = args.password or os.environ.get("SSHPASS_84", "")

    targets = load_targets(from_db=not args.no_db)
    hosts = sorted(targets)
    print(f"待测域名 {len(hosts)} 个\n")

    print("[1/2] 探测 Windows（本机）...")
    win = probe_local(hosts)
    print(f"     可达 {sum(1 for v in win.values() if v[0])} / {len(hosts)}")

    print("[2/2] 探测 84（Ubuntu）...")
    lin: dict[str, tuple[bool, int]] = {}
    if password:
        try:
            lin = probe_remote(hosts, password=password)
            print(f"     可达 {sum(1 for v in lin.values() if v[0])} / {len(hosts)}")
        except Exception as exc:  # noqa: BLE001
            print(f"     ✗ SSH 失败: {exc}")
    else:
        print("     ⚠ 未提供密码，跳过（用 --password 或 SSHPASS_84）")

    # ---- 输出对比 ----
    only_win, only_lin, both_ok, both_bad = [], [], [], []
    for h in hosts:
        w_ok = win.get(h, (False, 0))[0]
        l_ok = lin.get(h, (False, 0))[0]
        if w_ok and l_ok:
            both_ok.append(h)
        elif w_ok and not l_ok:
            only_win.append(h)
        elif l_ok and not w_ok:
            only_lin.append(h)
        else:
            both_bad.append(h)

    print("\n" + "=" * 96)
    print(f"{'域名':<32} {'站点':<24} {'Windows':<12} {'84(Ubuntu)':<12} {'结论'}")
    print("-" * 96)

    def cell(v: tuple[bool, int]) -> str:
        return f"✓ :{v[1]}" if v[0] else "✗ 不可达"

    for h in hosts:
        w, l = win.get(h, (False, 0)), lin.get(h, (False, 0))
        if not lin:
            verdict = "—"
        elif w[0] and not l[0]:
            verdict = "★ 仅 Windows 可采"
        elif not w[0] and l[0]:
            verdict = "★ 仅 84 可采"
        elif w[0] and l[0]:
            verdict = "两边都可"
        else:
            verdict = "两边都不可达"
        print(f"{h:<32} {targets[h][:22]:<24} {cell(w):<12} {cell(l):<12} {verdict}")

    print("-" * 96)
    print("\n汇总：")
    print(f"  两边都可       : {len(both_ok)}")
    print(f"  ★ 仅 Windows 可: {len(only_win)}")
    print(f"  ★ 仅 84 可     : {len(only_lin)}")
    print(f"  ★ 两边都不可达 : {len(both_bad)}")

    if only_win:
        print(f"\n★ 只有 Windows 能连、84 连不上的（{len(only_win)} 个）—— 这批需在 Windows 侧采集：")
        for h in only_win:
            print(f"    {h:<32} {targets[h]}")
    if both_bad:
        print(f"\n★ 两边都连不上的（{len(both_bad)} 个）—— 站点侧限制，需换网络出口：")
        for h in both_bad:
            print(f"    {h:<32} {targets[h]}")
    if only_lin:
        print(f"\n★ 只有 84 能连的（{len(only_lin)} 个）：")
        for h in only_lin:
            print(f"    {h:<32} {targets[h]}")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(hosts),
        "windows": {h: {"ok": v[0], "port": v[1]} for h, v in win.items()},
        "linux84": {h: {"ok": v[0], "port": v[1]} for h, v in lin.items()},
        "summary": {
            "both_ok": len(both_ok), "only_windows": len(only_win),
            "only_linux": len(only_lin), "both_bad": len(both_bad),
        },
        "only_windows": only_win,
        "only_linux": only_lin,
        "both_bad": both_bad,
    }
    out = PROJECT_ROOT / args.report
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已落盘：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
