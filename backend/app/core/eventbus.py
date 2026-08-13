"""统一事件总线抽象（对齐 TD §5.5 事件驱动 / DEV_GUIDE §14）。

底层使用 Redis Pub/Sub + 本地订阅者注册表，publish 失败时 best-effort 并记录告警日志。
替代所有散落的 _safe_publish 实现。

TECH-04（R&D-04）：本地订阅者调用失败时指数退避重试（1s→2s→4s→max 30s），
3 次重试后写入死信队列（DLQ），保证事件不丢失。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from typing import Any

import redis.asyncio as aioredis

from app.core.logging import get_logger

logger = get_logger(__name__)

# TECH-04: 指数退避重试延迟（秒），3 次重试后写 DLQ
_RETRY_DELAYS: list[float] = [1.0, 2.0, 4.0]
_MAX_RETRY_BACKOFF = 30.0  # 退避上限


def _enqueue_dlq(event_type: str, payload: dict[str, Any], reason: str) -> None:
    """将发布失败的事件写入死信队列（best-effort，不阻断主流程）。

    TECH-04：Redis 发布失败时事件进入 DLQ，由 DLQ 定时重放兜底，
    保证事件不丢失（对齐 R&D-04 死信队列）。
    """
    try:
        from app.core.dlq import get_dlq

        get_dlq().send_to_dlq(event_type, payload, reason)
    except Exception:
        logger.warning("eventbus.dlq_enqueue_failed", event_type=event_type, exc_info=True)


# 订阅者回调类型
Handler = Callable[[dict[str, Any]], Any]


class EventBus:
    """统一事件总线：Redis Pub/Sub + 本地订阅者注册表。

    - publish: 发布事件到 Redis 频道 + 调用本地订阅者
    - subscribe / unsubscribe: 管理本地订阅者
    - best-effort: publish 失败时仅记录告警日志，不影响主流程
    - TECH-04: 本地订阅者调用失败时指数退避重试，3 次后写 DLQ
    """

    def __init__(self, redis_pool: aioredis.Redis | None = None) -> None:
        self._redis = redis_pool
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        actor_id: str = "",
        *,
        _skip_dlq: bool = False,
    ) -> None:
        """发布事件。

        Args:
            event_type: 事件类型（如 "metric.created"、"quality.anomaly"）。
            payload: 事件负载字典。
            actor_id: 事件发起者 ID。
            _skip_dlq: 内部参数——DLQ 重放时置 True，避免失败事件被重新
                加入死信队列造成循环。
        """
        event = {
            "event_type": event_type,
            "payload": payload,
            "actor_id": actor_id,
        }
        # 1. 调用本地订阅者（TECH-04: 指数退避重试）
        handlers = self._subscribers.get(event_type, [])
        for handler in handlers:
            await self._invoke_with_retry(handler, event, event_type)

        # 2. 发布到 Redis Pub/Sub（best-effort）
        if self._redis is not None:
            try:
                channel = f"unisense:events:{event_type}"
                import json

                await self._redis.publish(channel, json.dumps(event, ensure_ascii=False))
            except Exception:
                logger.warning(
                    "eventbus.redis_publish_failed",
                    event_type=event_type,
                    exc_info=True,
                )
                # TECH-04：发布失败进入死信队列（DLQ 重放时跳过，避免循环）
                if not _skip_dlq:
                    _enqueue_dlq(event_type, payload, "eventbus.redis_publish_failed")

    async def _invoke_with_retry(
        self, handler: Handler, event: dict[str, Any], event_type: str
    ) -> None:
        """调用本地订阅者，失败时指数退避重试，3 次后写 DLQ（TECH-04）。

        Args:
            handler: 订阅者回调函数。
            event: 事件字典。
            event_type: 事件类型（用于日志/DLQ）。
        """
        for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
            try:
                result = handler(event)
                if hasattr(result, "__await__"):
                    await result
                return  # 成功则直接返回
            except Exception:
                actual_delay = min(delay, _MAX_RETRY_BACKOFF)
                logger.warning(
                    "eventbus.local_handler_failed_retry",
                    event_type=event_type,
                    handler=handler.__qualname__,
                    attempt=attempt,
                    max_retries=len(_RETRY_DELAYS),
                    retry_in=actual_delay,
                    exc_info=True,
                )
                await asyncio.sleep(actual_delay)

        # 所有重试耗尽，写入 DLQ
        logger.error(
            "eventbus.local_handler_exhausted",
            event_type=event_type,
            handler=handler.__qualname__,
            reason="handler_failed_after_retries",
        )
        _enqueue_dlq(event_type, event.get("payload", {}), "eventbus.handler_exhausted")

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """注册本地订阅者。

        Args:
            event_type: 订阅的事件类型。
            handler: 回调函数，接收事件字典。
        """
        self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        """移除本地订阅者。

        Args:
            event_type: 事件类型。
            handler: 要移除的回调函数。
        """
        handlers = self._subscribers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)


# 模块级单例（lifespan 中注入 Redis 连接池后替换）
_eventbus: EventBus | None = None


def get_eventbus() -> EventBus:
    """获取 EventBus 单例。

    Returns:
        EventBus 实例。
    """
    global _eventbus
    if _eventbus is None:
        _eventbus = EventBus()
    return _eventbus


def init_eventbus(redis_pool: aioredis.Redis | None) -> EventBus:
    """初始化 EventBus（lifespan 中调用）。

    Args:
        redis_pool: Redis 连接池，None 时仅使用本地订阅者。

    Returns:
        初始化后的 EventBus 实例。
    """
    global _eventbus
    _eventbus = EventBus(redis_pool)
    return _eventbus
