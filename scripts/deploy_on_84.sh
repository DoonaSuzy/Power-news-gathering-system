#!/usr/bin/env bash
# =====================================================================
# RustFS 部署脚本 —— **在 192.168.101.84 上执行**
#
# 设计前提：Windows 只是开发测试环境，**所有服务跑在 84**。
# 所以这个脚本是 Linux 侧的一键部署/自检入口。
#
# 使用方式（两条路，任选）：
#
#   【路径 1】从 Windows 同步过去后，在 84 上执行
#     bash scripts/sync_to_84.sh                      # 本机执行，排除 docs/
#     ssh <user>@192.168.101.84
#     cd /home/zhu/Power News Aggregator && bash scripts/deploy_on_84.sh --check
#
#   【路径 2】只把本脚本 scp 过去执行
#     scp scripts/deploy_on_84.sh user@192.168.101.84:/tmp/
#     ssh user@192.168.101.84 'bash /tmp/deploy_on_84.sh --check'
#
# 子命令：
#   --check    环境自检（docker / compose / 端口 / .env），不动任何东西
#   --env      生成 .env 的 S3 段（密钥交互输入，**不写进版本库**）
#   --up       起 RustFS 容器 + 建桶 + 验证
#   --verify   验证运行中的实例（含 S3 读写自检）
#   --urls     打印 84 上的可访问 URL 清单
#   --down     停止容器（保留数据卷）
#   --logs     跟踪容器日志
# =====================================================================
set -euo pipefail

# ---------------------------------------------------------------- 配置
# 2026-10-13 实测：84 上的部署目录（有空格，全程加引号）
PROJECT_DIR="${PROJECT_DIR:-/home/zhu/Power News Aggregator}"
API_PORT="${RUSTFS_API_PORT:-9000}"
CONSOLE_PORT="${RUSTFS_CONSOLE_PORT:-9001}"
HEALTH_PATH="/health"
COMPOSE_PROFILE="storage"

# 中台端口：84 上 8000 已被 newsagg-fastapi 占用，本项目中台走 8080
PLATFORM_PORT="${POWERNEWS_PLATFORM_HOST_PORT:-8080}"

# 本机对外 IP（脚本里自动探测，探测失败则用回退值）
detect_ip() {
  ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K[\d.]+' | head -1 \
    || hostname -I 2>/dev/null | awk '{print $1}' \
    || echo "192.168.101.84"
}
HOST_IP="$(detect_ip)"

# 无参数或显式要帮助时，**先打印帮助再校验目录** ——
# 否则在本机（Windows）误跑会先报「项目目录不存在」，
# 把人挡在帮助信息之外，等于没写帮助。
case "${1:-}" in
  ""|--help|-h)
    cat <<EOF
RustFS 部署脚本（在 192.168.101.84 上执行）

用法：
  bash scripts/deploy_on_84.sh --check     # 环境自检（先跑这个）
  bash scripts/deploy_on_84.sh --env       # 生成 .env 的 S3 段（交互输密钥）
  bash scripts/deploy_on_84.sh --up        # 起容器 + 建桶 + 输出 URL
  bash scripts/deploy_on_84.sh --verify    # 验证运行中的实例
  bash scripts/deploy_on_84.sh --urls      # 打印 URL 清单
  bash scripts/deploy_on_84.sh --logs      # 跟踪日志
  bash scripts/deploy_on_84.sh --down      # 停止（保留数据）

环境变量覆盖：
  PROJECT_DIR=/home/zhu/Power News Aggregator RUSTFS_API_PORT=9000 bash scripts/deploy_on_84.sh --check
EOF
    exit 0
    ;;
esac

cd "$PROJECT_DIR" 2>/dev/null || { echo "✗ 项目目录不存在: $PROJECT_DIR"; echo "  （本脚本应在 192.168.101.84 上执行；本机开发环境不需要它）"; exit 1; }

