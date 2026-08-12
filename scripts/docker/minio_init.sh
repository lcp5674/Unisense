#!/bin/bash
# MinIO 初始化脚本：创建审计归档 bucket + 设置策略
# 用法：./minio_init.sh [minio_host] [access_key] [secret_key]
# 依赖：mc (MinIO Client)

set -euo pipefail

MINIO_HOST="${1:-localhost:19000}"
ACCESS_KEY="${2:-minioadmin}"
SECRET_KEY="${3:-minioadmin}"
BUCKET_NAME="unisense-audit-archive"

echo "==> 配置 mc alias..."
mc alias set unisense http://${MINIO_HOST} ${ACCESS_KEY} ${SECRET_KEY} 2>/dev/null || {
  echo "ERROR: 无法连接 MinIO at ${MINIO_HOST}"
  exit 1
}

echo "==> 创建 bucket: ${BUCKET_NAME}..."
mc mb unisense/${BUCKET_NAME} 2>/dev/null || echo "Bucket 已存在，跳过"

echo "==> 设置 bucket 版本化（审计留痕需要）..."
mc version enable unisense/${BUCKET_NAME} 2>/dev/null || echo "版本化已启用，跳过"

echo "==> 设置生命周期规则（归档 365 天后自动删除）..."
mc ilm rule add unisense/${BUCKET_NAME} --expire-days 365 2>/dev/null || echo "生命周期规则已存在"

echo "==> 验证..."
mc ls unisense/${BUCKET_NAME} >/dev/null 2>&1 || {
  echo "ERROR: Bucket 验证失败"
  exit 1
}

echo "==> MinIO 初始化完成: ${BUCKET_NAME}"
