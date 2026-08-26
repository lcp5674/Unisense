"""安全工具：密码哈希与 JWT 签发。

对齐 TD §5（鉴权）与 §12.4（密钥配置）。
- 密码哈希使用 bcrypt（存储于 user.password_hash）。
- JWT 使用 HS256，密钥取自 settings.jwt_secret。
- JWT 包含 jti(UUID4) 字段，支持令牌撤销（黑名单）。
- 黑名单使用 Redis SET + TTL，Redis 不可用时降级到进程内存。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("unisense.security")

#: refresh token 有效期（秒），与 create_refresh_token 的 7 天保持一致，
#: 同时作为单端登录「活跃 refresh jti」映射的 TTL。
REFRESH_TOKEN_TTL_SECONDS = 7 * 24 * 3600

_memory_blacklist: dict[str, float] = {}

#: 单端登录降级存储：user_id -> (当前活跃 refresh jti, 过期时间戳)。
#: Redis 不可用时退化为进程内存（多 worker 下仅本进程生效，与 _memory_blacklist 同语义）。
_memory_active_refresh: dict[int, tuple[str, float]] = {}


async def hash_password(password: str) -> str:
    """对明文密码进行 bcrypt 哈希（异步，不阻塞事件循环）。"""
    result = await asyncio.to_thread(bcrypt.hashpw, password.encode("utf-8"), bcrypt.gensalt())
    return result.decode("utf-8")


async def verify_password(password: str, hashed: str) -> bool:
    """校验明文密码与 bcrypt 哈希是否匹配。

    bcrypt 为 CPU 密集型计算，若直接在 async 路由中同步调用会阻塞整个事件循环，
    导致单实例吞吐量被锁死（perf 实测 10 VU 仅 ~5 req/s）。改为在线程池中执行，
    释放事件循环以并发处理其他请求。

    Args:
        password: 明文密码。
        hashed: 存储的 bcrypt 哈希。

    Returns:
        匹配返回 True，否则 False（含非法哈希格式）。
    """
    try:
        return await asyncio.to_thread(
            bcrypt.checkpw, password.encode("utf-8"), hashed.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


def create_access_token(
    *,
    sub: int | str,
    role: str,
    org_id: int,
    expire_minutes: int | None = None,
) -> str:
    """签发 JWT 访问令牌。

    Args:
        sub: 用户 ID（subject）。
        role: 用户角色。
        org_id: 所属组织 ID。
        expire_minutes: 过期分钟数，缺省取 settings.jwt_expire_minutes。

    Returns:
        签名后的 JWT 字符串。
    """
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=expire_minutes or settings.jwt_expire_minutes)
    payload = {
        "sub": str(sub),
        "role": role,
        "org_id": org_id,
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(*, sub: int | str, role: str, org_id: int) -> str:
    """签发 JWT 刷新令牌（7天有效期）。"""
    now = datetime.now(UTC)
    expire = now + timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS)
    payload = {
        "sub": str(sub),
        "role": role,
        "org_id": org_id,
        "type": "refresh",
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


async def blacklist_token(jti: str, ttl_seconds: int) -> None:
    """将 JWT jti 加入黑名单。

    优先使用 Redis SET（key=unisense:jwt_blacklist:{jti}，value="1"，TTL=ttl_seconds）；
    Redis 不可用时降级到进程内 _memory_blacklist 字典。

    Args:
        jti: JWT 唯一标识符。
        ttl_seconds: 黑名单保留秒数（建议 = 剩余令牌有效期）。
    """
    redis_key = f"unisense:jwt_blacklist:{jti}"
    try:
        from app.db.redis import get_redis

        redis_client = get_redis()
        await redis_client.set(redis_key, "1", ex=ttl_seconds)
        logger.info("jwt_blacklisted_redis", jti=jti, ttl=ttl_seconds)
    except Exception:
        expiry_ts = time.time() + ttl_seconds
        _memory_blacklist[jti] = expiry_ts
        logger.warning("jwt_blacklisted_memory_fallback", jti=jti, ttl=ttl_seconds)


async def is_token_blacklisted(jti: str) -> bool:
    """检查 JWT jti 是否在黑名单中。

    Args:
        jti: JWT 唯一标识符。

    Returns:
        True 表示已撤销，False 表示有效。
    """
    redis_key = f"unisense:jwt_blacklist:{jti}"
    try:
        from app.db.redis import get_redis

        redis_client = get_redis()
        result = await redis_client.get(redis_key)
        if result is not None:
            return True
    except Exception:
        pass

    now = time.time()
    expired_jtis = [k for k, v in _memory_blacklist.items() if v <= now]
    for k in expired_jtis:
        _memory_blacklist.pop(k, None)

    return jti in _memory_blacklist


async def _get_active_refresh_memory(user_id: int) -> tuple[str, int] | None:
    """读取用户当前活跃 refresh (jti, exp_ts)——仅进程内存降级路径。"""
    entry = _memory_active_refresh.get(user_id)
    if entry is None:
        return None
    jti, exp_ts = entry
    if exp_ts > time.time():
        return jti, int(exp_ts)
    _memory_active_refresh.pop(user_id, None)
    return None


async def revoke_active_refresh(user_id: int) -> None:
    """登出时吊销该用户当前活跃 refresh token（TD §5 单端登录的对称操作）。

    读取 ``unisense:active_refresh:{user_id}`` 的活跃 refresh jti，将其加入黑名单
    （TTL = 剩余有效期）并清除活跃映射——登出后该用户的 refresh token 立即失效，
    无法再续期 access token。此前登出仅拉黑 access token 的 jti，被劫持的
    refresh 在 7 天有效期内可无限续期（会话吊销缺口）；本函数补上吊销闭环。

    Redis 不可用时降级进程内存（与 ``rotate_active_refresh`` 同语义，多 worker 下
    仅本进程生效，生产须 Redis）。
    """
    now = int(time.time())
    redis_key = f"unisense:active_refresh:{user_id}"
    try:
        from app.db.redis import get_redis

        redis_client = get_redis()
        val = await redis_client.get(redis_key)
        if val:
            parts = str(val).split(":", 1)
            old_jti = parts[0]
            old_exp = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            if old_jti:
                ttl = (old_exp - now) if old_exp > 0 else REFRESH_TOKEN_TTL_SECONDS
                if ttl > 0:
                    await blacklist_token(old_jti, ttl)
            await redis_client.delete(redis_key)
            logger.info("refresh_revoked_redis", user_id=user_id, jti=old_jti[:12])
        return
    except Exception:
        _warn_memory_fallback_once(user_id)

    # 内存降级（单进程原子；多 worker 下仅本进程生效，生产须 Redis）
    old = await _get_active_refresh_memory(user_id)
    if old is not None:
        old_jti, old_exp_ts = old
        old_ttl = (old_exp_ts - now) if old_exp_ts > 0 else REFRESH_TOKEN_TTL_SECONDS
        if old_ttl > 0:
            await blacklist_token(old_jti, old_ttl)
        _memory_active_refresh.pop(user_id, None)
        logger.info("refresh_revoked_memory", user_id=user_id)


#: 单端登录原子轮换 Lua 脚本。Redis 单线程保证「读旧→拉黑旧→写新」原子性，
#: 消除并发双登录下旧会话未被拉黑的竞态（TD §5）。旧 jti 拉黑 TTL = 其剩余有效期，
#: 仅旧格式（无 exp）回退默认 TTL。
_ROTATE_ACTIVE_REFRESH_LUA = """
local key = KEYS[1]
local new_jti = ARGV[1]
local new_exp = tonumber(ARGV[2])
local blacklist_prefix = ARGV[3]
local now = tonumber(ARGV[4])
local default_ttl = tonumber(ARGV[5])

