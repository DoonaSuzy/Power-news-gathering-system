"""RustFS 一键部署 / 自检脚本。

## 为什么单独写这个脚本

`docker compose up` 只负责起容器，**建桶、验证连通性、生成图片 URL 前缀**
这些步骤每次都手工敲很容易漏。这里固化成一个可重复执行的脚本。

## 前置条件（脚本会自检并明确报错）

1. **Docker（或兼容的容器运行时）** —— 本机当前**未安装**，见下方「无 Docker 时的替代方案」
2. `.env` 里有 `POWERNEWS_S3_ACCESS_KEY` / `POWERNEWS_S3_SECRET_KEY`

## 用法

    python scripts/deploy_rustfs.py --check              # 只做环境自检，不动任何东西
    python scripts/deploy_rustfs.py --up                 # 起容器 + 建桶 + 验证
    python scripts/deploy_rustfs.py --verify             # 只验证已运行的实例
    python scripts/deploy_rustfs.py --urls               # 打印可访问 URL 清单
    python scripts/deploy_rustfs.py --down               # 停止并保留数据卷

## 无 Docker 时的两条替代路径

**路径 A：WSL 里装 Docker（推荐，最接近 84 主机形态）**

    wsl --install -d Ubuntu            # 若未装 WSL 发行版
    # 进入 WSL 后：
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER      # 重新登录后免 sudo

**路径 B：直接用 RustFS 的 Windows 二进制（无需容器，可立即验证）**

    # 下载 1.0.1 Windows 版（约 106 MB）
    # https://github.com/rustfs/rustfs/releases/download/1.0.1/rustfs-windows-x86_64-v1.0.1.zip
    # 解压后：
    $env:RUSTFS_ACCESS_KEY="powernews_minio_dev"
    $env:RUSTFS_SECRET_KEY="powernews_minio_dev_secret"
    ./rustfs.exe C:\\powernews-s3-data
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ENV_FILE = PROJECT_ROOT / ".env"

#: RustFS 端口
API_PORT = 9000
CONSOLE_PORT = 9001

#: 健康检查端点。**注意**：MinIO 的 ``/minio/health/live`` 在 RustFS 上不存在，
#: 实测用它会 404 —— 这是两个「都兼容 S3」但辅助端点不同的具体例子。
HEALTH_PATH = "/health"


def _load_env() -> dict[str, str]:
    """读 .env（不引依赖，手工解析即可）。

    只关心本脚本要用的几项，避免为一个自检脚本引入 python-dotenv。
    """
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _which_docker() -> str | None:
    """找可用的容器运行时。返回命令名或 None。"""
    for cmd in ("docker", "podman", "nerdctl"):
        if shutil.which(cmd):
            return cmd
    return None


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """端口是否可连（不依赖 nc，用 socket 直连）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_ok(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """访问一个 URL，返回 (是否 2xx, 说明)。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # 401/403 说明服务活着、只是这个端点要鉴权 —— 对「是否起来了」仍是好消息
        return False, f"HTTP {exc.code}（服务在响应，端点可能需鉴权）"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def check() -> int:
    """环境自检。返回 0 = 全部就绪，1 = 有阻塞项。"""
    env = _load_env()
    docker = _which_docker()

    print("=" * 68)
    print("RustFS 部署环境自检")
    print("=" * 68)

    ok = True

    print("\n[1] 容器运行时")
    if docker:
        print(f"    ✓ 找到 {docker}")
        try:
            ver = subprocess.run([docker, "version", "--format", "{{.Server.Version}}"],
                                 capture_output=True, text=True, timeout=20)
            if ver.returncode == 0:
                print(f"      Server 版本: {ver.stdout.strip()}")
            else:
                print(f"      ⚠ 守护进程未运行: {ver.stderr.strip()[:80]}")
                ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"      ⚠ 无法执行 {docker} version: {exc}")
            ok = False
    else:
        print("    ✗ **未找到 docker / podman / nerdctl**")
        print("      → 这是当前唯一的阻塞项。两条替代路径见本脚本文件头部注释：")
        print("        路径 A：在 WSL 里装 Docker（curl -fsSL https://get.docker.com | sh）")
        print("        路径 B：直接用 RustFS 的 Windows 二进制（无需容器）")
        ok = False

    print("\n[2] 配置文件")
    print(f"    {'✓' if COMPOSE_FILE.exists() else '✗'} docker-compose.yml")
    print(f"    {'✓' if ENV_FILE.exists() else '⚠'} .env（缺失则用 .env.example 复制一份）")

    print("\n[3] compose 里的 rustfs 服务")
    if COMPOSE_FILE.exists():
        text = COMPOSE_FILE.read_text(encoding="utf-8")
        has_svc = "rustfs:" in text
        has_vol = "rustfs_data:" in text
        print(f"    {'✓' if has_svc else '✗'} 服务定义")
        print(f"    {'✓' if has_vol else '✗'} 数据卷定义")
        if has_svc:
            for line in text.splitlines():
                if "rustfs/rustfs:" in line:
                    print(f"      镜像: {line.strip().split('image:')[-1].strip()}")
        ok = ok and has_svc and has_vol

    print("\n[4] 必填环境变量")
    for key in ("POWERNEWS_S3_ACCESS_KEY", "POWERNEWS_S3_SECRET_KEY", "POWERNEWS_S3_BUCKET"):
        val = env.get(key) or os.getenv(key)
        if val:
            shown = val if "SECRET" not in key else f"{val[:4]}***"
            print(f"    ✓ {key} = {shown}")
        else:
            print(f"    ✗ {key} 未设置（在 .env 里补上）")
            ok = False

    print("\n[5] 端口占用")
    for port, name in ((API_PORT, "S3 API"), (CONSOLE_PORT, "Console")):
        busy = _port_open("127.0.0.1", port)
        print(f"    {'⚠ 已被占用' if busy else '✓ 空闲'}  {port} ({name})")
        if busy:
            print("      → 可能 RustFS 已在运行（可跑 --verify 确认），或与其它服务冲突")

    print("\n" + "=" * 68)
    print("结论：" + ("✅ 环境就绪，可执行 `--up`" if ok else "❌ 存在阻塞项，见上方 ✗"))
    print("=" * 68)
    return 0 if ok else 1


def up() -> int:
    """起容器 → 建桶 → 验证。"""
    docker = _which_docker()
    if not docker:
        print("✗ 没有可用的容器运行时，无法启动。请先按 --check 的提示处理。")
        return 1
    if not ENV_FILE.exists():
        print("✗ 缺少 .env（先从 .env.example 复制一份并填写密钥）")
        return 1

    compose = [docker, "compose"] if docker == "docker" else [docker, "compose"]
    base = [*compose, "--env-file", str(ENV_FILE), "--profile", "storage"]

    print("① 启动 rustfs 容器 ...")
    r = subprocess.run([*base, "up", "-d", "rustfs"], cwd=PROJECT_ROOT)
    if r.returncode != 0:
        print("✗ 启动失败，检查镜像是否能拉取（镜像名 rustfs/rustfs:1.0.1）")
        return r.returncode

    print("\n② 等待健康检查通过 ...")
    import time

    for i in range(30):
        if _port_open("127.0.0.1", API_PORT):
            print(f"   ✓ {API_PORT} 已就绪（等待 {i * 2}s）")
            break
        time.sleep(2)
    else:
        print("   ⚠ 端口未在 60s 内就绪，用 `docker logs power-rustfs` 查看")

    print("\n③ 验证端点 ...")
    for path in (HEALTH_PATH,):
        ok, msg = _http_ok(f"http://127.0.0.1:{API_PORT}{path}")
        print(f"   {'✓' if ok else '⚠'} {path}  {msg}")

    print("\n④ 建桶（用 S3 API，不依赖 mc 客户端）")
    env = _load_env()
    bucket = env.get("POWERNEWS_S3_BUCKET", "powernews-images")
    access = env.get("POWERNEWS_S3_ACCESS_KEY", "")
    secret = env.get("POWERNEWS_S3_SECRET_KEY", "")
    try:
        import boto3  # type: ignore[import-not-found]

        s3 = boto3.client(
            "s3",
            endpoint_url=f"http://127.0.0.1:{API_PORT}",
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            region_name="us-east-1",
        )
        existing = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
        if bucket in existing:
            print(f"   ✓ 桶已存在: {bucket}")
        else:
            s3.create_bucket(Bucket=bucket)
            print(f"   ✓ 已创建桶: {bucket}")
    except ImportError:
        print("   ⚠ 未装 boto3，跳过建桶。安装后重跑 `--verify`：")
        print("     pip install boto3")
    except Exception as exc:  # noqa: BLE001
        print(f"   ⚠ 建桶失败: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 68)
    print("部署完成。可访问 URL 见 `python scripts/deploy_rustfs.py --urls`")
    print("=" * 68)
    return 0


def verify() -> int:
    """验证已运行的实例。"""
    print("=" * 68)
    print("RustFS 实例验证")
    print("=" * 68)
    ok = True

    print("\n[1] 端口")
    for port, name in ((API_PORT, "S3 API"), (CONSOLE_PORT, "Console")):
        up = _port_open("127.0.0.1", port)
        print(f"    {'✓' if up else '✗'} {port} ({name})")
        ok = ok and up

    print(f"\n[2] 健康端点 {HEALTH_PATH}")
    good, msg = _http_ok(f"http://127.0.0.1:{API_PORT}{HEALTH_PATH}")
    print(f"    {'✓' if good else '⚠'} {msg}")

    print(f"\n[3] Console 页面 {CONSOLE_PORT}")
    good, msg = _http_ok(f"http://127.0.0.1:{CONSOLE_PORT}/")
    print(f"    {'✓' if good else '⚠'} {msg}")

    print("\n[4] S3 读写自检")
    env = _load_env()
    bucket = env.get("POWERNEWS_S3_BUCKET", "powernews-images")
    try:
        import boto3  # type: ignore[import-not-found]

        s3 = boto3.client(
            "s3",
            endpoint_url=f"http://127.0.0.1:{API_PORT}",
            aws_access_key_id=env.get("POWERNEWS_S3_ACCESS_KEY", ""),
            aws_secret_access_key=env.get("POWERNEWS_S3_SECRET_KEY", ""),
            region_name="us-east-1",
        )
        key = "_healthcheck/self-test.txt"
        payload = b"powernews rustfs self test"
        s3.put_object(Bucket=bucket, Key=key, Body=payload)
        got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        s3.delete_object(Bucket=bucket, Key=key)
        if got == payload:
            print("    ✓ 写入 → 读取 → 删除 全部成功，内容一致")
        else:
            print(f"    ✗ 内容不一致: 期望 {payload!r}, 得到 {got!r}")
            ok = False
    except ImportError:
        print("    ⚠ 未装 boto3：pip install boto3")
    except Exception as exc:  # noqa: BLE001
        print(f"    ✗ {type(exc).__name__}: {exc}")
        ok = False

    print("\n" + "=" * 68)
    print("结论：" + ("✅ 实例工作正常" if ok else "❌ 有问题，见上方 ✗"))
    print("=" * 68)
    return 0 if ok else 1


def urls() -> int:
    """打印可访问 URL 清单。"""
    import platform

    hostname = platform.node()
    print("=" * 72)
    print("RustFS 可访问 URL 清单")
    print("=" * 72)

    def block(title: str, host: str, note: str = "") -> None:
        print(f"\n{title}")
        if note:
            print(f"  （{note}）")
        print(f"  S3 API        : http://{host}:{API_PORT}")
        print(f"  Console       : http://{host}:{CONSOLE_PORT}")
        print(f"  健康检查      : http://{host}:{API_PORT}{HEALTH_PATH}")
        print("  Console 登录  : Access Key / Secret Key 见 .env 的 POWERNEWS_S3_*")

    block("① 本机（开发机 DESKTOP）", "127.0.0.1")
    block("② 本机主机名访问", hostname)
    block("③ 局域网其它机器访问", "<本机内网 IP>",
          "用 ipconfig 查；需在防火墙放行 9000/9001")

    print("\n④ 中台（新闻聚合服务）")
    print("  中台本机           : http://127.0.0.1:8123")
    print("  中台 Swagger       : http://127.0.0.1:8123/docs")
    print("  图片（中台挂载）   : http://127.0.0.1:8123/images/<站点>/<日期>/<文件>")

    print("\n⑤ 部署到 192.168.101.84 之后（版本稳定后）")
    print("  S3 API        : http://192.168.101.84:9000")
    print("  Console       : http://192.168.101.84:9001")
    print("  健康检查      : http://192.168.101.84:9000/health")
    print("  中台          : http://192.168.101.84:8080")
    print("  中台 Swagger  : http://192.168.101.84:8080/docs")
    print("  图片          : http://192.168.101.84:8080/images/<站点>/<日期>/<文件>")
    print("  ⚠ 84 上 8000 已被 newsagg-fastapi 占用，中台用 8080（compose 注释里已记录）")

    print("\n" + "-" * 72)
    print("访问提示")
    print("-" * 72)
    print("  · 本机有 HTTP 代理时，curl/browser 访问 127.0.0.1 会走代理导致 502：")
    print("      curl --noproxy '*' http://127.0.0.1:9000/health")
    print("  · Console 首次登录用 .env 里的 POWERNEWS_S3_ACCESS_KEY / SECRET_KEY")
    print("  · S3 端点地址要填「宿主机可达」的地址：容器内用 http://rustfs:9000，")
    print("    宿主机/外部用 http://<IP>:9000 —— 写进图片 URL 时必须用后者")
    print("=" * 72)
    return 0


def down() -> int:
    """停止容器（保留数据卷）。"""
    docker = _which_docker()
    if not docker:
        print("✗ 没有可用的容器运行时")
        return 1
    return subprocess.run(
        [docker, "compose", "--env-file", str(ENV_FILE), "--profile", "storage",
         "down", "rustfs"],
        cwd=PROJECT_ROOT,
    ).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="RustFS 部署与自检")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="只做环境自检")
    g.add_argument("--up", action="store_true", help="起容器 + 建桶 + 验证")
    g.add_argument("--verify", action="store_true", help="验证已运行的实例")
    g.add_argument("--urls", action="store_true", help="打印可访问 URL 清单")
    g.add_argument("--down", action="store_true", help="停止容器（保留数据）")
    args = ap.parse_args()

    if args.check:
        return check()
    if args.up:
        return up()
    if args.verify:
        return verify()
    if args.urls:
        return urls()
    return down()


if __name__ == "__main__":
    raise SystemExit(main())
