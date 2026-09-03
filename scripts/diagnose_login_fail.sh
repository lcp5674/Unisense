#!/usr/bin/env bash
# 诊断「登录提示登录失败（纯文案，无错误码）」——用于生产环境快速定位。
# 现象含义：前端 fetch 没拿到 HTTP 响应（网络层/CSP/TypeError），或响应后 JS 异常；
#           而非业务拒绝（密码错/限流会显示具体文案+错误码）。
# 覆盖三类高嫌疑：① 前端产物烤入绝对后端地址（跨域被 CSP 拦）；② LLM/服务内存压力
# OOM 拖垮 backend；③ 后端对该次登录的真实处理（审计/限流）。
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

echo "== 0. 定位 .env.production =="
ENV_FILE=".env.production"
[ -f "$ENV_FILE" ] || ENV_FILE="$(ls .env* 2>/dev/null | head -1)"
echo "env_file=${ENV_FILE:-未找到}"

echo
echo "== 1. 前端产物是否烤入绝对后端地址（有输出=异常，空=正常走 nginx 相对路径）=="
IDX=$(curl -s --max-time 5 http://127.0.0.1:8180/ 2>/dev/null | grep -oE 'assets/index-[A-Za-z0-9_-]+\.js' | head -1)
echo "bundle=${IDX:-未找到 index 资源}"
if [ -n "$IDX" ]; then
  curl -s --max-time 10 "http://127.0.0.1:8180/$IDX" 2>/dev/null \
    | grep -oE 'https?://[^"'"'"' ]*(8100|8000|localhost|127\.0\.0\.1)[^"'"'"' ]*' \
    | sort -u | head -10
  echo "--- 若上面无输出：API 走相对路径（同源 nginx 反代），排除绝对地址/CSP 问题 ---"
fi

echo
echo "== 2. 内存压力（LLM + 全家桶是否逼近 32G / 是否 OOM）=="
free -g | head -2
echo "--- docker stats（按内存排序，前 8）---"
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}' 2>/dev/null | sort -k2 -h | tail -8
echo "--- 内核 OOM 记录（最近）---"
dmesg -T 2>/dev/null | grep -iE "oom|killed process" | tail -5 || echo "（dmesg 无权限或无记录）"

echo
echo "== 3. 后端日志：最近登录处理（审计/限流/异常）=="
docker logs unisense-backend --tail 400 2>&1 | grep -iE "auth\.login|login_failed|AUTH_RATE|AUTH_INVALID|429|Rate limit" | tail -15
echo "--- 若无输出：近 400 行无登录相关日志，登录请求可能根本没到 backend（网络层被拦）---"

echo
echo "== 4. 登录接口连通性（本机真实调用）=="
PASS=""
if [ -n "${ENV_FILE:-}" ]; then
  PASS=$(grep -E "^UNISENSE_SEED_ADMIN_PASSWORD=" "$ENV_FILE" 2>/dev/null | cut -d= -f2)
fi
if [ -z "$PASS" ]; then
  echo "（未从 env 读到 admin 密码，跳过真实登录探测——请用 F12 看浏览器那条 login 请求）"
else
  curl -s --max-time 10 -o /dev/null -w "本机登录 HTTP %{http_code}\n" \
    -X POST http://127.0.0.1:8180/api/v1/auth/login \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"admin\",\"password\":\"$PASS\"}"
fi

echo
echo "== 5. 结论 =="
echo "A. 第 1 步有绝对地址输出  → 前端构建烤入了 http://…:8100/8000，浏览器访问该地址失败（CSP/网络）→ 需用 VITE_API_BASE_URL='' 重建 frontend"
echo "B. 第 2 步内存接近 32G 或有 OOM 记录 → LLM/服务内存压力拖垮 backend（间歇不可达）→ 调小 llama.cpp -m/ctx 或扩容"
echo "C. 第 3 步无登录日志 且 第 4 步本机也失败 → backend 异常（看完整日志）"
echo "D. 第 3 步无日志 但 第 4 步本机 200 → 登录请求没到 backend（浏览器侧网络/安全设备拦 POST）→ 用 F12 看那条 login 请求的状态与响应体"
echo "E. 以上都正常 → 请 F12 → Network 点击 login 请求 → 贴出 Status、Response 与 Console 报错"
