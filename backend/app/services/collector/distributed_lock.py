"""分布式锁（对齐 TD §12.1 / spec FR-018）。

Redis SET NX EX 原子命令实现采集并发保护。
锁 key = ``collect_lock:{source_id}``，TTL = 600 秒，owner_id = job_id。

设计要点：
- acquire: SET key owner_id NX EX ttl → 原子获取锁
- release: Lua 脚本确保只有锁的 owner 才能释放（防误删）
- is_locked: EXISTS key 检查锁状态
- Redis 不可用时降级为始终获取成功（不影响主流程）
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("unisense.collector.distributed_lock")

# Lua 脚本：仅当 key 的值等于 owner_id 时才删除（原子操作）
_RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


class CollectionLock:
    """采集分布式锁（Redis SET NX EX 实现）。"""

    def __init__(self, redis: Any | None = None) -> None:
        self._redis = redis

    @staticmethod
    def _lock_key(source_id: str) -> str:
        return f"collect_lock:{source_id}"

    async def acquire(
        self, source_id: str, owner_id: str, ttl: int = 600
    ) -> bool:
        """获取分布式锁。

        Args:
            source_id: 数据源标识。
            owner_id: 锁持有者标识（如 job_id）。
            ttl: 锁超时时间（秒），默认 600。

        Returns:
            True 如果成功获取锁；False 如果锁已被占用。
        """
        if self._redis is None:
            # Redis 不可用时降级：始终返回 True（不阻断主流程）
            logger.warning("distributed_lock_redis_unavailable: 降级为无锁模式")
            return True

        try:
            key = self._lock_key(source_id)
            result = await self._redis.set(key, owner_id, nx=True, ex=ttl)
            return result is not None
        except Exception as exc:
            logger.warning("distributed_lock_acquire_failed: %s", exc)
            # Redis 异常时降级：允许执行（避免因 Redis 故障阻断采集）
            return True

    async def release(self, source_id: str, owner_id: str) -> bool:
        """释放分布式锁（仅 owner 可释放）。

        Args:
            source_id: 数据源标识。
            owner_id: 锁持有者标识。

        Returns:
            True 如果成功释放；False 如果锁不属于此 owner。
        """
        if self._redis is None:
            return True

        try:
            key = self._lock_key(source_id)
            result = await self._redis.eval(_RELEASE_SCRIPT, 1, key, owner_id)
            return int(result) == 1
        except Exception as exc:
            logger.warning("distributed_lock_release_failed: %s", exc)
            return True

    async def is_locked(self, source_id: str) -> bool:
        """检查锁是否被占用。

        Args:
            source_id: 数据源标识。

        Returns:
            True 如果锁被占用。
        """
        if self._redis is None:
            return False

        try:
            key = self._lock_key(source_id)
            return bool(await self._redis.exists(key))
        except Exception as exc:
            logger.warning("distributed_lock_check_failed: %s", exc)
            return False