local old_val = redis.call('GET', key)
if old_val then
  local old_jti = old_val
  local old_exp = 0
  local sep = string.find(old_val, ':', 1, true)
  if sep then
    old_jti = string.sub(old_val, 1, sep - 1)
    old_exp = tonumber(string.sub(old_val, sep + 1)) or 0
  end
  if old_jti ~= new_jti then
    local ttl = 0
    if old_exp > 0 then
      ttl = old_exp - now
    else
      ttl = default_ttl
    end
    if ttl > 0 then
      redis.call('SET', blacklist_prefix .. old_jti, '1', 'EX', math.floor(ttl))
    end
  end
end

local active_ttl = new_exp - now
if active_ttl < 1 then active_ttl = 1 end
redis.call('SET', key, new_jti .. ':' .. new_exp, 'EX', math.floor(active_ttl))
return 1
"""

#: 内存降级告警限频窗口（秒）：同一 user 在窗口内只告警一次，避免多登录刷日志。
_MEM_FALLBACK_WARN_WINDOW = 60.0
_last_mem_fallback_warn: dict[int, float] = {}


def _warn_memory_fallback_once(user_id: int) -> None:
    """记录单端登录内存降级告警（限频），提示多 worker 下互踢仅本进程生效。"""
    now = time.time()
    if now - _last_mem_fallback_warn.get(user_id, 0.0) < _MEM_FALLBACK_WARN_WINDOW:
        return
    _last_mem_fallback_warn[user_id] = now
    logger.warning(
        "single_login_memory_fallback",
        user_id=user_id,
        detail="单端登录降级为进程内存（多 worker 下仅本进程生效）；生产环境请确保 Redis 可用",
    )


async def rotate_active_refresh(
    user_id: int,
    refresh_token: str,
    ttl_seconds: int = REFRESH_TOKEN_TTL_SECONDS,
) -> None:
    """单端登录（TD §5）：新签发的 refresh token 使该用户旧会话失效。

    记录该用户当前有效的 refresh jti（含过期时间戳）；签发新 refresh token
    （登录/刷新轮换）时，把旧 jti 加入黑名单（TTL = 旧 refresh 剩余有效期）——
    旧会话的 refresh 即被吊销，其 access token 短效（jwt_expire_minutes，
    默认 15 分钟）过期后无法无感续期，即被踢下线。

    原子性：Redis 路径经 Lua 脚本原子执行「读旧→拉黑旧→写新」，并发双登录下
    后执行的登录必然拉黑先执行的 jti，杜绝「两个新会话都未被拉黑」的竞态。
    内存降级路径为 best-effort：单进程 + Redis 连接池未初始化（get_redis 同步抛错）
    时无真实 await 让出、天然原子；若池已建但 Redis 不可达，blacklist_token 内
    await 会让出事件循环，极端并发下存在读-改-写理论窗口——该场景属故障降级，
    以限频告警提示（``_warn_memory_fallback_once``），生产多 worker 必须依赖 Redis
    保证跨进程互踢。

    语义：同账号同时只能有一处活跃会话；旧会话最长存活 access token 的
    剩余有效期（短效容忍窗口），避免极端并发下自身刷新被误踢。

    Args:
        user_id: 用户 ID。
        refresh_token: 新签发的 refresh token（解码取其 jti/exp）。
        ttl_seconds: 活跃映射保留秒数（= refresh 有效期），旧格式无 exp 时兜底。
    """
    try:
        payload = jwt.decode(
            refresh_token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        new_jti = str(payload.get("jti", ""))
        new_exp = int(payload.get("exp", 0))
    except jwt.InvalidTokenError:
        return
    if not new_jti:
        return

    now = int(time.time())
    redis_key = f"unisense:active_refresh:{user_id}"
    try:
        from app.db.redis import get_redis

        redis_client = get_redis()
        # redis.asyncio 的 eval 类型注解为 Awaitable[str] | str（装饰器推断缺陷），
        # 实际恒为协程；mypy misc 精确豁免，与 db/redis.py 的 ignore 风格一致。
        await redis_client.eval(  # type: ignore[misc]
            _ROTATE_ACTIVE_REFRESH_LUA,
            1,
            redis_key,
            new_jti,
            str(new_exp),
            "unisense:jwt_blacklist:",
            str(now),
            str(ttl_seconds),
        )
        return
    except Exception:
        _warn_memory_fallback_once(user_id)

    # 内存降级（单进程原子；多 worker 下仅本进程生效，生产须 Redis）
    old = await _get_active_refresh_memory(user_id)
    if old is not None:
        old_jti, old_exp_ts = old
        if old_jti != new_jti:
            old_ttl = (old_exp_ts - now) if old_exp_ts > 0 else ttl_seconds
            if old_ttl > 0:
                await blacklist_token(old_jti, old_ttl)
    _memory_active_refresh[user_id] = (new_jti, new_exp or now + ttl_seconds)
