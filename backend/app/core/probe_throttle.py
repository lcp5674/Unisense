"""探活/枚举类操作限流（防 SSRF 端口扫描滥用，对齐 TD §13 安全）。

对 ``test-connection`` / ``list-databases`` / ``list-tables`` 这类接受
任意连接配置并发起真实网络连接的端点做固定窗口限流——即便 host 校验已挡住
内网目标，限流仍可遏制攻击者用大量合法 host 做端口/服务指纹扫描。

存储策略与 ``app/core/login_throttle.py`` 一致：优先 Redis（INCR+EXPIRE
原子操作），Redis 不可用时降级进程内存计数——限流是防滥用手段，绝不阻断
主流程（降级时仅尽力而为）。

函数为纯逻辑（不依赖请求上下文），便于单测注入时钟/存储。
"""

from __future__ import annotations

import time

from app.core.logging import get_logger

logger = get_logger("unisense.probe_throttle")

#: 固定窗口内允许的最大探活次数与窗口时长（秒）。
MAX_PROBES = 15
WINDOW_SECONDS = 60

#: 进程内存降级存储：key -> (次数, 窗口截止时间戳)。
_memory: dict[str, tuple[int, float]] = {}


def _redis_key(key: str) -> str:
    return f"unisense:probe:{key}"


def _memory_count(key: str, *, now: float) -> int:
    """返回当前窗口内探活次数；窗口过期则重置并清除记录。"""
    entry = _memory.get(key)
    if entry is None:
        return 0
    count, expires_at = entry
    if now >= expires_at:
        _memory.pop(key, None)
        return 0
    return count


def _memory_bump(key: str, *, now: float) -> int:
    """内存窗口计数 +1，返回新计数（窗口过期时重置）。"""
    count = _memory_count(key, now=now)
    count += 1
    _memory[key] = (count, now + WINDOW_SECONDS)
    return count


async def check_probe_rate(key: str, *, max_probes: int = MAX_PROBES) -> None:
    """校验 key（用户维度）当前窗口探活次数是否超限。

    Args:
        key: 限流键（如 ``f"user:{user.id}"``）。
        max_probes: 窗口内允许的最大探活次数。

    Raises:
        BusinessError: 超限（error_code=PROBE_RATE_LIMITED）。
    """
    from app.core.exceptions import BusinessError
    from app.db.redis import get_redis

    try:
        redis = get_redis()
        pipe = redis.pipeline()
        pipe.incr(_redis_key(key))
        pipe.expire(_redis_key(key), WINDOW_SECONDS)
        count, _ = await pipe.execute()
        if int(count) > max_probes:
            raise BusinessError(
                "探活请求过于频繁，请稍后再试",
                error_code="PROBE_RATE_LIMITED",
            )
    except BusinessError:
        raise
    except Exception:
        # Redis 不可用：降级内存计数（best-effort，不阻断主流程）
        count = _memory_bump(key, now=time.time())
        if count > max_probes:
            raise BusinessError(
                "探活请求过于频繁，请稍后再试",
                error_code="PROBE_RATE_LIMITED",
            ) from None
