#!/usr/bin/env bash
# =====================================================================
# Unisense 本地 LLM 部署脚本（生产机 CentOS 7 + Docker 24 验证）
#
# 支持两档模型（LLM_MODEL 切换，可并存双实例）：
#   - 8b        Qwen3-8B       (Q4_K_M, ~4.9GB)  密集，速度快（默认档）
#   - 30b-a3b   Qwen3-30B-A3B  (Q4_K_M, ~18.6GB) MoE：质量≈30B dense、速度≈3B 激活
#                 —— 62G 内存生产机的「最优×最快」甜点（见 README 10.7）
#
# llama.cpp server（OpenAI /v1 兼容，Unisense LLM 路由直接接入）
#
# 用法：
#   bash scripts/deploy_local_llm.sh                       # 默认 8b（端口 8081）
#   LLM_MODEL=30b-a3b bash scripts/deploy_local_llm.sh     # MoE 档（端口 8082）
#   LLM_MODEL_SOURCE=hf bash scripts/deploy_local_llm.sh   # 改用 hf-mirror 下载
#   LLM_PORT=8083 LLM_MEM=16g LLM_CPUS=24 ...              # 覆盖任意参数
#   LLM_NUMA=off bash ...                                  # 关闭 NUMA 绑定
#
# 幂等：模型已存在/容器已运行则跳过，可重复执行。
# 安全：容器加 seccomp=unconfined（本机默认 seccomp 曾拦截 nginx pwrite，
#       llama.cpp 多线程/mmap syscall 同样可能被拦——前车之鉴，勿移除）。
# 双路 NUMA：默认 --numa distribute（容器挂载 /sys/devices/system/node 只读，
#       避免跨 NUMA 访问内存带宽减半）；启动失败自动回退无 NUMA 重启一次。
# =====================================================================
set -euo pipefail

# ---------- 模型档位（LLM_MODEL=8b | 30b-a3b） ----------
LLM_MODEL="${LLM_MODEL:-8b}"
case "${LLM_MODEL}" in
  8b)
    LLM_MODEL_FILE="${LLM_MODEL_FILE:-Qwen3-8B-Q4_K_M.gguf}"
    LLM_REPO="${LLM_REPO:-Qwen/Qwen3-8B-GGUF}"
    LLM_DEFAULT_MEM="8g"; LLM_DEFAULT_PORT="8081"
    LLM_DEFAULT_CPUS="28"; LLM_DEFAULT_CTX="16384"
    LLM_VERIFY_MODEL="qwen3-8b"
    LLM_DISPLAY="Qwen3-8B (dense, Q4_K_M ~4.9GB)"
    ;;
  30b-a3b)
    LLM_MODEL_FILE="${LLM_MODEL_FILE:-Qwen3-30B-A3B-Q4_K_M.gguf}"
    LLM_REPO="${LLM_REPO:-Qwen/Qwen3-30B-A3B-GGUF}"
    LLM_DEFAULT_MEM="28g"; LLM_DEFAULT_PORT="8082"
    LLM_DEFAULT_CPUS="28"; LLM_DEFAULT_CTX="16384"
    LLM_VERIFY_MODEL="qwen3-30b-a3b"
    LLM_DISPLAY="Qwen3-30B-A3B (MoE, Q4_K_M ~18.6GB)"
    ;;
  *)
    echo "[ERR] LLM_MODEL 仅支持 8b | 30b-a3b（当前: ${LLM_MODEL}）"; exit 1
    ;;
esac

# ---------- 可调参数（环境变量覆盖；默认值随档位） ----------
LLM_MODEL_DIR="${LLM_MODEL_DIR:-/data/llm/models}"          # 模型目录（放数据盘）
LLM_PORT="${LLM_PORT:-${LLM_DEFAULT_PORT}}"
LLM_MEM="${LLM_MEM:-${LLM_DEFAULT_MEM}}"                    # 容器内存上限
LLM_CPUS="${LLM_CPUS:-${LLM_DEFAULT_CPUS}}"                 # CPU 线程（双路 32 物理核留 4 给系统）
LLM_CTX="${LLM_CTX:-${LLM_DEFAULT_CTX}}"                    # 上下文长度（NL2SQL 16k 足够）
LLM_NUMA="${LLM_NUMA:-distribute}"                          # distribute | off
LLM_MODEL_SOURCE="${LLM_MODEL_SOURCE:-modelscope}"          # modelscope | hf

# 容器名按档位区分（8b 保持原名兼容既有部署，MoE 档独立命名，可并存）
if [ "${LLM_MODEL}" = "8b" ]; then
  LLM_CONTAINER="unisense-llm"
else
  LLM_CONTAINER="unisense-llm-${LLM_MODEL}"
fi

# 镜像（ghcr 直连失败自动回退 DaoCloud ghcr mirror）
LLM_IMAGE="ghcr.io/ggml-org/llama.cpp:server"
LLM_IMAGE_MIRROR="docker.m.daocloud.io/ghcr.io/ggml-org/llama.cpp:server"

# 模型下载源（按档位仓库）
MODELSCOPE_URL="https://modelscope.cn/models/${LLM_REPO}/resolve/master/${LLM_MODEL_FILE}"
HF_MIRROR_URL="https://hf-mirror.com/${LLM_REPO}/resolve/main/${LLM_MODEL_FILE}"

# ---------- 1. 校验环境 ----------
command -v docker >/dev/null || { echo "[ERR] docker 未安装"; exit 1; }
DOCKER_VER=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "?")
echo "[OK] Docker server: ${DOCKER_VER} | 档位: ${LLM_DISPLAY} | 端口: ${LLM_PORT}"

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