c_ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
c_bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; }
c_warn() { printf "  \033[33m⚠\033[0m %s\n" "$1"; }
hdr()    { printf "\n\033[1m%s\033[0m\n" "$1"; }

need_docker() {
  if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
      return 0
    fi
    c_bad "docker 已安装但守护进程未运行（服务端）"
    printf "      → 启动：sudo systemctl start docker\n"
    return 1
  fi
  c_bad "**未安装 docker**"
  printf "      → 安装：curl -fsSL https://get.docker.com | sh\n"
  printf "      → 免 sudo：sudo usermod -aG docker \$USER && newgrp docker\n"
  return 1
}

compose_cmd() {
  # 优先 `docker compose`（v2 插件），回退 `docker-compose`（v1）
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    echo "docker-compose"
  else
    return 1
  fi
}

port_busy() { ss -ltn 2>/dev/null | grep -q ":$1 " ; }

# ---------------------------------------------------------------- check
do_check() {
  hdr "RustFS 部署自检（目标机：$(hostname) / $HOST_IP）"
  local ok=1

  hdr "[1] 容器运行时"
  if need_docker; then
    c_ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
    if CC="$(compose_cmd)"; then
      c_ok "compose 命令：$CC"
    else
      c_bad "没有 docker compose / docker-compose"
      ok=0
    fi
  else
    ok=0
  fi

  hdr "[2] 项目文件"
  for f in docker-compose.yml .env sql/schema.sql; do
    [ -e "$f" ] && c_ok "$f" || { c_bad "$f 缺失"; ok=0; }
  done

  hdr "[3] compose 里的 rustfs 服务"
  if grep -q "^  rustfs:" docker-compose.yml; then
    c_ok "服务已定义"
    grep -q "rustfs_data:" docker-compose.yml && c_ok "数据卷已定义" || { c_bad "缺 rustfs_data 卷"; ok=0; }
    sed -n 's/.*image: \(rustfs\/rustfs:.*\)/  镜像: \1/p' docker-compose.yml | head -1
  else
    c_bad "docker-compose.yml 里没有 rustfs 服务（需先同步最新代码）"
    ok=0
  fi

  hdr "[4] 必需环境变量（.env）"
  for key in POWERNEWS_S3_ACCESS_KEY POWERNEWS_S3_SECRET_KEY POWERNEWS_S3_BUCKET; do
    if grep -q "^${key}=" .env 2>/dev/null; then
      c_ok "$key 已设置"
    else
      c_bad "$key 未设置（执行 --env 生成）"
      ok=0
    fi
  done
  # 占位值检测 —— 开发密钥不能带到生产
  if grep -qE "^POWERNEWS_S3_(ACCESS|SECRET)_KEY=.*(dev_key|dev_secret)" .env 2>/dev/null; then
    c_warn "**检测到开发占位密钥** —— 上生产前必须更换（执行 --env 重新生成）"
  fi

  hdr "[5] 端口占用"
  for p in "$API_PORT" "$CONSOLE_PORT"; do
    if port_busy "$p"; then
      c_warn "$p 已被占用（若 RustFS 已在跑则正常，否则需改端口）"
    else
      c_ok "$p 空闲"
    fi
  done

  hdr "[6] 与其它服务的端口关系"
  printf "  RustFS  S3 API   : %s\n" "$API_PORT"
  printf "  RustFS  Console  : http://127.0.0.1:%s/rustfs/console/\n" "$CONSOLE_PORT"
  printf "  本项目中台       : %s  （8000 已被 newsagg-fastapi 占用，故走 8080）\n" "$PLATFORM_PORT"

  hdr "[7] 磁盘空间（图片预计 1.5 GB 起，留 10 倍余量）"
  df -h "$PROJECT_DIR" 2>/dev/null | tail -1 | awk '{printf "  可用 %s / 总 %s（已用 %s）\n", $4, $2, $5}'

  printf "\n%s\n" "$(printf '=%.0s' {1..60})"
  if [ "$ok" -eq 1 ]; then
    printf "\033[32m结论：环境就绪，可执行 --up\033[0m\n"
  else
    printf "\033[31m结论：存在阻塞项，见上方 ✗\033[0m\n"
  fi
  printf "%s\n" "$(printf '=%.0s' {1..60})"
  return $((1 - ok))
}

