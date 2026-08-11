"""Redis 客户端管理。

对齐 DEV_GUIDE §17.2（缓存策略）。
连接池由 lifespan 管理（启动创建、关闭释放），
提供 get_redis_pool() 依赖注入替代模块级单例。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import redis.asyncio as aioredis

from app.core.config import settings

# 模块级连接池（由 lifespan 初始化/关闭，不再在导入期创建）
_redis_pool: aioredis.Redis | None = None


def create_redis_pool() -> aioredis.Redis:
    """创建 Redis 连接池。

    Returns:
        Redis 异步客户端。
    """
    return aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        max_connections=20,
    )


async def init_redis_pool() -> aioredis.Redis:
    """初始化 Redis 连接池（lifespan 中调用）。

    Returns:
        初始化后的 Redis 客户端。
    """
    global _redis_pool
    _redis_pool = create_redis_pool()
    return _redis_pool


async def close_redis_pool() -> None:
    """关闭 Redis 连接池（lifespan 中调用）。"""
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.aclose()
        _redis_pool = None


async def get_redis_pool() -> AsyncGenerator[aioredis.Redis, None]:
    """FastAPI 依赖：提供 Redis 连接池。

    Yields:
        Redis 异步客户端。
    """
    if _redis_pool is None:
        raise RuntimeError("Redis 连接池未初始化，请在 lifespan 中调用 init_redis_pool()")
    yield _redis_pool


def get_redis() -> aioredis.Redis:
    """获取 Redis 客户端（非依赖注入场景使用）。

    Returns:
        Redis 异步客户端。
    """
    if _redis_pool is None:
        raise RuntimeError("Redis 连接池未初始化，请在 lifespan 中调用 init_redis_pool()")
    return _redis_pool
