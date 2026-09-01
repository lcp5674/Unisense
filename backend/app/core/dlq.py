"""死信队列模块（TECH-04: 事件总线指数退避 + 死信队列）。

职责：
1. 存储事件总线重试耗尽的事件
2. 定时重放死信事件
3. 管理接口查询死信状态

内存实现（事件量不大），对齐 R&D-04。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger

logger = get_logger("unisense.dlq")

# 死信队列最大容量（防止无界增长）
_DLQ_MAX_SIZE = 10_000

# 重放间隔（秒）
_REPLAY_INTERVAL = 300  # 5 分钟

# 单次重放上限
_REPLAY_BATCH_SIZE = 10


class DeadLetterEvent:
    """死信事件（内存模型）。"""

    __slots__ = (
        "event_type",
        "payload",
        "failure_reason",
        "retry_count",
        "created_at",
        "last_retry_at",
        "status",
    )

    def __init__(
        self,
        event_type: str,
        payload: dict[str, Any],
        failure_reason: str,
        retry_count: int = 0,
    ) -> None:
        self.event_type = event_type
        self.payload = payload
        self.failure_reason = failure_reason
        self.retry_count = retry_count
        self.created_at = datetime.now(UTC)
        self.last_retry_at: datetime | None = None
        self.status: str = "PENDING"  # PENDING / RETRIED / EXHAUSTED

    def to_dict(self) -> dict[str, Any]:
        """序列化（Redis 持久化用）。"""
        return {
            "event_type": self.event_type,
            "payload": self.payload,
            "failure_reason": self.failure_reason,
            "retry_count": self.retry_count,
            "created_at": self.created_at.isoformat(),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeadLetterEvent":
        """反序列化（Redis 恢复用）。"""
        ev = cls(
            event_type=str(data.get("event_type", "")),
            payload=data.get("payload") or {},
            failure_reason=str(data.get("failure_reason", "")),
            retry_count=int(data.get("retry_count", 0)),
        )
        created = data.get("created_at")
        if created:
            try:
                ev.created_at = datetime.fromisoformat(str(created))
            except ValueError:
                pass
        ev.status = str(data.get("status", "PENDING"))
        return ev


class DeadLetterQueue:
    """内存死信队列（单实例，进程内）。"""

    _REDIS_KEY = "unisense:dlq"

    def __init__(self, max_size: int = _DLQ_MAX_SIZE) -> None:
        self._queue: deque[DeadLetterEvent] = deque(maxlen=max_size)
        self._replay_task: asyncio.Task[None] | None = None
        # R5（审查修复）：Redis 持久化兜底——进程重启（滚动发布）不丢死信；
        # 内存 deque 打满时告警（此前静默丢弃最老且 queue_size 恒为容量值掩盖积压）。
        self._dropped_count = 0
        self._max_size = max_size

    def _persist_to_redis(self, event: DeadLetterEvent) -> None:
        """死信写入 Redis LIST（best-effort：Redis 不可用仅内存 + 告警）。"""
        try:
            from app.db.redis import get_redis

            import redis.asyncio as aioredis

            redis_client = get_redis()
            if isinstance(redis_client, aioredis.Redis):
                # 同步方法内不能 await——投递到事件循环由调用方场景异步执行不可行，
                # 故此处用 loop.create_task 后台写（best-effort）。
                import asyncio as _aio

                try:
                    loop = _aio.get_running_loop()
                    loop.create_task(
                        redis_client.lpush(
                            self._REDIS_KEY, json.dumps(event.to_dict(), ensure_ascii=False)
                        )
                    )
                except RuntimeError:
                    pass
        except Exception:  # noqa: BLE001
            logger.warning("dlq_redis_persist_failed", event_type=event.event_type)

    async def restore_from_redis(self) -> int:
        """启动时从 Redis 恢复死信到内存队列（恢复后清空 Redis，由内存接管）。"""
        try:
            from app.db.redis import get_redis

            redis_client = get_redis()
            raw_items = await redis_client.lrange(self._REDIS_KEY, 0, -1)
            if not raw_items:
                return 0
            restored = 0
            for raw in raw_items:
                try:
                    data = json.loads(str(raw))
                    ev = DeadLetterEvent.from_dict(data)
                    self._queue.append(ev)
                    restored += 1
                except Exception:  # noqa: BLE001
                    continue
            await redis_client.delete(self._REDIS_KEY)
            if restored:
                logger.info("dlq_restored_from_redis", count=restored)
            return restored
        except Exception:  # noqa: BLE001 - Redis 不可用则仅内存
            return 0

    async def _remove_from_redis(self, event: DeadLetterEvent) -> None:
        """重放成功/耗尽的死信从 Redis 移除（best-effort）。"""
        try:
            from app.db.redis import get_redis

            await get_redis().lrem(
                self._REDIS_KEY, 1, json.dumps(event.to_dict(), ensure_ascii=False)
            )
        except Exception:  # noqa: BLE001
            pass

    def send_to_dlq(
        self,
        event_type: str,
        payload: dict[str, Any],
        failure_reason: str,
        retry_count: int = 0,
    ) -> None:
        """将事件写入死信队列。"""
        # R5（审查修复）：打满时告警（此前 deque maxlen 静默丢弃最老事件，
        # queue_size 恒为容量值掩盖真实积压）
        if len(self._queue) >= self._max_size:
            self._dropped_count += 1
            logger.error(
                "dlq_full_dropping_oldest",
                event_type=event_type,
                dropped_total=self._dropped_count,
                queue_size=len(self._queue),
                max_size=self._max_size,
            )
        event = DeadLetterEvent(
            event_type=event_type,
            payload=payload,
            failure_reason=failure_reason,
            retry_count=retry_count,
        )
        self._queue.append(event)
        self._persist_to_redis(event)
        logger.warning(
            "event_sent_to_dlq",
            event_type=event_type,
            failure_reason=failure_reason,
            retry_count=retry_count,
            queue_size=len(self._queue),
            dropped_total=self._dropped_count,
        )

    def get_pending(self, limit: int = _REPLAY_BATCH_SIZE) -> list[DeadLetterEvent]:
        """获取待重放事件（PENDING 状态）。"""
        pending = [e for e in self._queue if e.status == "PENDING"]
        return pending[:limit]

    def get_all(self) -> list[DeadLetterEvent]:
        """获取全部死信事件（管理接口用）。"""
        return list(self._queue)

    @property
    def size(self) -> int:
        """当前队列大小。"""
        return len(self._queue)

    def mark_retried(self, event: DeadLetterEvent, success: bool) -> None:
        """标记重试结果。"""
        event.last_retry_at = datetime.now(UTC)
        if success:
            event.status = "RETRIED"
            # 成功重放 → 同步从 Redis 移除该死信（best-effort）
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._remove_from_redis(event))
            except RuntimeError:
                pass
        else:
            event.retry_count += 1
            if event.retry_count >= 5:
                event.status = "EXHAUSTED"
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._remove_from_redis(event))
                except RuntimeError:
                    pass
            else:
                event.status = "PENDING"

    async def start_replay_loop(self) -> None:
        """启动定时重放循环。"""
        self._replay_task = asyncio.create_task(self._replay_loop())

    async def _replay_loop(self) -> None:
        """定时重放死信事件。"""
        while True:
            try:
                await asyncio.sleep(_REPLAY_INTERVAL)
                await self.retry_dlq()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("dlq_replay_loop_error", exc_info=True)

    async def retry_dlq(self, limit: int = _REPLAY_BATCH_SIZE) -> int:
        """重试死信队列中的事件。

        Returns:
            成功重放的事件数。
        """
        from app.core.eventbus import get_eventbus

        pending = self.get_pending(limit)
        if not pending:
            return 0

        bus = get_eventbus()
        success_count = 0
        for event in pending:
            try:
                # _skip_dlq=True：避免重放失败的事件被重新加入死信队列（循环）
                await bus.publish(event.event_type, event.payload, _skip_dlq=True)
                self.mark_retried(event, success=True)
                success_count += 1
                logger.info("dlq_replay_success", event_type=event.event_type)
            except Exception as exc:
                self.mark_retried(event, success=False)
                logger.warning(
                    "dlq_replay_failed",
                    event_type=event.event_type,
                    error=str(exc),
                )
        return success_count

    async def stop(self) -> None:
        """停止重放循环。"""
        if self._replay_task is not None:
            self._replay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._replay_task
            self._replay_task = None


# 模块级单例
_dlq: DeadLetterQueue | None = None


def get_dlq() -> DeadLetterQueue:
    """获取死信队列单例。"""
    global _dlq
    if _dlq is None:
        _dlq = DeadLetterQueue()
    return _dlq


def init_dlq() -> DeadLetterQueue:
    """初始化死信队列（lifespan 中调用）。"""
    global _dlq
    _dlq = DeadLetterQueue()
    return _dlq