# ---------------------------------------------------------------- env
do_env() {
  hdr "生成 .env 的 S3 配置段"
  if grep -q "^POWERNEWS_S3_ACCESS_KEY=" .env 2>/dev/null; then
    c_warn ".env 里已有 S3 配置，将**覆盖**密钥部分"
  fi

  # 密钥交互输入，不回显 —— 避免落到 shell history 或屏幕截图里
  read -rsp "  Access Key（回车用随机值）: " AK || true; echo
  read -rsp "  Secret Key（回车用随机值）: " SK || true; echo

  # 32 字节随机 → hex，满足 RustFS 对密钥长度的要求（secret ≥ 8）
  AK="${AK:-pw_$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
  SK="${SK:-$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')}"

  # 备份再改：改坏了能回退
  [ -f .env ] && cp .env ".env.bak.$(date +%Y%m%d%H%M%S)"

  # 先删掉旧的 S3 段（避免重复键），再追加
  sed -i '/^# ==== 对象存储/,/^POWERNEWS_S3_SECRET_KEY=/d' .env 2>/dev/null || true
  cat >> .env <<ENVEOF

# ==== 对象存储（RustFS，--profile storage）====
# 生成时间：$(date '+%Y-%m-%d %H:%M:%S')
POWERNEWS_S3_ENDPOINT=http://rustfs:9000
# 对外地址：写进图片 URL 给浏览器用，**必须是宿主机可达的**
POWERNEWS_S3_PUBLIC_ENDPOINT=http://${HOST_IP}:${API_PORT}
POWERNEWS_S3_BUCKET=${POWERNEWS_S3_BUCKET:-powernews-images}
POWERNEWS_S3_ACCESS_KEY=${AK}
POWERNEWS_S3_SECRET_KEY=${SK}
ENVEOF

  c_ok "已写入 .env"
  printf "  Access Key : %s\n" "$AK"
  printf "  Secret Key : %s…（已落盘，此处不再完整回显）\n" "${SK:0:8}"
  printf "  对外端点   : http://%s:%s\n" "$HOST_IP" "$API_PORT"
}

# ---------------------------------------------------------------- up
do_up() {
  hdr "启动 RustFS"
  need_docker || return 1
  CC="$(compose_cmd)" || { c_bad "缺 compose"; return 1; }

  c_ok "拉取镜像（rustfs/rustfs:1.0.1）"
  $CC --env-file .env --profile "$COMPOSE_PROFILE" pull rustfs || c_warn "拉取失败，将用本地缓存"

  c_ok "启动容器"
  $CC --env-file .env --profile "$COMPOSE_PROFILE" up -d rustfs

  hdr "等待就绪（最长 90s）"
  for i in $(seq 1 45); do
    if curl -fsS --max-time 2 "http://127.0.0.1:${API_PORT}${HEALTH_PATH}" >/dev/null 2>&1; then
      c_ok "健康检查通过（${i}×2s）"
      break
    fi
    sleep 2
    [ "$i" -eq 45 ] && c_warn "超时未就绪，用 --logs 查看容器日志"
  done

  hdr "建桶"
  do_create_bucket
  do_urls
}

