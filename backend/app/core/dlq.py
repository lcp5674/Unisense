"""死信队列模块（TECH-04: 事件总线指数退避 + 死信队列）。

职责：
1. 存储事件总线重试耗尽的事件
2. 定时重放死信事件
3. 管理接口查询死信状态

内存实现（事件量不大），对齐 R&D-04。
"""

from __future__ import annotations

import asyncio
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


class DeadLetterQueue:
    """内存死信队列（单实例，进程内）。"""

    def __init__(self, max_size: int = _DLQ_MAX_SIZE) -> None:
        self._queue: deque[DeadLetterEvent] = deque(maxlen=max_size)
        self._replay_task: asyncio.Task[None] | None = None  # type: ignore[type-arg]

    def send_to_dlq(
        self,
        event_type: str,
        payload: dict[str, Any],
        failure_reason: str,
        retry_count: int = 0,
    ) -> None:
        """将事件写入死信队列。"""
        event = DeadLetterEvent(
            event_type=event_type,
            payload=payload,
            failure_reason=failure_reason,
            retry_count=retry_count,
        )
        self._queue.append(event)
        logger.warning(
            "event_sent_to_dlq",
            event_type=event_type,
            failure_reason=failure_reason,
            retry_count=retry_count,
            queue_size=len(self._queue),
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
        else:
            event.retry_count += 1
            if event.retry_count >= 5:
                event.status = "EXHAUSTED"
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
                await bus.publish(event.event_type, event.payload)
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
            try:
                await self._replay_task
            except asyncio.CancelledError:
                pass
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
