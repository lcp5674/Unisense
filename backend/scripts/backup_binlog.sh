#!/usr/bin/env bash
# =====================================================================
# Unisense MySQL binlog 增量备份脚本（RPO ≤ 15min）
#
# 配合每日全量备份（backup.sh）使用，形成「全量 + 增量」恢复链：
#   - 全量：backup.sh 每日 02:00 mysqldump（RPO≤1d）
#   - 增量：本脚本每 5 分钟 FLUSH LOGS 轮转 + 归档新 binlog 文件（RPO≤15min）
#
# 前置：mysql 容器已开启 binlog（--log-bin=/var/lib/mysql-binlog/binlog），
#   binlog 目录挂载为独立卷 unisense_binlog，本容器只读挂载同卷 /mysql-binlog。
#
# 用法（容器内由 binlog-backup 服务周期触发；也可手动）：
#   BINLOG_SRC=/mysql-binlog BINLOG_DIR=/backups/binlog MYSQL_HOST=mysql \
#     MYSQL_USER=unisense MYSQL_PASSWORD=test RETENTION_DAYS=7 \
#     ./backup_binlog.sh
#
# 输出：
#   $BINLOG_DIR/<YYYYmmdd>/binlog.<NNNNNN>     归档的 binlog 文件（按日分目录）
#   $BINLOG_DIR/position.txt                    最近一次归档的位置（File/Pos）
# 轮转：删除超过 RETENTION_DAYS 天的归档目录。
# =====================================================================
set -euo pipefail

BINLOG_SRC="${BINLOG_SRC:-/mysql-binlog}"
BINLOG_DIR="${BINLOG_DIR:-/backups/binlog}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
MYSQL_HOST="${MYSQL_HOST:-mysql}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_USER="${MYSQL_USER:-unisense}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-test}"

TODAY="$(date +%Y%m%d)"
DEST_DIR="$BINLOG_DIR/$TODAY"
mkdir -p "$DEST_DIR"

# 1) FLUSH LOGS：让当前 binlog 轮转，之后旧文件保持静止（归档安全）
if command -v mysqladmin >/dev/null 2>&1; then
  MYSQL_PWD="$MYSQL_PASSWORD" mysqladmin \
    -h"$MYSQL_HOST" -P"$MYSQL_PORT" -u"$MYSQL_USER" flush-logs \
    >/dev/null 2>&1 || echo "[binlog] WARN: flush-logs 失败（可能无权限/连接抖动），继续归档已存在的文件" >&2
else
  echo "[binlog] WARN: mysqladmin 不可用，跳过 FLUSH LOGS（仅归档已有文件）" >&2
fi

# 2) 归档全部 binlog 文件（幂等：已在目标目录的文件跳过）
if ! ls "$BINLOG_SRC"/binlog.* >/dev/null 2>&1; then
  echo "[binlog] 无 binlog 文件（binlog 尚未开启？）：$BINLOG_SRC"
  exit 0
fi

copied=0
for f in "$BINLOG_SRC"/binlog.*; do
  [ -e "$f" ] || continue
  name="$(basename "$f")"
  if [ ! -f "$DEST_DIR/$name" ]; then
    cp -p "$f" "$DEST_DIR/$name"
    copied=$((copied + 1))
  fi
done

# 3) 记录当前 master 位置（恢复时确定增量起点：全量备份时刻之后的 binlog）
if command -v mysql >/dev/null 2>&1; then
  pos="$(MYSQL_PWD="$MYSQL_PASSWORD" mysql -N -h"$MYSQL_HOST" -P"$MYSQL_PORT" -u"$MYSQL_USER" \
    -e "SHOW MASTER STATUS;" 2>/dev/null | awk '{print $1, $2}')"
  if [ -n "$pos" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $pos" >> "$BINLOG_DIR/position.txt"
    echo "[binlog] 记录位置: $pos"
  fi
fi

# 4) 轮转：删除超过保留期的归档目录
if command -v find >/dev/null 2>&1; then
  find "$BINLOG_DIR" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*' \
    -mtime "+$RETENTION_DAYS" -exec rm -rf {} + \
    && echo "[binlog] 已清理超过 ${RETENTION_DAYS} 天的增量归档"
fi

echo "[binlog] 完成: 归档 $copied 个新文件 → $DEST_DIR ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
