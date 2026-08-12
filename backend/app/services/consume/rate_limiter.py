"""Redis 滑动窗口限流器 + InMemory 降级（对齐 TD §5.3 / FR-7）。

核心能力：
1. RedisRateLimiter：基于 sorted set + ZADD/ZREMRANGEBYSCORE/ZCARD 原子操作的滑动窗口
2. InMemoryRateLimiter：进程内令牌桶 + 日配额计数（Redis 不可用时降级）
3. 自动降级：Redis 连接失败时自动切换到 InMemory 并记录告警日志
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import redis.asyncio as aioredis

from app.core.logging import get_logger

logger = get_logger(__name__)


class InMemoryRateLimiter:
    """进程内令牌桶 + 日配额计数（降级方案，对齐 TD §5.3）。

    仅在 Redis 不可用时使用，全局限流失效，仅进程级限流。
    """

    def __init__(self) -> None:
        self._buckets: dict[str, list[float]] = {}
        self._daily: dict[str, tuple[str, int]] = {}

    def allow(self, key: str, qps: int) -> bool:
        """QPS 限流（滑动窗口，1秒内请求数）。"""
        now = time.monotonic()
        window = self._buckets.setdefault(key, [])
        window[:] = [t for t in window if now - t < 1.0]
        if len(window) >= qps:
            return False
        window.append(now)
        return True

    def allow_daily(self, key: str, quota: int, today: str) -> bool:
        """日配额闸门：按自然日计数，跨日自动重置。"""
        day, used = self._daily.get(key, (today, 0))
        if day != today:
            day, used = today, 0
        if used >= quota:
            self._daily[key] = (day, used)
            return False
        self._daily[key] = (day, used + 1)
        return True


class RedisRateLimiter:
    """Redis 滑动窗口限流器（对齐 TD §5.3 / FR-7）。

    使用 Redis sorted set + ZADD/ZREMRANGEBYSCORE/ZCARD 原子操作实现滑动窗口。
    支持 QPS 限流和日配额限流双维度。
    Redis 不可用时自动降级为 InMemoryRateLimiter 并记录告警日志。
    """

    def __init__(self, redis: aioredis.Redis | None = None) -> None:
        self._redis = redis
        self._fallback = InMemoryRateLimiter()

    async def allow(self, key: str, qps: int) -> bool:
        """QPS 滑动窗口限流。

        使用 Redis sorted set：
        1. ZREMRANGEBYSCORE 清除 1 秒前的记录
        2. ZCARD 获取当前窗口内请求数
        3. ZADD 添加当前请求
        4. 超过 qps 则拒绝

        Args:
            key: 限流键（如 client_id）。
            qps: 每秒允许的最大请求数。

        Returns:
            True 允许，False 拒绝。
        """
        if self._redis is None:
            logger.warning("rate_limiter.redis_unavailable_fallback", key=key)
            return self._fallback.allow(key, qps)

        try:
            now = time.time()
            window_key = f"unisense:rate:{key}"

            # Pipeline 保证原子性
            async with self._redis.pipeline(transaction=True) as pipe:
                # 1. 清除 1 秒前的记录
                pipe.zremrangebyscore(window_key, 0, now - 1.0)
                # 2. 获取当前窗口内请求数
                pipe.zcard(window_key)
                # 3. 添加当前请求
                pipe.zadd(window_key, {str(now): now})
                # 4. 设置过期时间（防止内存泄漏）
                pipe.expire(window_key, 2)

                results = await pipe.execute()

            current_count = results[1]
            return not current_count >= qps

        except Exception:
            logger.warning(
                "rate_limiter.redis_error_fallback",
                key=key,
                exc_info=True,
            )
            return self._fallback.allow(key, qps)

    async def allow_daily(self, key: str, quota: int, today: str | None = None) -> bool:
        """日配额限流。

        使用 Redis INCR + EXPIRE 实现原子计数。

        Args:
            key: 限流键（如 client_id）。
            quota: 日配额上限。
            today: 当前日期字符串（YYYY-MM-DD），None 自动获取。

        Returns:
            True 允许，False 拒绝。
        """
        if today is None:
            today = datetime.now(tz=UTC).strftime("%Y-%m-%d")

        if self._redis is None:
            logger.warning("rate_limiter.redis_unavailable_fallback_daily", key=key)
            return self._fallback.allow_daily(key, quota, today)

        try:
            daily_key = f"unisense:rate:daily:{key}:{today}"

            # INCR + EXPIRE 原子操作
            current = await self._redis.incr(daily_key)
            if current == 1:
                # 首次写入，设置过期时间（2 天后自动清理）
                await self._redis.expire(daily_key, 172800)

            return not current > quota

        except Exception:
            logger.warning(
                "rate_limiter.redis_error_fallback_daily",
                key=key,
                exc_info=True,
            )
            return self._fallback.allow_daily(key, quota, today)


# 模块级限流器（lifespan 中替换为 Redis 版本）
_rate_limiter: RedisRateLimiter | InMemoryRateLimiter = InMemoryRateLimiter()


def get_rate_limiter() -> RedisRateLimiter | InMemoryRateLimiter:
    """获取当前限流器实例。"""
    return _rate_limiter


def init_rate_limiter(redis: aioredis.Redis | None) -> None:
    """初始化限流器（lifespan 中调用）。

    Args:
        redis: Redis 连接池，None 时使用 InMemory 降级。
    """
    global _rate_limiter
    if redis is not None:
        _rate_limiter = RedisRateLimiter(redis)
        logger.info("rate_limiter.initialized", type="redis")
    else:
        _rate_limiter = InMemoryRateLimiter()
        logger.warning("rate_limiter.initialized", type="inmemory_fallback")
