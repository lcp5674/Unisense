#!/usr/bin/env bash
# =====================================================================
# Unisense 数据备份脚本（生产可用）
#
# 备份内容：
#   - MySQL（mysqldump：--single-transaction --routines --triggers）
#   - Neo4j / Elasticsearch：见 docs/runbooks/backup-restore.md（本脚本
#     主备份 MySQL；Neo4j 需 neo4j-admin dump，ES 需 snapshot API，
#     分别在对应节点/工具上执行，避免镜像内缺客户端）。
#
# 用法（容器内由 docker-compose backup 服务每日触发；也可手动）：
#   BACKUP_DIR=/backups MYSQL_HOST=mysql MYSQL_USER=unisense \
#     MYSQL_PASSWORD=test MYSQL_DATABASE=unisense RETENTION_DAYS=7 \
#     ./backup.sh
#   MYSQL_DATABASES="unisense e2e_biz"  # 多库备份（空格分隔；含 MySQL 降级业务库）
#
# 输出：$BACKUP_DIR/mysql_<db>_YYYYmmdd_HHMMSS.sql.gz
# 轮转：删除超过 RETENTION_DAYS 天的备份文件。
# 失败：任一库失败即 exit 1（compose backup 服务捕获后告警 + 快速重试，不静默吞掉）。
# =====================================================================
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
MYSQL_HOST="${MYSQL_HOST:-mysql}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_USER="${MYSQL_USER:-unisense}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-test}"
MYSQL_DATABASE="${MYSQL_DATABASE:-unisense}"
# P1-2（第八轮）：MYSQL_DATABASES 支持多库（空格分隔），未设置时回退单库 MYSQL_DATABASE
MYSQL_DATABASES="${MYSQL_DATABASES:-$MYSQL_DATABASE}"

TS="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

if ! command -v mysqldump >/dev/null 2>&1; then
  echo "ERROR: mysqldump 不可用，无法执行 MySQL 备份" >&2
  exit 1
fi

RC=0
for db in $MYSQL_DATABASES; do
  [ -n "$db" ] || continue
  echo "[backup] 开始 MySQL 备份: $db@$MYSQL_HOST:$MYSQL_PORT"
  if MYSQL_PWD="$MYSQL_PASSWORD" mysqldump \
    -h"$MYSQL_HOST" \
    -P"$MYSQL_PORT" \
    -u"$MYSQL_USER" \
    --single-transaction \
    --routines \
    --triggers \
    --set-gtid-purged=OFF \
    "$db" \
    | gzip > "$BACKUP_DIR/mysql_${db}_${TS}.sql.gz"; then
    echo "[backup] $db 备份完成: $BACKUP_DIR/mysql_${db}_${TS}.sql.gz ($(du -h "$BACKUP_DIR/mysql_${db}_${TS}.sql.gz" | cut -f1))"
  else
    echo "ERROR: 备份 $db 失败（退出码 $?）" >&2
    RC=1
  fi
done

if [ "$RC" -ne 0 ]; then
  echo "ERROR: 至少一个数据库备份失败，请检查 mysqldump 错误输出" >&2
  exit 1
fi

# ---- 轮转：删除超过保留期的备份 ----
if command -v find >/dev/null 2>&1; then
  find "$BACKUP_DIR" -name 'mysql_*.sql.gz' -mtime "+$RETENTION_DAYS" -delete \
    && echo "[backup] 已清理超过 ${RETENTION_DAYS} 天的旧备份"
fi

echo "[backup] 完成: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
