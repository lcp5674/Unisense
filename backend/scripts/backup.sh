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
# 输出：$BACKUP_DIR/mysql_<db>_YYYYmmdd_HHMMSS.sql.gz（未配置加密密钥时）
#       $BACKUP_DIR/mysql_<db>_YYYYmmdd_HHMMSS.sql.gz.enc（配置 BACKUP_ENCRYPTION_KEY 时，AES-256-CBC）
# 轮转：删除超过 RETENTION_DAYS 天的备份文件。
# 失败：任一库失败即 exit 1（compose backup 服务捕获后告警 + 快速重试，不静默吞掉）。
#
# 安全（S15 审查修复）：备份含用户 password_hash 等敏感数据，生产必须配置
#   BACKUP_ENCRYPTION_KEY（≥16 字符）启用 AES-256 加密落盘；未配置时输出明文并告警。
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
# S15：备份加密密钥（可选；生产必须设置，AES-256-CBC 加密落盘）
BACKUP_ENCRYPTION_KEY="${BACKUP_ENCRYPTION_KEY:-}"

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
  if [ -n "$BACKUP_ENCRYPTION_KEY" ]; then
    # S15：AES-256-CBC 加密落盘（-pass env 避免密钥进进程列表；pbkdf2 派生）
    OUT="$BACKUP_DIR/mysql_${db}_${TS}.sql.gz.enc"
    if MYSQL_PWD="$MYSQL_PASSWORD" mysqldump \
      -h"$MYSQL_HOST" \
      -P"$MYSQL_PORT" \
      -u"$MYSQL_USER" \
      --single-transaction \
      --routines \
      --triggers \
      --set-gtid-purged=OFF \
      "$db" \
      | gzip \
      | openssl enc -aes-256-cbc -pbkdf2 -iter 100000 -salt -pass env:BACKUP_ENCRYPTION_KEY \
        > "$OUT"; then
      echo "[backup] $db 备份完成（AES-256 加密）: $OUT ($(du -h "$OUT" | cut -f1))"
    else
      echo "ERROR: 备份 $db 失败（退出码 $?）" >&2
      RC=1
    fi
  else
    OUT="$BACKUP_DIR/mysql_${db}_${TS}.sql.gz"
    echo "[backup] 警告: 未配置 BACKUP_ENCRYPTION_KEY，备份以明文落盘（含 password_hash，生产请启用加密）"
    if MYSQL_PWD="$MYSQL_PASSWORD" mysqldump \
      -h"$MYSQL_HOST" \
      -P"$MYSQL_PORT" \
      -u"$MYSQL_USER" \
      --single-transaction \
      --routines \
      --triggers \
      --set-gtid-purged=OFF \
      "$db" \
      | gzip > "$OUT"; then
      echo "[backup] $db 备份完成: $OUT ($(du -h "$OUT" | cut -f1))"
    else
      echo "ERROR: 备份 $db 失败（退出码 $?）" >&2
      RC=1
    fi
  fi
done

if [ "$RC" -ne 0 ]; then
  echo "ERROR: 至少一个数据库备份失败，请检查 mysqldump 错误输出" >&2
  exit 1
fi

# ---- 轮转：删除超过保留期的备份（兼容明文 .gz 与加密 .gz.enc）----
if command -v find >/dev/null 2>&1; then
  find "$BACKUP_DIR" \( -name 'mysql_*.sql.gz' -o -name 'mysql_*.sql.gz.enc' \) -mtime "+$RETENTION_DAYS" -delete \
    && echo "[backup] 已清理超过 ${RETENTION_DAYS} 天的旧备份"
fi

echo "[backup] 完成: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
