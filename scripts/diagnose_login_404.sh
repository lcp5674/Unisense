#!/usr/bin/env bash
# 登录后 404 诊断脚本（生产机用）：
#   1) 登录拿 token，探测登录后前端自动请求的核心接口
#   2) 检查 index.html 引用的静态资源是否可加载（区分「接口 404」vs「静态资源 404」）
# 用法：bash scripts/diagnose_login_404.sh [前端URL，默认 http://localhost:8180]
set -uo pipefail

BASE="${1:-http://localhost:8180}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$DIR/.env.production"

echo "== 0. 定位 .env.production =="
if [[ ! -f "$ENV_FILE" ]]; then
  echo "未找到 $ENV_FILE，请确认在项目根目录执行（或传第二参指定路径）"
  exit 1
fi

PASS="$(grep '^UNISENSE_SEED_ADMIN_PASSWORD=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
if [[ -z "$PASS" ]]; then
  echo "未在 $ENV_FILE 找到 UNISENSE_SEED_ADMIN_PASSWORD"
  exit 1
fi

echo "== 1. 登录 =="
LOGIN="$(curl -s -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$PASS\"}")"
TOKEN="$(echo "$LOGIN" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["data"]["access_token"] if d.get("code")=="OK" else "")' 2>/dev/null)"
if [[ -z "$TOKEN" ]]; then
  echo "登录失败，原始响应："
  echo "$LOGIN" | head -c 500
  exit 1
fi
echo "登录 OK（token 长度 ${#TOKEN}）"

echo ""
echo "== 2. 核心接口探测（404 = 后端缺路由，200 = 接口正常）=="
ENDPOINTS=(
  "auth/me"
  "me/permissions"
  "notify/notifications/unread-count"
  "semantics/dashboard"
  "recommend/metrics?limit=5"
  "recommend/terms?limit=5"
  "observability/metrics/health"
  "assetmap/summary"
)
API404=0
for ep in "${ENDPOINTS[@]}"; do
  code="$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/$ep")"
  printf "  %s  %s\n" "$code" "/api/v1/$ep"
  [[ "$code" == "404" ]] && API404=1
done

echo ""
echo "== 3. 静态资源检测（404 = 前端构建产物/缓存问题）=="
# 取 index.html 引用的 js/css 资源名，逐个 HEAD 探测
HTML="$(curl -s "$BASE/")"
ASSETS="$(echo "$HTML" | grep -oE '/assets/[A-Za-z0-9._-]+\.(js|css)' | sort -u | head -20)"
if [[ -z "$ASSETS" ]]; then
  echo "  index.html 未解析到 /assets 资源（HTML 是否被网关改写？），HTML 前 300 字节："
  echo "$HTML" | head -c 300
  echo ""
else
  STATIC404=0
  while IFS= read -r a; do
    code="$(curl -s -o /dev/null -w "%{http_code}" "$BASE$a")"
    printf "  %s  %s\n" "$code" "$a"
    [[ "$code" == "404" ]] && STATIC404=1
  done <<< "$ASSETS"
fi

# 浏览器在页面未声明 favicon 时会自动请求 /favicon.ico（Chrome/Edge 行为），
# 文件不存在会在 Console 报 404 红字（无害但容易误判为故障）。补探测。
echo ""
echo "== 3.1 自动请求项探测（favicon 等浏览器隐式请求）=="
STATIC404="${STATIC404:-0}"
FAVICON_CODE="$(curl -s -o /dev/null -w "%{http_code}" "$BASE/favicon.ico")"
printf "  %s  /favicon.ico（浏览器隐式请求，404 = 无害噪音；已随 favicon.svg 声明消除）\n" "$FAVICON_CODE"
FAV_SVG_CODE="$(curl -s -o /dev/null -w "%{http_code}" "$BASE/favicon.svg")"
printf "  %s  /favicon.svg（index.html 声明的图标）\n" "$FAV_SVG_CODE"
if [[ "$FAV_SVG_CODE" != "200" ]]; then
  echo "  ⚠ /favicon.svg 非 200：前端镜像未含新 public 资源，需重建 frontend"
  STATIC404=1
fi

echo ""
echo "== 4. 结论 =="
if [[ "$API404" == "1" ]]; then
  echo "→ 存在接口 404：后端镜像缺前端正在调用的路由（前后端版本不一致）。"
  echo "  修复：cd $DIR && git pull && docker compose --env-file .env.production up -d --build backend worker"
elif [[ "$STATIC404" == "1" ]]; then
  echo "→ 存在静态资源 404：浏览器/网关缓存了旧 index.html，引用了已不存在的构建产物。"
  echo "  修复：浏览器 Ctrl+Shift+R 强刷；仍 404 则重建前端：docker compose --env-file .env.production up -d --build frontend"
else
  echo "→ 核心接口与静态资源均正常。请打开浏览器 F12 → Network → 找标红那条（404）→ 把 Request URL 贴给开发者。"
fi
