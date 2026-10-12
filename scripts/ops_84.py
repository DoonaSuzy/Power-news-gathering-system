"""通过 SSH 侦察 / 操作 192.168.101.84 上的 Docker 环境。

## 为什么用 paramiko 而不是 ssh + sshpass

本机（Windows）没有 sshpass / plink / expect，OpenSSH 客户端又无法在
非交互场景下喂密码。paramiko 是纯 Python 实现，跨平台且**密码只走内存**。

## 密码怎么传

**不放脚本里、不写进命令行**（都会落到文件或进程列表）——
用环境变量：

    SSHPASS_84=<密码> python scripts/ops_84.py --recon

## 子命令

    --recon     只侦察：列容器 / 项目 / 卷 / 网络 / 磁盘，**不改任何东西**
    --stop-newsagg 停止 newsagg 相关的容器（用 stop，**不删卷**）

## 安全约束（写死在脚本里）

1. **只用 `docker stop`，绝不用 `docker rm -v` / `volume rm`** ——
   数据卷保留，随时可恢复
2. 停止前**打印将影响哪些容器**，便于人工核对
3. 不碰 MySQL 的数据卷
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

HOST = os.getenv("REMOTE_HOST", "192.168.101.84")
USER = os.getenv("REMOTE_USER", "zhu")
PORT = int(os.getenv("REMOTE_PORT", "22"))

#: 侦察命令清单：(标题, shell 命令)
RECON_COMMANDS: list[tuple[str, str]] = [
    ("系统信息", "uname -a && cat /etc/os-release 2>/dev/null | head -3"),
    ("Docker 版本", "docker version --format 'Server: {{.Server.Version}}' 2>&1 | head -2"),
    ("运行中的容器", "docker ps --format 'table {{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}'"),
    ("全部容器（含已停）", "docker ps -a --format 'table {{.Names}}\\t{{.Image}}\\t{{.Status}}'"),
    ("compose 项目标签", "docker ps -a --format '{{.Names}}|{{.Label \"com.docker.compose.project\"}}|{{.Label \"com.docker.compose.service\"}}'"),
    ("数据卷", "docker volume ls --format 'table {{.Name}}\\t{{.Driver}}'"),
    ("网络", "docker network ls --format 'table {{.Names}}\\t{{.Driver}}'"),
    ("磁盘", "df -h / /opt 2>/dev/null | head -4"),
    ("项目目录", "ls -la /opt/power-news 2>/dev/null | head -20 || echo '(无 /opt/power-news)'"),
    ("其它可能位置", "ls -d /root/*news* /home/*/*news* /opt/*news* /srv/*news* 2>/dev/null || echo '(未找到其它项目目录)'"),
    ("监听端口", "ss -ltnp 2>/dev/null | grep -E ':(8000|8080|9000|9001|3306) ' || netstat -ltnp 2>/dev/null | grep -E ':(8000|8080|9000|9001|3306) '"),
]


def connect():
    """建立 SSH 连接。密码从环境变量取。"""
    import paramiko

    password = os.getenv("SSHPASS_84") or os.getenv("REMOTE_PASSWORD")
    if not password:
        print("✗ 未提供密码。用法：")
        print("    SSHPASS_84=<密码> python scripts/ops_84.py --recon")
        return None

    client = paramiko.SSHClient()
    # 首次连接自动记录主机指纹（内网环境可接受；换成 StrictHostKeyChecking=yes 更安全）
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            HOST, port=PORT, username=USER, password=password,
            timeout=15, banner_timeout=15, auth_timeout=15,
            look_for_keys=False, allow_agent=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"✗ SSH 连接失败：{type(exc).__name__}: {exc}")
        return None
    return client


def run(client, cmd: str, timeout: int = 30) -> tuple[int, str, str]:
    """执行一条远程命令，返回 (退出码, stdout, stderr)。"""
    _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return stdout.channel.recv_exit_status(), out, err


def recon(client) -> int:
    """只侦察，不改动任何东西。"""
    print("=" * 76)
    print(f"侦察 {USER}@{HOST}（只读，不改任何东西）")
    print("=" * 76)
    for title, cmd in RECON_COMMANDS:
        code, out, err = run(client, cmd)
        print(f"\n── {title} " + "─" * max(0, 60 - len(title)))
        if out:
            for line in out.splitlines():
                print(f"  {line}")
        if err and "Warning" not in err:
            print(f"  (stderr) {err[:300]}")
        if not out and not err:
            print("  (无输出)")
    print("\n" + "=" * 76)
    return 0


def find_targets(client, project: str) -> list[tuple[str, str]]:
    """按 **compose 项目名**精确找出目标容器，返回 [(容器名, 说明)]。

    ⚠️ 为什么不用关键词匹配容器名/镜像名：
       早先用 `newsagg` / `powernews` 做模糊匹配，结果**既多停又漏停** ——
       · `power-fastapi` 的镜像叫 `power-news/app:local`，撞了 `power-news` 关键词，
         **本仓库的容器被误判为目标**；
       · 而 `power-redis` / `power-mysql` 的镜像叫 `redis:7-alpine` / `mysql:8.4`，
         **不含关键词，反而漏掉了**。
       两者叠加会留下「停了 fastapi 但留着 redis/mysql」的半残状态。

       正确的维度是 **compose 项目标签**（`com.docker.compose.project`）——
       它是 Docker Compose 写进容器的权威归属标记，一个项目下的容器
       绝不会误伤另一个项目。
    """
    code, out, _ = run(
        client,
        "docker ps -a --format '"
        "{{.Names}}|{{.Label \"com.docker.compose.project\"}}|"
        "{{.Image}}|{{.Status}}'",
    )
    targets: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 4:
            continue
        name, proj, image, status = parts[0], parts[1], parts[2], parts[3]
        if proj == project:                      # 精确等于，不做包含判断
            targets.append((name, f"{image}  [{status}]"))
    return targets


def stop_newsagg(client, project: str, *, dry_run: bool = False) -> int:
    """停止指定 compose 项目下的容器。**只 stop，不 rm、不删卷。**"""
    targets = find_targets(client, project)
    if not targets:
        print(f"✓ compose 项目 `{project}` 下没有容器（无需操作）")
        print("  提示：84 上已知的项目有 newsagg-platform、power-news")
        print("        （用 --recon 看完整清单）")
        return 0

    print("=" * 76)
    print(f"将要停止 compose 项目 `{project}` 下的容器")
    print("**只 stop，不删容器、不删数据卷**")
    print("=" * 76)
    for i, (name, desc) in enumerate(targets, 1):
        print(f"  {i}. {name}\n       {desc}")
    print()

    # 明确显示**不会**被碰的容器 —— 这是「精确停」最重要的反馈
    _c, others, _ = run(
        client,
        "docker ps --format '{{.Names}}|{{.Label \"com.docker.compose.project\"}}'",
    )
    kept = [
        f"{p[0]}（项目 {p[1]}）"
        for p in (line.split("|") for line in others.splitlines())
        if len(p) == 2 and p[1] != project
    ]
    if kept:
        print("保持运行（不受影响）：")
        for k in kept:
            print(f"  · {k}")
        print()

    print("⚠️  数据卷会被**保留**：")
    code, out, _ = run(client, "docker volume ls --format '{{.Name}}'")
    for line in out.splitlines():
        print(f"     - {line}")
    print()

    if dry_run:
        print("（dry-run：未实际执行。去掉 --dry-run 才真正停止）")
        return 0

    # 用 `docker stop` 而不是 `docker compose down`：
    # down 会**删除容器**（虽然保留卷），而 stop 只是停 —— 恢复更快、更可逆。
    names = " ".join(name for name, _ in targets)
    print(f"执行：docker stop {names}")
    code, out, err = run(client, f"docker stop {names}", timeout=120)
    if out:
        for line in out.splitlines():
            print(f"  {line}")
    if code != 0:
        print(f"✗ 部分停止失败：{err[:300]}")
        return code

    print("\n── 停止后仍在运行的容器 " + "─" * 46)
    _c, out, _ = run(client, "docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'")
    for line in out.splitlines():
        print(f"  {line}")

    print("\n── 端口复核 " + "─" * 60)
    _c, out, _ = run(
        client,
        "ss -ltn 2>/dev/null | grep -E ':(8000|8080|5000|9000) ' "
        "|| echo '  ✓ 8000 / 8080 / 5000 / 9000 均未监听'",
    )
    for line in out.splitlines():
        print(f"  {line}")

    print("\n" + "=" * 76)
    print(f"✅ 已停止 compose 项目 `{project}`")
    print(f"   恢复：docker start {names}")
    print("   彻底清理（**会删容器，卷仍保留**）：")
    print(f"     docker rm {names}")
    print("=" * 76)
    return 0


def clean_stopped(client, project: str, *, dry_run: bool = False) -> int:
    """删除指定 compose 项目下**已停止**的容器。

    ⚠️ 只删容器，**绝不删数据卷** —— 用户 2026-10-13 明确选择「删已停容器，
    保留全部数据卷」。所以这里用 ``docker rm``（不带 ``-v``）。
    若带 ``-v`` 会连带删除匿名卷，**可能误删数据**，因此写死不用。

    Args:
        client: SSH 连接。
        project: compose 项目名。
        dry_run: 只展示将删哪些，不实际执行。
    """
    code, out, _ = run(
        client,
        "docker ps -a --format '"
        "{{.Names}}|{{.Label \"com.docker.compose.project\"}}|{{.Status}}'",
    )
    stopped = [
        (p[0], p[2])
        for line in out.splitlines()
        if len(p := line.split("|")) == 3 and p[1] == project
        and not p[2].lower().startswith("up")
    ]
    if not stopped:
        print(f"✓ compose 项目 `{project}` 下没有已停止的容器")
        return 0

    print("=" * 76)
    print(f"将删除 compose 项目 `{project}` 下**已停止**的容器")
    print("（只删容器，**数据卷全部保留**）")
    print("=" * 76)
    for i, (name, status) in enumerate(stopped, 1):
        print(f"  {i}. {name}   [{status}]")

    print("\n数据卷（**不会被删除**）：")
    _c, vols, _ = run(client, "docker volume ls --format '{{.Name}}'")
    for line in vols.splitlines():
        print(f"  · {line}")

    if dry_run:
        print("\n（dry-run：未实际执行）")
        return 0

    names = " ".join(n for n, _ in stopped)
    print(f"\n执行：docker rm {names}   （不带 -v，卷保留）")
    code, out, err = run(client, f"docker rm {names}", timeout=60)
    if out:
        for line in out.splitlines():
            print(f"  {line}")
    if code != 0:
        print(f"✗ 删除失败：{err[:300]}")
        return code

    print("\n── 清理后全部容器 " + "─" * 52)
    _c, out, _ = run(client, "docker ps -a --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'")
    for line in out.splitlines():
        print(f"  {line}")

    print("\n── 数据卷仍在 " + "─" * 60)
    _c, out, _ = run(client, "docker volume ls --format '{{.Name}}'")
    for line in out.splitlines():
        print(f"  {line}")

    print("\n" + "=" * 76)
    print(f"✅ 已清理 `{project}` 的已停容器；数据卷完好")
    print("=" * 76)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="84 主机 Docker 运维")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--recon", action="store_true", help="只侦察（只读）")
    g.add_argument("--stop-newsagg", action="store_true",
                   help="停止 newsagg-platform 项目下的容器")
    g.add_argument("--clean-stopped", action="store_true",
                   help="删除 newsagg-platform 下已停止的容器（保留数据卷）")
    ap.add_argument("--dry-run", action="store_true", help="只看会操作什么，不执行")
    args = ap.parse_args()

    client = connect()
    if client is None:
        return 1
    try:
        if args.recon:
            return recon(client)
        if args.stop_newsagg:
            return stop_newsagg(client, "newsagg-platform", dry_run=args.dry_run)
        return clean_stopped(client, "newsagg-platform", dry_run=args.dry_run)
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
