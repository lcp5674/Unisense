#!/usr/bin/env bash
# 生产密钥生成脚本（方案 A 配套：产出 .env.production 的密钥片段）
#
# 用法：
#   ./scripts/gen_prod_secrets.sh                    # 打印到 stdout（复制粘贴到 .env.production）
#   ./scripts/gen_prod_secrets.sh --out .env.production        # 直接写入（目标已存在则拒绝，防误覆盖）
#   ./scripts/gen_prod_secrets.sh --out .env.production --force  # 强制覆盖（先备份为 .bak.<时间戳>）
#
# 说明：
#   - 生成的密钥均为「URL/Shell 安全字符集」：
#     · 各组件密码用 [A-Za-z0-9]——因为 UNISENSE_MYSQL_PASSWORD 会被 compose 拼接进
#       UNISENSE_DB_URL=mysql+pymysql://<user>:<password>@mysql:3306/...，
#       若含 @ / : / # / ? 会破坏 URL 解析；ES 密码还参与 healthcheck 的
#       `curl -u elastic:<password>`，需 Shell 安全。
#     · 密钥类用十六进制（[0-9a-f]）——杜绝 `$` 触发 compose 变量插值。
#   - UNISENSE_SEED_ADMIN_PASSWORD 额外校验满足系统密码复杂度
#     （≥8 位且含大写/小写/数字/特殊字符中至少 3 类，见 api/users.py:_validate_password_complexity）。
#   - 生成后自动自检：长度达标、不命中 config.py 的弱凭据黑名单、不含非法字符。
#
# ⚠️ 输出含明文密钥：请直接落入目标 .env（已 gitignore），
#    并另存至密码管理器；切勿贴入工单/聊天/提交信息。
set -euo pipefail

OUT_FILE=""
FORCE=0

# ---- 参数解析 ----
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)
      OUT_FILE="${2:-}"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -h | --help)
      sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "未知参数: $1（用法见 $0 --help）" >&2
      exit 2
      ;;
  esac
done

# ---- 随机源检测（openssl 优先，python3 回退）----
if command -v openssl >/dev/null 2>&1; then
  rand_hex() { openssl rand -hex "$1"; }
  # 算术上下文内不得给 $1 加引号（否则展开为带引号字面量导致 syntax error）
  rand_raw() { openssl rand -base64 $(( $1 * 2 )); }
elif command -v python3 >/dev/null 2>&1; then
  rand_hex() { python3 -c "import secrets;print(secrets.token_hex($1))"; }
  rand_raw() { python3 -c "import secrets;print(secrets.token_urlsafe($1 * 2))"; }
else
  echo "错误：需要 openssl 或 python3 之一作为随机源" >&2
  exit 1
fi

