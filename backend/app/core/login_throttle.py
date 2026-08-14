"""登录失败限流（TD §5 鉴权：防撞库/暴力破解）。

固定窗口计数：以 ``username+IP`` 为 key，15 分钟窗口内最多允许 10 次登录失败，
超出后由调用方（auth.login）返回 429。

存储策略与 ``app/core/security.py`` 黑名单一致：优先 Redis（INCR+EXPIRE 原子操作），
Redis 不可用时降级进程内存计数——限流是防滥用手段，绝不阻断登录主流程。

函数为纯逻辑（不依赖请求上下文），便于单测注入时钟/存储。
"""

from __future__ import annotations

import time

from app.core.logging import get_logger

logger = get_logger("unisense.login_throttle")

#: 窗口内允许的最大失败次数与窗口时长（秒）。
MAX_FAILURES = 10
WINDOW_SECONDS = 15 * 60

#: 进程内存降级存储：key -> (失败次数, 窗口截止时间戳)。
_memory: dict[str, tuple[int, float]] = {}


def _redis_key(key: str) -> str:
    return f"unisense:login_fail:{key}"


def _memory_count(key: str, *, now: float) -> int:
    """返回当前窗口内失败次数；窗口过期则重置并清除记录。"""
    entry = _memory.get(key)
    if entry is None:
        return 0
    count, expires_at = entry
    if now >= expires_at:
        _memory.pop(key, None)
        return 0
    return count


async def is_login_blocked(key: str, *, max_failures: int = MAX_FAILURES) -> bool:
    """key（username+IP）当前失败次数是否已达上限。

    Args:
        key: 限流键。
        max_failures: 窗口内失败上限。

    Returns:
        True 表示应拒绝本次登录（429）；Redis 不可用时回落到内存计数。
    """
    try:
        from app.db.redis import get_redis

        value = await get_redis().get(_redis_key(key))
        return int(value or 0) >= max_failures
    except Exception:
        return _memory_count(key, now=time.time()) >= max_failures


async def record_login_failure(key: str, *, window_seconds: int = WINDOW_SECONDS) -> None:
    """记录一次登录失败（首次失败起计固定窗口）。

    Args:
        key: 限流键。
        window_seconds: 固定窗口时长（秒）。
    """
    rkey = _redis_key(key)
    try:
        from app.db.redis import get_redis

        redis_client = get_redis()
        current = await redis_client.incr(rkey)
        if current == 1:
            await redis_client.expire(rkey, window_seconds)
    except Exception:
        now = time.time()
        _memory[key] = (_memory_count(key, now=now) + 1, now + window_seconds)


async def reset_login_failures(key: str) -> None:
    """登录成功时清除失败计数，避免历史失败拖累正常登录。"""
    try:
        from app.db.redis import get_redis

        await get_redis().delete(_redis_key(key))
    except Exception:
        _memory.pop(key, None)
