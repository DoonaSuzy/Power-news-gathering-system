"""把本仓库部署到 192.168.101.84 —— 打包传输 + 解压 + 起服务。

## 为什么用「git archive + SFTP」而不是 rsync 或 84 上 git pull

| 方案 | 问题 |
| --- | --- |
| 84 上 `git pull` | 84 **没有 remote**，也**没有 GitHub SSH key**（实测 `Permission denied (publickey)`），且 https 出网不通 |
| `rsync` over ssh | rsync 走 ssh，**密码无法非交互喂入**（本机无 sshpass/plink/expect）；且 rsync 会把未跟踪文件一起推 |
| **git archive + SFTP** ✅ | 只含 **git 跟踪的文件**（天然排除 `.env` / `data/` / `docs/`），SFTP 走 paramiko **密码可编程传入** |

## 安全设计

1. **`.env` 不被传输** —— 用 `git archive` 打包，未跟踪文件天然不在包内；
   84 上原有的 `.env` **先备份、传输后恢复**，密钥永不离开目标机。
2. **`data/` 不被传输** —— 采集产物（raw/csv/images）留在本机；
   84 上 MySQL 卷里的 1,274 篇文章**不受影响**。
3. **密码只走环境变量** `SSHPASS_84`，不进脚本、不进命令行参数。

## 用法

    SSHPASS_84=<密码> python scripts/deploy_84_transfer.py            # 完整流程
    SSHPASS_84=<密码> python scripts/deploy_84_transfer.py --dry-run  # 只打包+试连，不传输
    SSHPASS_84=<密码> python scripts/deploy_84_transfer.py --rustfs   # 连 RustFS 一起起
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REMOTE_DIR = "/home/zhu/Power News Aggregator"
ENV_BACKUP_NAME = ".env.pre-deploy.bak"


def run_local(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """执行本机命令，失败即抛（带上输出便于定位）。"""
    print(f"  $ {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, **kw)
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        raise SystemExit(f"✗ 命令失败（exit {proc.returncode}）")
    return proc


def build_archive(out: Path) -> int:
    """用 git archive 打包 —— 只含 git 跟踪的文件。"""
    head = run_local(["git", "rev-parse", "HEAD"]).stdout.strip()
    print(f"  打包 HEAD = {head[:10]}")

    size = run_local(["git", "archive", "--format=tar.gz", "-o", str(out), "HEAD"])
    _ = size
    n = run_local(["git", "ls-files"]).stdout.splitlines()
    print(f"  包含 {len(n)} 个 git 跟踪的文件，包大小 {out.stat().st_size / 1024:.0f} KB")
    return len(n)


def verify_archive(path: Path, expected_files: int) -> None:
    """传输前校验：① 包能打开 ② 文件数一致 ③ **不含敏感文件**。

    第 ③ 条是关键 —— 万一有人误把 `.env` / `data/` 提交进了 git，
    这里会拦住，不会把密钥推到 84。

    Note:
        ``getnames()`` **包含目录条目** —— tar 里目录是独立成员。
        实测 427 个文件 + 139 个目录 = 566 条，直接与 ``git ls-files`` 的
        427 对比会误报「文件数不符」。所以这里用 ``getmembers()`` + ``isfile()``
        只数**文件**，目录另算。
    """
    print("  校验归档内容 ...")
    with tarfile.open(path, "r:gz") as tf:
        members = tf.getmembers()
    files = [m for m in members if m.isfile()]
    dirs = [m for m in members if m.isdir()]

    if len(files) != expected_files:
        raise SystemExit(
            f"✗ 包内文件数 {len(files)} != git 跟踪数 {expected_files}"
            f"（目录条目 {len(dirs)} 个，已排除）"
        )

    # 敏感检查用**全部成员名**（含目录），避免 `data/` 这种只有目录的情况漏网
    bad = [
        m.name for m in members
        if m.name in (".env", ".env.local")
        or m.name.startswith("data/") or m.name.startswith("docs/")
    ]
    if bad:
        raise SystemExit(
            "✗ 归档中含敏感/排除内容，已中止：\n    " + "\n    ".join(bad[:10])
            + "\n  （说明这些文件被 git 跟踪了 —— 检查 .gitignore 与 git ls-files）"
        )
    print(f"  ✓ {len(files)} 个文件 + {len(dirs)} 个目录，无 .env / data/ / docs/")


def main() -> int:
    ap = argparse.ArgumentParser(description="部署到 192.168.101.84")
    ap.add_argument("--dry-run", action="store_true", help="只打包+试连，不传输不起服务")
    ap.add_argument("--rustfs", action="store_true", help="同时启动 RustFS（profile storage）")
    ap.add_argument("--ops", action="store_true", help="同时启动监控栈（profile ops）")
    args = ap.parse_args()

    import paramiko

    password = os.getenv("SSHPASS_84")
    if not password:
        print("✗ 缺少密码。用法：SSHPASS_84=<密码> python scripts/deploy_84_transfer.py")
        return 1

    # ---------------- ① 打包 ----------------
    print("① 用 git archive 打包（只含 git 跟踪文件）")
    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / "powernews.tar.gz"
        n = build_archive(archive)
        verify_archive(archive, n)

        # ---------------- ② 连接 ----------------
        print("\n② 连接 84")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            "192.168.101.84", port=22, username="zhu", password=password,
            timeout=20, look_for_keys=False, allow_agent=False,
        )
        print("  ✓ 已连接")

        def run(cmd: str, timeout: int = 300):
            _i, o, e = client.exec_command(cmd, timeout=timeout)
            out = o.read().decode("utf-8", "replace")
            err = e.read().decode("utf-8", "replace")
            return o.channel.recv_exit_status(), out, err

        try:
            # ---------------- ③ 备份 .env ----------------
            print("\n③ 备份 84 上的 .env（密钥不离开目标机）")
            code, out, _ = run(f"test -f '{REMOTE_DIR}/.env' && echo yes || echo no")
            has_env = out.strip() == "yes"
            if has_env:
                run(f"cp '{REMOTE_DIR}/.env' '{REMOTE_DIR}/{ENV_BACKUP_NAME}'")
                print(f"  ✓ 已备份到 {ENV_BACKUP_NAME}")
            else:
                print("  (84 上没有 .env，稍后需从 .env.example 建)")

            if args.dry_run:
                print("\n（dry-run：打包与连接已验证，未传输、未起服务）")
                return 0

            # ---------------- ④ 传输 ----------------
            print("\n④ SFTP 传输归档")
            sftp = client.open_sftp()
            remote_tmp = "/tmp/powernews-deploy.tar.gz"
            sftp.put(str(archive), remote_tmp)
            size_kb = archive.stat().st_size / 1024
            print(f"  ✓ 已传输 {size_kb:.0f} KB -> {remote_tmp}")
            sftp.close()

            # ---------------- ⑤ 解压 ----------------
            print("\n⑤ 解压到部署目录")
            code, out, err = run(
                f"mkdir -p '{REMOTE_DIR}' && tar -xzf {remote_tmp} -C '{REMOTE_DIR}' "
                f"&& rm -f {remote_tmp} && echo OK"
            )
            if "OK" not in out:
                print(f"✗ 解压失败：{err[:400]}")
                return 1
            code, out, _ = run(f"ls -1 '{REMOTE_DIR}' | wc -l")
            print(f"  ✓ 解压完成，目录现有 {out.strip()} 个顶层项")

            # ---------------- ⑥ 恢复 .env ----------------
            print("\n⑥ 恢复 .env")
            if has_env:
                run(f"cp '{REMOTE_DIR}/{ENV_BACKUP_NAME}' '{REMOTE_DIR}/.env'")
                print("  ✓ 已恢复原 .env（密钥未被覆盖）")
            else:
                code, out, _ = run(f"test -f '{REMOTE_DIR}/.env' && echo yes || echo no")
                if out.strip() != "yes":
                    print("  ⚠ 84 上没有 .env —— 需手工从 .env.example 创建并填密钥：")
                    print(f"     ssh zhu@192.168.101.84 'cd \"{REMOTE_DIR}\" && cp .env.example .env && vi .env'")

            # ---------------- ⑥.5 上传前端产物 ----------------
            # `web/dist` 不入版本库（构建产物），compose 用 volume 挂载它。
            # 只传 git 归档的话，84 上留着的还是上一次的旧 dist ——
            # 实测就是这样：后端 42 个端点都上了，首页 JS 哈希却没变。
            print("\n⑥.5 上传前端产物 web/dist")
            dist = PROJECT_ROOT / "web" / "dist"
            if not (dist / "index.html").is_file():
                print("  ⚠ 本地没有 web/dist（先 cd web && npm run build），跳过")
            else:
                web_tar = Path(td) / "web-dist.tar.gz"
                with tarfile.open(web_tar, "w:gz") as tf:
                    tf.add(dist, arcname="dist")
                sftp = client.open_sftp()
                sftp.put(str(web_tar), "/tmp/web-dist.tar.gz")
                sftp.close()
                run(f"rm -rf '{REMOTE_DIR}/web/dist' && mkdir -p '{REMOTE_DIR}/web'")
                code, out, err = run(
                    f"tar -xzf /tmp/web-dist.tar.gz -C '{REMOTE_DIR}/web' "
                    f"&& rm -f /tmp/web-dist.tar.gz && echo OK"
                )
                if "OK" in out:
                    print(f"  ✓ 已更新（{web_tar.stat().st_size // 1024} KB）")
                else:
                    print(f"  ✗ 上传失败：{err[:200]}")
                    return 1

            # ---------------- ⑦ 重建镜像 ----------------
            print("\n⑦ 重建镜像（docker compose build）")
            # 不能写成 `... build 2>&1 | tail -5`：管道让 exit code 变成 tail 的（恒 0），
            # 构建失败会被吞掉，脚本照样打印「部署完成」。这里取全量输出，本地截尾显示。
            code, out, err = run(
                f"cd '{REMOTE_DIR}' && docker compose --env-file .env build 2>&1",
                timeout=900,
            )
            for line in out.splitlines()[-8:]:
                print(f"  {line}")
            if code != 0:
                print(f"✗ 构建失败：{err[:400]}")
                return 1

            # ---------------- ⑧ 起服务 ----------------
            print("\n⑧ 启动服务")
            profiles = ""
            if args.rustfs:
                profiles += " --profile storage"
            if args.ops:
                profiles += " --profile ops"
            cmd = f"cd '{REMOTE_DIR}' && docker compose --env-file .env{profiles} up -d 2>&1"
            code, out, err = run(cmd, timeout=600)
            for line in out.splitlines()[-12:]:
                print(f"  {line}")
            if code != 0:
                print(f"✗ 启动失败：{err[:400]}")
                return 1

            # ---------------- ⑧.5 让 Prometheus / Alertmanager 加载新配置 ----------------
            # 配置已改成**目录挂载** + Prometheus 开了 --web.enable-lifecycle，
            # 正常情况下热加载即可，不必重建容器。
            #
            # 历史教训：单文件 bind mount 时 tar 解压会换 inode，容器仍指向旧文件，
            # `kill -HUP` 与 `/-/reload` 都无效（容器里 grep 不到新规则），只能
            # --force-recreate。这里保留重建作为兜底，但先试热加载。
            #
            # 只在显式传 --ops 时做：原实现判断 `alerts.yml` 是否存在（恒真），
            # 于是**不带 --ops 也会把监控栈拉起来**，属于误伤。
            if args.ops:
                print("\n⑧.5 热加载 Prometheus 配置")
                code, _out, err = run(
                    "curl -fsS -X POST http://127.0.0.1:9090/-/reload", timeout=60
                )
                if code == 0:
                    print("  ✓ 已热加载")
                else:
                    print("  ⚠ 热加载失败，回退为重建容器")
                    code, _out, err = run(
                        f"cd '{REMOTE_DIR}' && docker compose --env-file .env --profile ops "
                        f"up -d --force-recreate prometheus 2>&1",
                        timeout=180,
                    )
                    print("  ✓ 已重建" if code == 0 else f"  ⚠ 重建失败（不影响业务）：{err[:200]}")

                # Alertmanager 同样是目录挂载；它没有热加载接口，配置变了就重建。
                run(
                    f"cd '{REMOTE_DIR}' && docker compose --env-file .env --profile ops "
                    f"up -d alertmanager 2>&1",
                    timeout=180,
                )
                code2, _o, _e = run(
                    "curl -fsS --max-time 5 http://127.0.0.1:9093/-/ready", timeout=30
                )
                print("  ✓ Alertmanager 就绪" if code2 == 0 else "  ⚠ Alertmanager 未就绪（不影响采集）")

            # ---------------- ⑨ 验证 ----------------
            print("\n⑨ 部署后验证")
            for label, c in [
                ("容器状态", "docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'"),
                # 注意路径要带 /api/v1：`/health` 不在 API 前缀下，会被 SPA 回退成 index.html，
# 看起来「有响应」其实拿不到健康数据。
                (
                    "中台健康",
                    "curl -fsS --max-time 5 http://127.0.0.1:8080/api/v1/health || echo '(健康端点无响应)'",
                ),
                ("数据卷", "docker volume ls --format '{{.Name}}'"),
            ]:
                code, out, _ = run(c)
                print(f"  ── {label} ──")
                for line in out.splitlines():
                    print(f"    {line}")

            print("\n" + "=" * 70)
            print("✅ 部署完成")
            print(f"   部署目录 : {REMOTE_DIR}")
            print("   中台     : http://192.168.101.84:8080")
            print("   Swagger  : http://192.168.101.84:8080/docs")
            if args.rustfs:
                print("   RustFS   : http://192.168.101.84:9000 (Console :9001)")
            if args.ops:
                print("   Grafana  : http://192.168.101.84:3000  Prometheus :9090")
            print("=" * 70)
            return 0
        finally:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