# gen_alnum <长度>：生成纯 [A-Za-z0-9] 随机串。
# base64 输出含 +/= 需过滤，过滤后长度会缩短，故循环拼接直至满足长度。
gen_alnum() {
  local want="$1" got="" tries=0
  while [[ ${#got} -lt $want ]]; do
    # 上限保护：随机源异常（tr 恒输出空）时不得死循环
    if [[ $tries -ge 20 ]]; then
      echo "错误：随机源连续 ${tries} 次未产出合法字符（openssl/python3 异常？）" >&2
      return 1
    fi
    got+="$(rand_raw "$want" | LC_ALL=C tr -dc 'A-Za-z0-9')"
    tries=$((tries + 1))
  done
  printf '%s' "${got:0:$want}"
}

# gen_admin_password：生成满足复杂度要求的种子管理员密码
# （大小写+数字三类齐全即满足「至少 3 类」，且保持 URL/Shell 安全）
gen_admin_password() {
  local len=16 pw=""
  for _ in $(seq 1 200); do
    pw="$(gen_alnum "$len")"
    [[ "$pw" =~ [A-Z] ]] && [[ "$pw" =~ [a-z] ]] && [[ "$pw" =~ [0-9] ]] && {
      printf '%s' "$pw"
      return 0
    }
  done
  echo "错误：无法生成满足复杂度的管理员密码（随机源异常）" >&2
  return 1
}

# ---- 生成全部密钥 ----
JWT_SECRET="$(rand_hex 48)"        # 96 hex chars（生产校验要求 ≥32）
FERNET_KEY="$(rand_hex 32)"        # 64 hex chars（PBKDF2 派生源，非标准 Fernet 格式亦可）
MYSQL_ROOT_PASSWORD="$(gen_alnum 24)"
MYSQL_PASSWORD="$(gen_alnum 24)"
ES_PASSWORD="$(gen_alnum 24)"
NEO4J_PASSWORD="$(gen_alnum 24)"
MINIO_ACCESS_KEY="$(gen_alnum 20)" # MinIO 要求 access key ≥3 字符
MINIO_SECRET_KEY="$(gen_alnum 32)" # MinIO 要求 secret key ≥8 字符
BACKUP_ENCRYPTION_KEY="$(rand_hex 32)"
QUICKBI_SIGN_KEY="$(rand_hex 32)"
SEED_ADMIN_PASSWORD="$(gen_admin_password)"

# ---- 自检（对齐 config.py:validate_production_config 的弱凭据黑名单）----
WEAK_VALUES="dev-jwt-secret-change-in-production-32bytes|test|test1234|changeme|es_changeme|minioadmin|admin|password|123456|12345678|secret"
self_check() {
  local name="$1" val="$2" min_len="${3:-0}" rc=0
  if [[ $min_len -gt 0 && ${#val} -lt $min_len ]]; then
    echo "[自检] FAIL $name 长度 ${#val} < $min_len" >&2
    rc=1
  fi
  if [[ "$val" =~ ^($WEAK_VALUES)$ ]]; then
    echo "[自检] FAIL $name 命中弱凭据黑名单" >&2
    rc=1
  fi
  return $rc
}

rc=0
self_check UNISENSE_JWT_SECRET "$JWT_SECRET" 32 || rc=1
for pair in \
  "UNISENSE_FERNET_KEY:$FERNET_KEY" \
  "UNISENSE_MYSQL_ROOT_PASSWORD:$MYSQL_ROOT_PASSWORD" \
  "UNISENSE_MYSQL_PASSWORD:$MYSQL_PASSWORD" \
  "UNISENSE_ES_PASSWORD:$ES_PASSWORD" \
  "UNISENSE_NEO4J_PASSWORD:$NEO4J_PASSWORD" \
  "UNISENSE_MINIO_ACCESS_KEY:$MINIO_ACCESS_KEY" \
  "UNISENSE_MINIO_SECRET_KEY:$MINIO_SECRET_KEY"; do
  self_check "${pair%%:*}" "${pair#*:}" 8 || rc=1
done
# 关键：MySQL 密码进 db_url，必须是 URL 安全字符集
if [[ ! "$MYSQL_PASSWORD" =~ ^[A-Za-z0-9]+$ ]]; then
  echo "[自检] FAIL UNISENSE_MYSQL_PASSWORD 含非 URL 安全字符（会破坏 db_url）" >&2
  rc=1
fi
# 关键：所有值不得含 `$`（compose 会把 ${...} 当变量插值）
if printf '%s\n' "$JWT_SECRET" "$FERNET_KEY" "$MYSQL_PASSWORD" "$ES_PASSWORD" \
  "$NEO4J_PASSWORD" "$MINIO_SECRET_KEY" "$SEED_ADMIN_PASSWORD" | grep -qF '$'; then
  echo "[自检] FAIL 生成的密钥含 '\$'（compose 会误判为变量插值）" >&2
  rc=1
fi
# 种子管理员密码复杂度（大小写+数字三类齐全）
if ! { [[ "$SEED_ADMIN_PASSWORD" =~ [A-Z] ]] && [[ "$SEED_ADMIN_PASSWORD" =~ [a-z] ]] \
  && [[ "$SEED_ADMIN_PASSWORD" =~ [0-9] ]]; }; then
  echo "[自检] FAIL UNISENSE_SEED_ADMIN_PASSWORD 未满足复杂度（需大小写+数字）" >&2
  rc=1
fi
[[ $rc -eq 0 ]] || {
  echo "密钥生成自检未通过，已中止（未写入任何文件）" >&2
  exit 1
}

# ---- 输出 ----
CONTENT="# ---- 由 scripts/gen_prod_secrets.sh 生成（$(date '+%Y-%m-%d %H:%M:%S')）----
# ⚠️ 含明文密钥：本文件已 gitignore，并请另存至密码管理器
UNISENSE_JWT_SECRET=${JWT_SECRET}
UNISENSE_FERNET_KEY=${FERNET_KEY}
UNISENSE_MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD}
UNISENSE_MYSQL_PASSWORD=${MYSQL_PASSWORD}
UNISENSE_ES_PASSWORD=${ES_PASSWORD}
UNISENSE_NEO4J_PASSWORD=${NEO4J_PASSWORD}
UNISENSE_MINIO_ACCESS_KEY=${MINIO_ACCESS_KEY}
UNISENSE_MINIO_SECRET_KEY=${MINIO_SECRET_KEY}
UNISENSE_BACKUP_ENCRYPTION_KEY=${BACKUP_ENCRYPTION_KEY}
UNISENSE_QUICKBI_SIGN_KEY=${QUICKBI_SIGN_KEY}
UNISENSE_SEED_ADMIN_PASSWORD=${SEED_ADMIN_PASSWORD}"

if [[ -n "$OUT_FILE" ]]; then
  if [[ -e "$OUT_FILE" ]]; then
    # 幂等保护：目标已存在时拒绝覆盖——密钥是生产的「锚点」，误覆盖会导致
    # JWT 全失效 / Fernet 无法解密存量密文 / DB/ES/Neo4j 密码与数据卷不一致而崩溃。
    # 确需轮换时显式 --force（先备份再覆盖），并同步改数据卷内密码 + 走 Fernet 密钥链。
    if [[ $FORCE -ne 1 ]]; then
      echo "[gen-secrets] 拒绝覆盖：${OUT_FILE} 已存在。" >&2
      echo "[gen-secrets] 生产环境请勿重复生成密钥（会导致会话全失效、加密数据无法解密、DB 连接失败）。" >&2
      echo "[gen-secrets] 确需轮换请用 --force（会先备份为 .bak.<时间戳>），并同步改数据卷密码 + Fernet 密钥链。" >&2
      exit 1
    fi
    bak="${OUT_FILE}.bak.$(date '+%Y%m%d%H%M%S')"
    cp "$OUT_FILE" "$bak"
    echo "[gen-secrets] 已备份既有文件 -> $bak" >&2
  fi
  printf '%s\n' "$CONTENT" >"$OUT_FILE"
  chmod 600 "$OUT_FILE"
  # 用 ${OUT_FILE} 界定变量边界：其后紧跟全角括号，裸 $OUT_FILE 在 UTF-8 locale
  # 下会把全角字符吞入变量名（bash 视为标识符字符）导致 unbound variable
  echo "[gen-secrets] 已写入 ${OUT_FILE}（权限 600，共 11 项密钥）" >&2
  echo "[gen-secrets] 自检通过：长度达标、未命中弱凭据黑名单、URL/Shell 安全" >&2
else
  printf '%s\n' "$CONTENT"
  echo "" >&2
  echo "[gen-secrets] 自检通过：长度达标、未命中弱凭据黑名单、URL/Shell 安全" >&2
  echo "[gen-secrets] 将上述内容写入 .env.production 即可；或重跑：$0 --out .env.production" >&2
fi
