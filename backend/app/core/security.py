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


async def _get_active_refresh(user_id: int) -> str | None:
    """读取用户当前活跃的 refresh token jti（Redis 优先，内存降级）。"""
    redis_key = f"unisense:active_refresh:{user_id}"
    try:
        from app.db.redis import get_redis

        redis_client = get_redis()
        val = await redis_client.get(redis_key)
        if val is not None:
            return str(val)
    except Exception:
        pass

    entry = _memory_active_refresh.get(user_id)
    if entry is not None:
        jti, expiry_ts = entry
        if expiry_ts > time.time():
            return jti
        _memory_active_refresh.pop(user_id, None)
    return None


async def _set_active_refresh(user_id: int, jti: str, ttl_seconds: int) -> None:
    """记录用户当前活跃的 refresh token jti（Redis 优先，内存降级）。"""
    redis_key = f"unisense:active_refresh:{user_id}"
    try:
        from app.db.redis import get_redis

        redis_client = get_redis()
        await redis_client.set(redis_key, jti, ex=ttl_seconds)
        return
    except Exception:
        pass

    _memory_active_refresh[user_id] = (jti, time.time() + ttl_seconds)


async def rotate_active_refresh(
    user_id: int,
    refresh_token: str,
    ttl_seconds: int = REFRESH_TOKEN_TTL_SECONDS,
) -> None:
    """单端登录（TD §5）：新签发的 refresh token 使该用户旧会话失效。

    记录该用户当前有效的 refresh jti；签发新 refresh token（登录/刷新轮换）时，
    把旧 jti 加入黑名单——旧会话的 refresh 即被吊销，其 access token 短效
    （jwt_expire_minutes，默认 15 分钟）过期后无法无感续期，即被踢下线。

    语义：同账号同时只能有一处活跃会话；旧会话最长存活 access token 的
    剩余有效期（短效容忍窗口），避免极端并发下自身刷新被误踢。

    Args:
        user_id: 用户 ID。
        refresh_token: 新签发的 refresh token（解码取其 jti）。
        ttl_seconds: 活跃映射保留秒数（= refresh 有效期）。
    """
    try:
        payload = jwt.decode(
            refresh_token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        new_jti = str(payload.get("jti", ""))
    except jwt.InvalidTokenError:
        return
    if not new_jti:
        return

    old_jti = await _get_active_refresh(user_id)
    if old_jti and old_jti != new_jti:
        await blacklist_token(old_jti, ttl_seconds)
    await _set_active_refresh(user_id, new_jti, ttl_seconds)
