"""Redis 客户端管理。

对齐 DEV_GUIDE §17.2（缓存策略）。
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.core.config import settings

redis_client: aioredis.Redis = aioredis.from_url(  # type: ignore[no-untyped-call]
    settings.redis_url,
    decode_responses=True,
    max_connections=20,
)


async def get_redis() -> aioredis.Redis:
    """获取 Redis 客户端。

    Returns:
        Redis 异步客户端。
    """
    return redis_client
