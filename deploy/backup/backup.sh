#!/bin/sh
# 每日 MySQL 备份 sidecar —— 启动即备份一次，之后每天 BACKUP_AT（默认 03:30）再备。
#
# 为什么做成容器而不是宿主机 cron
# ------------------------------
# 这套部署是「整包上传 + compose up」，宿主机 cron 属于流水线外的野生状态：
# 不进 docker ps、不被部署脚本验收、换机器就丢。复用 mysql:8.4 镜像还能零新镜像。
#
# 为什么 dump 不带 --databases
# ---------------------------
# 带上会写入 CREATE DATABASE / USE，回放时只能回原库名，没法演练到 _restore 库。
#
# 为什么必须 --skip-routines --skip-triggers --skip-events
# --------------------------------------------------------
# 带存储过程/触发器时 mysqldump 会输出 `DELIMITER ;;`，而
# scripts/restore_dump.py 按分号切分且不认 DELIMITER，回放会碎。
# 本项目 DDL 本身就不用它们（sql/schema.sql 开头有说明）。
#
# 恢复演练（在 84 上执行，利用容器内的 MYSQL_ROOT_PASSWORD，不用手打口令）
# --------------------------------------------------------------------
#   cd "/home/zhu/Power News Aggregator/backups" && ls -lt | head -5
#   sha256sum -c powernews_YYYYmmdd-HHMMSS.sql.gz.sha256
#   docker exec power-mysql sh -c 'MYSQL_PWD=$MYSQL_ROOT_PASSWORD mysql -uroot -e "
#     DROP DATABASE IF EXISTS powernews_restore;
#     CREATE DATABASE powernews_restore CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"'
#   gunzip -c powernews_YYYYmmdd-HHMMSS.sql.gz | docker exec -i power-mysql \
#     sh -c 'MYSQL_PWD=$MYSQL_ROOT_PASSWORD mysql -uroot powernews_restore'
#   docker exec power-mysql sh -c 'MYSQL_PWD=$MYSQL_ROOT_PASSWORD mysql -uroot -N -e "
#     SELECT (SELECT COUNT(*) FROM powernews_restore.articles) AS restored,
#            (SELECT COUNT(*) FROM powernews.articles)         AS prod;"'
#   docker exec power-mysql sh -c 'MYSQL_PWD=$MYSQL_ROOT_PASSWORD mysql -uroot -e "
#     DROP DATABASE powernews_restore;"'

set -u
umask 077

BACKUP_DIR=/backup
RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-7}"
BACKUP_AT="${BACKUP_AT:-03:30}"
HOST="${BACKUP_HOST:-mysql}"
USER_="${BACKUP_USER:-root}"

# 口令走配置文件而不是命令行：`mysqldump -p<pass>` 会出现在进程列表里
CNF=/tmp/.backup.cnf
printf '[client]\nhost=%s\nuser=%s\npassword=%s\n' "$HOST" "$USER_" "${BACKUP_PASSWORD:-}" > "$CNF"
chmod 600 "$CNF"
unset BACKUP_PASSWORD

log() { echo "[backup] $(date '+%F %T') $*"; }

# 要备份哪些库：显式指定优先，否则枚举（排除系统库）。
# 必须枚举——同实例里还有省级库 newsagg（6,606 篇），硬编码库名会把它漏掉。
list_databases() {
    if [ -n "${BACKUP_DATABASES:-}" ]; then
        echo "$BACKUP_DATABASES"
        return
    fi
    mysql --defaults-extra-file="$CNF" -N -e 'SHOW DATABASES' 2>/dev/null \
        | grep -vE '^(information_schema|performance_schema|mysql|sys)$' \
        | grep -vE '^(Database)$'
}

dump_one() {
    db="$1"
    stamp=$(date +%Y%m%d-%H%M%S)
    out="$BACKUP_DIR/${db}_${stamp}.sql.gz"

    # 先写 .part，校验通过再改名：中途失败不会留下一个「看起来正常」的坏备份
    if mysqldump --defaults-extra-file="$CNF" \
            --single-transaction --quick --no-tablespaces \
            --set-gtid-purged=OFF --default-character-set=utf8mb4 \
            --skip-routines --skip-triggers --skip-events \
            "$db" 2>/tmp/dump.err | gzip -9 > "$out.part" \
        && gzip -t "$out.part" 2>/dev/null && [ -s "$out.part" ]; then
        mv "$out.part" "$out"
        (cd "$BACKUP_DIR" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")
        log "OK $db -> $(basename "$out") ($(du -h "$out" | cut -f1))"
    else
        rm -f "$out.part"
        log "FAIL $db: $(tail -2 /tmp/dump.err 2>/dev/null | tr '\n' ' ')"
    fi
}

dump_all() {
    log "开始备份（host=$HOST）"
    for db in $(list_databases); do
        dump_one "$db"
    done
    # 保留：mtime 超过 (N-1) 天即删，等价于「保留最近 N 份每日备份」
    find "$BACKUP_DIR" -maxdepth 1 -name '*.sql.gz' -mtime +$((RETAIN_DAYS - 1)) -delete 2>/dev/null
    find "$BACKUP_DIR" -maxdepth 1 -name '*.sql.gz.sha256' -mtime +$((RETAIN_DAYS - 1)) -delete 2>/dev/null
    log "完成，当前保留 $(find "$BACKUP_DIR" -maxdepth 1 -name '*.sql.gz' | wc -l) 份"
}

# 启动即备份一次：部署完立刻能验收，而不是等第二天才看到第一份文件
dump_all

# 之后每天 BACKUP_AT 执行。date -d 失败（无 GNU date）时退化为「24 小时一次」。
while true; do
    now=$(date +%s)
    target=$(date -d "today $BACKUP_AT" +%s 2>/dev/null || echo "")
    if [ -z "$target" ] || [ "$target" -le "$now" ]; then
        target=$(date -d "tomorrow $BACKUP_AT" +%s 2>/dev/null || echo $((now + 86400)))
    fi
    wait_s=$((target - now))
    [ "$wait_s" -lt 60 ] && wait_s=60
    log "下次备份在 ${wait_s}s 后（$BACKUP_AT）"
    sleep "$wait_s"
    dump_all
done
