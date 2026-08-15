"""描述推断 in-flight 去重（TD §12.1 / FR-023）。

LLM 推断是慢操作（数十秒）。用户退出页面再进入、或并发点击，会对同一字段/表
发起重复推断请求，造成 LLM 调用浪费与并发写覆盖。本模块提供推断进行中标记：

- Redis 可用：``SET key owner NX EX``（TTL=120s）原子去重，跨进程生效；
  release 用 Lua 脚本仅释放 owner 自己的锁（防误删）。
- Redis 不可用：降级为进程内 dict（模块级共享，TTL 自动过期），单进程内去重。

设计对齐采集锁 CollectionLock（``distributed_lock.py``），构造时传入 redis 实例
（API 层用 ``contextlib.suppress(RuntimeError)`` 包裹 ``get_redis()`` 降级）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("unisense.collector.infer_guard")

# 默认锁 TTL：LLM 推断最长约 60s，留足余量防异常路径残留
DEFAULT_TTL = 120

# Redis 不可用时的进程内降级表：key -> (owner_id, expires_at)
# 模块级（跨请求共享）：API 层每次请求新建 CollectorService，不能放实例上。
_inflight_local: dict[str, tuple[str, float]] = {}

# Lua 脚本：仅当 key 的值等于 owner_id 时才删除（原子操作）
_RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


class InferInflightGuard:
    """描述推断进行中去重（Redis SET NX EX + 进程内降级）。"""

    def __init__(self, redis: Any | None = None) -> None:
        self._redis = redis

    @staticmethod
    def _key(kind: str, entity_id: int | str, column: str | None = None) -> str:
        """锁 key：kind 区分 column/batch/table/metric，column 为 None 时以 * 表示整表/整批。"""
        return f"infer_inflight:{kind}:{entity_id}:{column or '*'}"

    async def acquire(
        self,
        kind: str,
        entity_id: int | str,
        column: str | None = None,
        *,
        owner: str | None = None,
        ttl: int = DEFAULT_TTL,
    ) -> bool:
        """尝试获取推断权。

        Args:
            kind: 推断类型（column/batch/table/metric）。
            entity_id: 目录实体 ID（int）或指标编码（str）。
            column: 字段名（仅 kind=column 时使用）。
            owner: 持有者标识（API 层传 api-{user}-{uuid}）。
            ttl: 锁超时时间（秒）。

        Returns:
            True 表示本请求获得推断权；False 表示已有推断进行中。
        """
        key = self._key(kind, entity_id, column)
        owner_id = owner or "default"
        if self._redis is not None:
            try:
                result = await self._redis.set(key, owner_id, nx=True, ex=ttl)
                return result is not None
            except Exception as exc:
                logger.warning("infer_guard_redis_acquire_failed: %s", exc)
                # Redis 异常降级进程内（不阻断推断主流程）
        return self._acquire_local(key, owner_id, ttl)

    def _acquire_local(self, key: str, owner_id: str, ttl: int) -> bool:
        now = time.monotonic()
        existing = _inflight_local.get(key)
        if existing is not None and existing[1] > now:
            return False
        _inflight_local[key] = (owner_id, now + ttl)
        return True

    async def release(
        self,
        kind: str,
        entity_id: int | str,
        column: str | None = None,
        *,
        owner: str | None = None,
    ) -> bool:
        """释放推断权（仅 owner 可释放；Redis 用 Lua，进程内比对 owner）。"""
        key = self._key(kind, entity_id, column)
        owner_id = owner or "default"
        if self._redis is not None:
            try:
                result = await self._redis.eval(_RELEASE_SCRIPT, 1, key, owner_id)
                return int(result) == 1
            except Exception as exc:
                logger.warning("infer_guard_redis_release_failed: %s", exc)
                return True
        return self._release_local(key, owner_id)

    def _release_local(self, key: str, owner_id: str) -> bool:
        existing = _inflight_local.get(key)
        if existing is not None and existing[0] == owner_id:
            del _inflight_local[key]
            return True
        return False

    async def is_locked(
        self,
        kind: str,
        entity_id: int | str,
        column: str | None = None,
    ) -> bool:
        """检查推断是否进行中（供前端/可观测性使用）。"""
        key = self._key(kind, entity_id, column)
        if self._redis is not None:
            try:
                return bool(await self._redis.exists(key))
            except Exception as exc:
                logger.warning("infer_guard_redis_check_failed: %s", exc)
                return False
        existing = _inflight_local.get(key)
        return existing is not None and existing[1] > time.monotonic()
