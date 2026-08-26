"""通用 cron 任务分布式锁（P11 C-1：防多 worker 副本/超周期重入双跑）。

arq 的 ``cron`` 无防重入：单 worker 内任务超周期即可并发重跑，多 worker 副本
同点双跑。对写副作用敏感的任务（审计归档/保留清理/升级/通知清理/质量巡检/
健康度刷新/血缘扫描）用本锁排他。

设计（对齐 collector 的 ``CollectionLock`` 哲学）：
- key = ``task_lock:{name}``，owner = 随机 UUID，TTL 默认 1h
- 仅 owner 可释放（Lua 脚本防误删）
- Redis 不可用/异常时**降级为获取成功**（不因锁故障阻断任务主流程）
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

logger = structlog.get_logger("unisense.tasks.lock")

_RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


class TaskLock:
    """任务级排他锁（Redis SET NX EX）。

    Usage::

        async with TaskLock(ctx.get("redis"), "audit-archive") as lock:
            if not lock.acquired:
                return {"status": "skipped", "reason": "locked"}
            ...  # 任务体
    """

    def __init__(self, redis: Any | None, name: str, ttl: int = 3600) -> None:
        self._redis = redis
        self._key = f"task_lock:{name}"
        self._owner = uuid.uuid4().hex
        self._ttl = ttl
        self.acquired = False

    async def acquire(self) -> bool:
        if self._redis is None:
            # Redis 不可用降级：视作获取成功（不阻断任务主流程），须同步置位 acquired。
            logger.warning("task_lock_redis_unavailable", name=self._key)
            self.acquired = True
            return True
        try:
            res = await self._redis.set(self._key, self._owner, nx=True, ex=self._ttl)
            self.acquired = res is not None
            return self.acquired
        except Exception as exc:  # noqa: BLE001 - 锁故障不阻断主流程
            logger.warning("task_lock_acquire_failed", name=self._key, error=str(exc))
            return True

    async def release(self) -> None:
        if self._redis is None or not self.acquired:
            return
        try:
            await self._redis.eval(_RELEASE_SCRIPT, 1, self._key, self._owner)
        except Exception as exc:  # noqa: BLE001 - 释放失败靠 TTL 兜底
            logger.warning("task_lock_release_failed", name=self._key, error=str(exc))
        self.acquired = False

    async def __aenter__(self) -> TaskLock:
        await self.acquire()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.release()


def task_locked(name: str, ttl: int = 3600):
    """arq cron 任务排他锁装饰器（P11 C-1：防多副本/重入双跑）。

    包裹 ``async def task(ctx, ...)``：以 ``ctx.get("redis")`` 获取分布式锁，
    未获得锁（其它副本/上一次超周期仍在跑）时返回 ``{"status": "SKIPPED",
    "reason": "locked"}``，不执行任务体。锁在任务结束/异常时自动释放。

    Usage::

        @task_locked("audit-archive")
        async def audit_archive_task(ctx: dict[str, Any]) -> dict[str, Any]:
            ...
    """

    from functools import wraps

    def deco(fn):
        @wraps(fn)
        async def wrapper(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            redis = ctx.get("redis") if isinstance(ctx, dict) else None
            async with TaskLock(redis, name, ttl=ttl) as lock:
                if not lock.acquired:
                    logger.info("task_skipped_locked", name=name)
                    return {"status": "SKIPPED", "reason": "locked"}
                return await fn(ctx, *args, **kwargs)

        return wrapper

    return deco
