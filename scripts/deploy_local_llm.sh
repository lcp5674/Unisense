#!/usr/bin/env bash
# =====================================================================
# Unisense 本地 LLM 部署脚本（生产机 CentOS 7 + Docker 24 验证）
#
# 部署内容：
#   - Qwen3-8B (Q4_K_M, ~4.9GB) —— 32GB 内存纯 CPU 生产选型（见 README 10.7）
#   - llama.cpp server（OpenAI /v1 兼容，Unisense LLM 路由直接接入）
#
# 用法：
#   bash scripts/deploy_local_llm.sh            # 默认（ModelScope 下载 + ghcr 镜像）
#   LLM_MODEL_SOURCE=hf bash scripts/deploy_local_llm.sh   # 改用 hf-mirror 下载
#
# 幂等：模型已存在/容器已运行则跳过，可重复执行。
# 安全：容器加 seccomp=unconfined（本机默认 seccomp 曾拦截 nginx pwrite，
#       llama.cpp 多线程/mmap syscall 同样可能被拦——前车之鉴，勿移除）。
# =====================================================================
set -euo pipefail

# ---------- 可调参数（环境变量覆盖） ----------
LLM_MODEL_DIR="${LLM_MODEL_DIR:-/data/llm/models}"        # 模型目录（放数据盘）
LLM_PORT="${LLM_PORT:-8081}"                              # 对外端口（避开 8080/8100/8180）
LLM_MEM="${LLM_MEM:-8g}"                                  # 容器内存上限（模型 5G + 上下文）
LLM_CPUS="${LLM_CPUS:-24}"                                # CPU 线程（双路 4214 物理 32 核，留余量）
LLM_CTX="${LLM_CTX:-16384}"                               # 上下文长度（Qwen3 原生 32k，NL2SQL 场景 16k 足够）
LLM_MODEL_SOURCE="${LLM_MODEL_SOURCE:-modelscope}"        # modelscope | hf
LLM_MODEL_FILE="Qwen3-8B-Q4_K_M.gguf"

# 镜像（ghcr 直连失败自动回退 DaoCloud ghcr mirror）
LLM_IMAGE="ghcr.io/ggml-org/llama.cpp:server"
LLM_IMAGE_MIRROR="docker.m.daocloud.io/ghcr.io/ggml-org/llama.cpp:server"
LLM_CONTAINER="unisense-llm"

# 模型下载源
MODELSCOPE_URL="https://modelscope.cn/models/Qwen/Qwen3-8B-GGUF/resolve/master/${LLM_MODEL_FILE}"
HF_MIRROR_URL="https://hf-mirror.com/Qwen/Qwen3-8B-GGUF/resolve/main/${LLM_MODEL_FILE}"

# ---------- 1. 校验环境 ----------
command -v docker >/dev/null || { echo "[ERR] docker 未安装"; exit 1; }
DOCKER_VER=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "?")
echo "[OK] Docker server: ${DOCKER_VER}"

# ---------- 2. 拉取 llama.cpp server 镜像 ----------
echo "[1/5] 拉取镜像 ${LLM_IMAGE} ..."
if ! docker image inspect "${LLM_IMAGE}" >/dev/null 2>&1; then
  if ! docker pull "${LLM_IMAGE}"; then
    echo "[INFO] ghcr 直连失败，回退 DaoCloud ghcr mirror ..."
    docker pull "${LLM_IMAGE_MIRROR}"
    docker tag "${LLM_IMAGE_MIRROR}" "${LLM_IMAGE}"
  fi
else
  echo "[INFO] 镜像已存在，跳过"
fi

# ---------- 3. 下载模型（幂等：已存在且 >1GB 视为完整） ----------
echo "[2/5] 下载模型 ${LLM_MODEL_FILE} -> ${LLM_MODEL_DIR}/ ..."
mkdir -p "${LLM_MODEL_DIR}"
MODEL_PATH="${LLM_MODEL_DIR}/${LLM_MODEL_FILE}"
if [ -f "${MODEL_PATH}" ] && [ "$(stat -c%s "${MODEL_PATH}" 2>/dev/null || echo 0)" -gt 1073741824 ]; then
  echo "[INFO] 模型已存在，跳过下载"
else
  if [ "${LLM_MODEL_SOURCE}" = "hf" ]; then
    URL="${HF_MIRROR_URL}"
  else
    URL="${MODELSCOPE_URL}"
  fi
  echo "[INFO] 下载源: ${URL}"
  # 断点续传 + 静默进度
  curl -fL --retry 3 -C - -o "${MODEL_PATH}.part" "${URL}"
  mv "${MODEL_PATH}.part" "${MODEL_PATH}"
fi
ls -lh "${MODEL_PATH}"

# ---------- 4. 启动容器（幂等） ----------
echo "[3/5] 启动容器 ${LLM_CONTAINER} ..."
if docker ps -a --format '{{.Names}}' | grep -qx "${LLM_CONTAINER}"; then
  echo "[INFO] 容器已存在，执行 recreate（应用最新参数）"
  docker rm -f "${LLM_CONTAINER}" >/dev/null
fi

docker run -d --name "${LLM_CONTAINER}" \
  --restart unless-stopped \
  --security-opt seccomp=unconfined \
  -m "${LLM_MEM}" --cpus "${LLM_CPUS}" \
  -p "${LLM_PORT}:${LLM_PORT}" \
  -v "${LLM_MODEL_DIR}:/models:ro" \
  "${LLM_IMAGE}" \
  -m "/models/${LLM_MODEL_FILE}" \
  --host 0.0.0.0 --port "${LLM_PORT}" \
  -c "${LLM_CTX}" -t "${LLM_CPUS}" \
  --jinja

# ---------- 5. 健康检查 ----------
echo "[4/5] 等待模型加载（8B Q4 约 30-90s，取决于磁盘）..."
for i in $(seq 1 30); do
  if curl -sf "http://localhost:${LLM_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 5
done

echo "[5/5] 验证 OpenAI /v1 端点（response_format=json_object）..."
RESP=$(curl -sf "http://localhost:${LLM_PORT}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"qwen3-8b\",\"messages\":[{\"role\":\"user\",\"content\":\"返回 JSON：{\\\"ok\\\":true}\"}],\"response_format\":{\"type\":\"json_object\"},\"max_tokens\":32}" 2>/dev/null || echo "ERR")
echo "${RESP}" | head -c 500
echo

if echo "${RESP}" | grep -q '"ok"'; then
  echo
  echo "=============================================================="
  echo "✅ 本地 LLM 部署成功！"
  echo "   端点:  http://localhost:${LLM_PORT}/v1"
  echo "   model: qwen3-8b（实际以 GGUF 为准）"
  echo "=============================================================="
  echo "下一步：登录 Unisense → 系统配置 → LLM 路由 → 新增实例："
  echo "  名称:     本地 Qwen3-8B"
  echo "  provider: custom"
  echo "  base_url: http://<本机IP>:${LLM_PORT}/v1"
  echo "  model:    qwen3-8b"
  echo "  api_key:  任意占位（本地无鉴权，如 local）"
  echo "  timeout:  60（CPU 推理预留）"
  echo "  priority: 按需（做主用填 0，做远程兜底填大值）"
  echo "  保存后点『测试连通』，再在 AI 问数/NL2SQL 实测。"
else
  echo "[WARN] 健康检查未通过，请查看日志: docker logs ${LLM_CONTAINER}"
  exit 1
fi
