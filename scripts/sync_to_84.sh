#!/usr/bin/env bash
# =====================================================================
# 同步项目到 192.168.101.84 —— **排除 docs/**
#
# 为什么要单独写脚本而不是手敲 rsync：
#   手敲时排除规则极易漏。一旦 docs/ 被同步过去，本地工作笔记
#   （省份攻克日志、逆向报告、含内网信息的分析）就落到另一台机器上了。
#   把排除清单固化在脚本里，比靠人记住可靠。
#
# 用法：
#   bash scripts/sync_to_84.sh --dry-run     # 先看会传哪些文件（务必先跑）
#   bash scripts/sync_to_84.sh               # 实际同步
#   bash scripts/sync_to_84.sh --check       # 只检查连通性与排除规则
#
# 前置：本机需有 rsync（Git Bash 自带），且能 ssh 到 84。
# =====================================================================
set -euo pipefail

REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_HOST="${REMOTE_HOST:-192.168.101.84}"
REMOTE_DIR="${REMOTE_DIR:-/opt/power-news}"
SSH_PORT="${SSH_PORT:-22}"

LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------
# 排除清单 —— **docs/ 在第一位，这是用户明确要求的**
#
# 其余排除项都是「不该进另一台机器」的类别：
#   运行时产物（data/）体积大且可在目标机重建
#   虚拟环境与缓存（.venv/pycache）目标机自己装
#   密钥（.env）必须手工放置，不能随同步泄漏
# ---------------------------------------------------------------------
EXCLUDES=(
  "docs/"                 # ← 用户要求：本地工作笔记不同步
  ".git/"
  ".venv/"
  "venv/"
  "__pycache__/"
  "*.pyc"
  ".env"                  # 密钥手工放
  ".env.local"
  "data/"                 # 运行时数据（raw/csv/images），目标机重建
  "_dist/"
  "node_modules/"
  "web/node_modules/"
  "web/dist/"
  "*.log"
  ".pytest_cache/"
  ".ruff_cache/"
  "samples/"
)

RSYNC_ARGS=(-az --delete --human-readable --itemize-changes)
for pattern in "${EXCLUDES[@]}"; do
  RSYNC_ARGS+=(--exclude "$pattern")
done

MODE="${1:-sync}"

case "$MODE" in
  --check)
    echo "=== 同步配置检查 ==="
    echo "  本机目录 : $LOCAL_DIR"
    echo "  目标     : ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}"
    echo "  SSH 端口 : ${SSH_PORT}"
    echo
    echo "  排除清单（${#EXCLUDES[@]} 项）："
    for pattern in "${EXCLUDES[@]}"; do
      printf "    - %s\n" "$pattern"
    done
    echo
    echo "=== 连通性 ==="
    if command -v ping >/dev/null 2>&1; then
      ping -c 1 -W 2 "$REMOTE_HOST" >/dev/null 2>&1 \
        && echo "  ✓ $REMOTE_HOST 可达" \
        || echo "  ✗ $REMOTE_HOST ping 不通（可能禁 ICMP，继续试 ssh）"
    fi
    if ssh -p "$SSH_PORT" -o BatchMode=yes -o ConnectTimeout=5 \
         "${REMOTE_USER}@${REMOTE_HOST}" "echo ok" >/dev/null 2>&1; then
      echo "  ✓ SSH 免密登录可用"
    else
      echo "  ⚠ SSH 需密码或密钥未配置（同步时会提示输入密码）"
    fi
    echo
    echo "=== docs/ 是否会被传过去（应为空）==="
    rsync "${RSYNC_ARGS[@]}" --dry-run "$LOCAL_DIR/" \
      "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/" 2>/dev/null \
      | grep -i "docs/" || echo "  ✓ 没有任何 docs/ 文件会被传输"
    ;;

  --dry-run)
    echo "=== dry-run：只显示会传什么，不实际传输 ==="
    echo "（重点确认输出里**没有** docs/）"
    echo
    rsync "${RSYNC_ARGS[@]}" --dry-run "$LOCAL_DIR/" \
      "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"
    ;;

  sync|"")
    echo "=== 同步 $LOCAL_DIR -> ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR} ==="
    echo "（已排除 ${#EXCLUDES[@]} 项，含 docs/）"
    echo
    ssh -p "$SSH_PORT" "${REMOTE_USER}@${REMOTE_HOST}" "mkdir -p '$REMOTE_DIR'"
    rsync "${RSYNC_ARGS[@]}" "$LOCAL_DIR/" \
      "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"
    echo
    echo "=== 传输后核验：目标机上的 docs/ 应不存在 ==="
    if ssh -p "$SSH_PORT" "${REMOTE_USER}@${REMOTE_HOST}" \
         "test -d '$REMOTE_DIR/docs' && echo present || echo absent" | grep -q absent; then
      echo "  ✓ 目标机上没有 docs/ 目录"
    else
      echo "  ⚠ 目标机上存在 docs/ —— 若是历史遗留，手工清理："
      echo "      ssh ${REMOTE_USER}@${REMOTE_HOST} 'rm -rf ${REMOTE_DIR}/docs'"
    fi
    ;;

  *)
    echo "用法: bash scripts/sync_to_84.sh [--check|--dry-run]"
    exit 1
    ;;
esac