do_create_bucket() {
  # 优先用容器内的 python/boto3；没有就用 mc；再没有就提示手工建
  if command -v aws >/dev/null 2>&1; then
    set -a; . ./.env; set +a
    AWS_ACCESS_KEY_ID="$POWERNEWS_S3_ACCESS_KEY" \
    AWS_SECRET_ACCESS_KEY="$POWERNEWS_S3_SECRET_KEY" \
    aws --endpoint-url "http://127.0.0.1:${API_PORT}" s3 mb "s3://${POWERNEWS_S3_BUCKET}" 2>/dev/null \
      && c_ok "已建桶 ${POWERNEWS_S3_BUCKET}" || c_warn "建桶失败或已存在"
  elif command -v mc >/dev/null 2>&1; then
    set -a; . ./.env; set +a
    mc alias set pw "http://127.0.0.1:${API_PORT}" "$POWERNEWS_S3_ACCESS_KEY" "$POWERNEWS_S3_SECRET_KEY" >/dev/null 2>&1
    mc mb --ignore-existing "pw/${POWERNEWS_S3_BUCKET}" >/dev/null 2>&1 && c_ok "已建桶 ${POWERNEWS_S3_BUCKET}"
  else
    printf "  (无 aws / mc 客户端，跳过自动建桶)\n"
    printf "  → 可到 Console http://%s:%s/rustfs/console/ 手工创建桶\n" "$HOST_IP" "$CONSOLE_PORT"
    printf "  → 或进容器：docker exec -it power-rustfs sh\n"
  fi
}

# ---------------------------------------------------------------- verify
do_verify() {
  hdr "RustFS 实例验证"
  local ok=1

  hdr "[1] 端口"
  for pair in "$API_PORT S3-API" "$CONSOLE_PORT Console"; do
    set -- $pair
    if port_busy "$1"; then c_ok "$1 ($2) 在监听"; else c_bad "$1 ($2) 未监听"; ok=0; fi
  done

  hdr "[2] 健康端点 ${HEALTH_PATH}"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${API_PORT}${HEALTH_PATH}" || echo 000)"
  if [ "$code" = "200" ]; then c_ok "HTTP 200"; else c_warn "HTTP $code"; fi

  hdr "[3] Console 页面"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${CONSOLE_PORT}/" || echo 000)"
  if [ "$code" = "200" ]; then c_ok "HTTP 200"; else c_warn "HTTP $code"; fi

  hdr "[4] 容器状态"
  docker ps --filter "name=power-rustfs" --format "  {{.Status}}\t{{.Image}}" 2>/dev/null | head -2

  hdr "[5] S3 读写自检"
  if command -v aws >/dev/null 2>&1; then
    set -a; . ./.env; set +a
    export AWS_ACCESS_KEY_ID="$POWERNEWS_S3_ACCESS_KEY"
    export AWS_SECRET_ACCESS_KEY="$POWERNEWS_S3_SECRET_KEY"
    EP="--endpoint-url http://127.0.0.1:${API_PORT}"
    echo "powernews self-test" > /tmp/.pn-selftest.txt
    if aws $EP s3 cp /tmp/.pn-selftest.txt "s3://${POWERNEWS_S3_BUCKET}/_healthcheck/self-test.txt" >/dev/null 2>&1 \
       && aws $EP s3 cp "s3://${POWERNEWS_S3_BUCKET}/_healthcheck/self-test.txt" /tmp/.pn-selftest.out >/dev/null 2>&1 \
       && cmp -s /tmp/.pn-selftest.txt /tmp/.pn-selftest.out; then
      c_ok "写入 → 读取 → 比对 全部通过"
      aws $EP s3 rm "s3://${POWERNEWS_S3_BUCKET}/_healthcheck/self-test.txt" >/dev/null 2>&1 || true
    else
      c_bad "S3 读写自检失败"
      ok=0
    fi
    rm -f /tmp/.pn-selftest.txt /tmp/.pn-selftest.out
  else
    c_warn "无 aws 客户端，跳过读写自检（安装：apt install awscli）"
  fi

  printf "\n%s\n" "$(printf '=%.0s' {1..60})"
  [ "$ok" -eq 1 ] && printf "\033[32m结论：实例工作正常\033[0m\n" || printf "\033[31m结论：有问题，见上方 ✗\033[0m\n"
  printf "%s\n" "$(printf '=%.0s' {1..60})"
  return $((1 - ok))
}