# ---------- 4. 启动容器（幂等 + NUMA 失败回退） ----------
RUN_ARGS_BASE=(
  -d --name "${LLM_CONTAINER}" --restart unless-stopped
  --security-opt seccomp=unconfined
  -m "${LLM_MEM}" --cpus "${LLM_CPUS}"
  -p "${LLM_PORT}:${LLM_PORT}"
  -v "${LLM_MODEL_DIR}:/models:ro"
)
MODEL_ARGS=(
  -m "/models/${LLM_MODEL_FILE}"
  --host 0.0.0.0 --port "${LLM_PORT}"
  -c "${LLM_CTX}" -t "${LLM_CPUS}"
  --jinja
)

start_container() {
  local numa="$1"
  local args=("${RUN_ARGS_BASE[@]}")
  if [ "${numa}" != "off" ]; then
    # 双路 NUMA：挂载 /sys 节点供 llama.cpp 探测（挂载是 docker 选项，放镜像名前）
    args+=(-v /sys/devices/system/node:/sys/devices/system/node:ro)
  fi
  # 镜像名之后才是容器内 llama-server 的启动参数：
  #   --numa/--jinja/-m/-c/-t 等都是 llama-server 参数，必须放镜像名后，
  #   放镜像名前会被 docker run 当成自身参数 → unknown flag: --numa
  args+=("${LLM_IMAGE}" "${MODEL_ARGS[@]}")
  if [ "${numa}" != "off" ]; then
    args+=(--numa "${numa}")
  fi
  docker run "${args[@]}"
}

echo "[3/5] 启动容器 ${LLM_CONTAINER}（NUMA=${LLM_NUMA}，内存 ${LLM_MEM}，线程 ${LLM_CPUS}）..."
if docker ps -a --format '{{.Names}}' | grep -qx "${LLM_CONTAINER}"; then
  echo "[INFO] 容器已存在，执行 recreate（应用最新参数）"
  docker rm -f "${LLM_CONTAINER}" >/dev/null
fi

start_container "${LLM_NUMA}"
if [ "${LLM_NUMA}" != "off" ]; then
  sleep 5
  if ! docker inspect -f '{{.State.Running}}' "${LLM_CONTAINER}" 2>/dev/null | grep -q true; then
    echo "[WARN] NUMA(${LLM_NUMA}) 启动失败（容器退出），回退无 NUMA 重启一次 ..."
    docker logs --tail 20 "${LLM_CONTAINER}" 2>/dev/null || true
    docker rm -f "${LLM_CONTAINER}" >/dev/null 2>&1 || true
    start_container off
  fi
fi

# ---------- 5. 健康检查 ----------
echo "[4/5] 等待模型加载（8B ~1-2 分钟 / 30B-A3B ~2-5 分钟，取决于磁盘）..."
for i in $(seq 1 60); do
  if curl -sf "http://localhost:${LLM_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 5
done
if ! curl -sf "http://localhost:${LLM_PORT}/health" >/dev/null 2>&1; then
  echo "[ERR] 健康检查超时，容器状态与日志："
  docker ps -a --filter "name=${LLM_CONTAINER}" --format '{{.Status}}'
  docker logs --tail 30 "${LLM_CONTAINER}" 2>/dev/null || true
  exit 1
fi

echo "[5/5] 验证 OpenAI /v1 端点（response_format=json_object + enable_thinking=false）..."
RESP=$(curl -sf --max-time 180 "http://localhost:${LLM_PORT}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"${LLM_VERIFY_MODEL}\",\"messages\":[{\"role\":\"system\",\"content\":\"你是数据平台的 AI 助手。直接给出最终答案，不要输出任何思考过程。\"},{\"role\":\"user\",\"content\":\"返回 JSON：{\\\"ok\\\":true}\"}],\"response_format\":{\"type\":\"json_object\"},\"chat_template_kwargs\":{\"enable_thinking\":false},\"max_tokens\":512}" 2>/dev/null || echo "ERR")
echo "${RESP}" | head -c 600
echo

# 断言：解析 JSON 检查 message.content（而非 reasoning_content）含 {"ok":true}。
#   用 python3 解析——curl 原始文本 grep 会被 JSON 转义 \" 干扰而误判。
VERIFY_OK=1
if [ "${RESP}" != "ERR" ]; then
  python3 - "$RESP" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1])
    content = data["choices"][0]["message"].get("content") or ""
    sys.exit(0 if '"ok":true' in content else 1)
except Exception:
    sys.exit(1)
PY
  VERIFY_OK=$?
fi

if [ "${VERIFY_OK}" -eq 0 ]; then
  echo
  echo "=============================================================="
  echo "✅ 本地 LLM 部署成功！"
  echo "   档位:   ${LLM_DISPLAY}"
  echo "   容器:   ${LLM_CONTAINER}"
  echo "   端点:   http://localhost:${LLM_PORT}/v1"
  echo "   model:  ${LLM_VERIFY_MODEL}（实际以 GGUF 为准）"
  echo "=============================================================="
  echo "下一步：登录 Unisense → 系统配置 → LLM 路由 → 新增实例："
  echo "  名称:     本地 ${LLM_VERIFY_MODEL}"
  echo "  provider: custom"
  echo "  base_url: http://<本机IP>:${LLM_PORT}/v1"
  echo "  model:    ${LLM_VERIFY_MODEL}"
  echo "  api_key:  任意占位（本地无鉴权，如 local）"
  echo "  timeout:  60（CPU 推理预留；30b-a3b 建议 90）"
  echo "  priority: 主用填 0；做另一实例/远程兜底填大值"
  echo "  保存后点『测试连通』，再在 AI 问数/NL2SQL 实测。"
else
  echo "[WARN] 健康检查未通过，请查看日志: docker logs ${LLM_CONTAINER}"
  exit 1
fi
