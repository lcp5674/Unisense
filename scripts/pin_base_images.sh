#!/usr/bin/env bash
# 基础镜像 digest 固定脚本（第九轮 S-2：消除浮动 tag 供应链风险）
#
# 用法：在【可联网】的构建/发布机器上运行：
#   ./scripts/pin_base_images.sh            # 检查模式：仅打印当前 tag 对应 digest
#   ./scripts/pin_base_images.sh --apply    # 应用模式：把 Dockerfile/compose 的
#                                           #   <tag> 替换为 <tag>@sha256:...（digest 固定）
#
# 覆盖镜像：
#   backend/Dockerfile    python:3.11-slim（builder + 运行时，两处）
#   frontend/Dockerfile   node:22-alpine（builder）、nginx:alpine（运行时）
#   docker-compose.yml    mysql:8.0、redis:7-alpine、elasticsearch:8.15.0、
#                         neo4j:5-community、minio/minio:latest、apache/doris:*
#
# 说明：digest 固定后 docker pull 不再跟随 tag 漂移（供应链可复现）；升级基础镜像
# 时先改回 tag → 重跑本脚本。应用模式会批量替换，建议先 git 提交再执行以便 diff 审查。
set -euo pipefail

MODE="${1:-check}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

declare -A IMAGES=(
  [python:3.11-slim]="backend/Dockerfile"
  [node:22-alpine]="frontend/Dockerfile"
  [nginx:alpine]="frontend/Dockerfile"
  [mysql:8.0]="docker-compose.yml"
  [redis:7-alpine]="docker-compose.yml"
  [elasticsearch:8.15.0]="docker-compose.yml"
  [neo4j:5-community]="docker-compose.yml"
  [minio/minio:latest]="docker-compose.yml"
)

resolve_digest() {
  local img="$1"
  # 优先平台无关的 manifest list digest；单架构镜像回退 config digest
  docker manifest inspect --verbose "$img" 2>/dev/null \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print(d[0]['Descriptor']['digest'] if isinstance(d,list) else d.get('Descriptor',{}).get('digest',''))" \
    || docker manifest inspect "$img" 2>/dev/null \
       | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('config',{}).get('digest',''))"
}

for img in "${!IMAGES[@]}"; do
  digest="$(resolve_digest "$img")"
  if [[ -z "$digest" || "$digest" == "None" ]]; then
    echo "[pin] WARN 无法解析 $img（网络/镜像不存在？）跳过" >&2
    continue
  fi
  echo "[pin] $img  ->  $img@$digest"
  if [[ "$MODE" == "--apply" ]]; then
    file="$ROOT/${IMAGES[$img]}"
    # 仅在文件里把未带 @ 的裸 tag 替换为带 digest 形式（幂等：已 pin 的跳过）
    sed -i.bak "s|${img}\b|${img}@${digest}|g" "$file" && rm -f "$file.bak"
    echo "      已更新 $file"
  fi
done

if [[ "$MODE" != "--apply" ]]; then
  echo
  echo "[pin] 检查完成。要应用固定请重跑： ./scripts/pin_base_images.sh --apply"
fi