# ---------------------------------------------------------------- urls
do_urls() {
  cat <<EOF

$(printf '=%.0s' {1..68})
RustFS 可访问 URL 清单（宿主机 $(hostname) / ${HOST_IP}）
$(printf '=%.0s' {1..68})

① 本机（84 自身）
  RustFS S3 API   : http://127.0.0.1:${API_PORT}
  RustFS Console  : http://127.0.0.1:${CONSOLE_PORT}/rustfs/console/
  健康检查        : http://127.0.0.1:${API_PORT}${HEALTH_PATH}

② 局域网 / Windows 开发机访问
  RustFS S3 API   : http://${HOST_IP}:${API_PORT}
  RustFS Console  : http://${HOST_IP}:${CONSOLE_PORT}/rustfs/console/
  健康检查        : http://${HOST_IP}:${API_PORT}${HEALTH_PATH}

③ 本项目服务
  中台 API        : http://${HOST_IP}:${PLATFORM_PORT}
  中台 Swagger    : http://${HOST_IP}:${PLATFORM_PORT}/docs
  图片            : http://${HOST_IP}:${PLATFORM_PORT}/images/<站点>/<日期>/<文件>
  ⚠ 8000 已被 newsagg-fastapi 占用，故中台走 ${PLATFORM_PORT}

④ 监控（--profile ops 才起）
  Prometheus      : http://${HOST_IP}:9090
  Grafana         : http://${HOST_IP}:3000

⑤ Console 登录
  Access Key / Secret Key 见 ${PROJECT_DIR}/.env 的 POWERNEWS_S3_* 三项

提示
  · 防火墙需放行：${API_PORT} ${CONSOLE_PORT}（以及 ${PLATFORM_PORT}）
      ufw allow ${API_PORT}/tcp && ufw allow ${CONSOLE_PORT}/tcp
      # 或 firewalld:
      firewall-cmd --add-port=${API_PORT}/tcp --permanent && firewall-cmd --reload
  · 容器内互访用 http://rustfs:9000；写进图片 URL 的必须用 http://${HOST_IP}:${API_PORT}
$(printf '=%.0s' {1..68})
EOF
}

# ---------------------------------------------------------------- down/logs
do_down() {
  hdr "停止 RustFS（保留数据卷）"
  need_docker || return 1
  CC="$(compose_cmd)" || return 1
  $CC --env-file .env --profile "$COMPOSE_PROFILE" down rustfs
  c_ok "已停止（数据卷 rustfs_data 保留；彻底删除加 -v）"
}

do_logs() {
  need_docker || return 1
  docker logs -f --tail 100 power-rustfs
}

# ---------------------------------------------------------------- main
case "${1:-}" in
  --check)  do_check ;;
  --env)    do_env ;;
  --up)     do_up ;;
  --verify) do_verify ;;
  --urls)   do_urls ;;
  --down)   do_down ;;
  --logs)   do_logs ;;
  *)
    cat <<EOF
RustFS 部署脚本（在 192.168.101.84 上执行）

用法：
  bash scripts/deploy_on_84.sh --check     # 环境自检（先跑这个）
  bash scripts/deploy_on_84.sh --env       # 生成 .env 的 S3 段（交互输密钥）
  bash scripts/deploy_on_84.sh --up        # 起容器 + 建桶 + 输出 URL
  bash scripts/deploy_on_84.sh --verify    # 验证运行中的实例
  bash scripts/deploy_on_84.sh --urls      # 打印 URL 清单
  bash scripts/deploy_on_84.sh --logs      # 跟踪日志
  bash scripts/deploy_on_84.sh --down      # 停止（保留数据）

环境变量覆盖：
  PROJECT_DIR=/home/zhu/Power News Aggregator   RUSTFS_API_PORT=9000   bash scripts/deploy_on_84.sh --check
EOF
    exit 1
    ;;
esac
