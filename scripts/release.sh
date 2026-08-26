#!/usr/bin/env bash
# 发布脚本（第九轮 S-3：发布/回滚载体）
#
# 用法：
#   ./scripts/release.sh build <tag>          # 本地构建 backend/frontend 镜像（不推送）
#   ./scripts/release.sh push  <tag> [registry]  # 构建并推送到 registry（默认本地 tag，不推送）
#
# 回滚：
#   部署侧将 docker-compose 的 UNISENSE_IMAGE_TAG 指回上一发布版本后
#   `docker compose up -d backend worker frontend` 即可整体回滚。
#
# 说明：
#   - 镜像命名与 docker-compose 的 image: unisense/<svc>:${UNISENSE_IMAGE_TAG:-dev} 对齐；
#   - registry 为空时仅本地构建（供内网 load），生产请传入 registry 前缀，
#     例如 ./scripts/release.sh push v1.2.3 registry.example.com/unisense，
#     会构建并推送 registry.example.com/unisense/backend:v1.2.3 等。
set -euo pipefail

ACTION="${1:-build}"
TAG="${2:-dev}"
REGISTRY="${3:-}"

if [[ "$ACTION" != "build" && "$ACTION" != "push" ]]; then
  echo "用法: $0 [build|push] <tag> [registry]" >&2
  exit 2
fi

# 构建镜像（docker-compose 已声明 image: unisense/<svc>:<tag>，build 自动打 tag）
echo "[release] build backend+worker image  unisense/backend:${TAG}"
docker compose build backend
echo "[release] build frontend image        unisense/frontend:${TAG}"
docker compose build frontend

if [[ "$ACTION" == "push" ]]; then
  if [[ -z "$REGISTRY" ]]; then
    echo "[release] 未指定 registry，跳过推送（仅本地构建）" >&2
    exit 0
  fi
  for svc in backend frontend; do
    src="unisense/${svc}:${TAG}"
    dst="${REGISTRY}/unisense/${svc}:${TAG}"
    echo "[release] tag ${src} -> ${dst}"
    docker tag "${src}" "${dst}"
    echo "[release] push ${dst}"
    docker push "${dst}"
  done
fi

echo "[release] done. 部署/回滚: UNISENSE_IMAGE_TAG=${TAG} docker compose up -d backend worker frontend"
