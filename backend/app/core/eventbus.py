"""统一事件总线抽象（对齐 TD §5.5 事件驱动 / DEV_GUIDE §14）。

底层使用 Redis Pub/Sub + 本地订阅者注册表，publish 失败时 best-effort 并记录告警日志。
替代所有散落的 _safe_publish 实现。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

import redis.asyncio as aioredis

from app.core.logging import get_logger

logger = get_logger(__name__)

# 订阅者回调类型
Handler = Callable[[dict[str, Any]], Any]


class EventBus:
    """统一事件总线：Redis Pub/Sub + 本地订阅者注册表。

    - publish: 发布事件到 Redis 频道 + 调用本地订阅者
    - subscribe / unsubscribe: 管理本地订阅者
    - best-effort: publish 失败时仅记录告警日志，不影响主流程
    """

    def __init__(self, redis_pool: aioredis.Redis | None = None) -> None:
        self._redis = redis_pool
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)

    async def publish(
        self, event_type: str, payload: dict[str, Any], actor_id: str = ""
    ) -> None:
        """发布事件。

        Args:
            event_type: 事件类型（如 "metric.created"、"quality.anomaly"）。
            payload: 事件负载字典。
            actor_id: 事件发起者 ID。
        """
        event = {
            "event_type": event_type,
            "payload": payload,
            "actor_id": actor_id,
        }
        # 1. 调用本地订阅者
        handlers = self._subscribers.get(event_type, [])
        for handler in handlers:
            try:
                result = handler(event)
                # 支持异步回调
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.warning(
                    "eventbus.local_handler_failed",
                    event_type=event_type,
                    handler=handler.__qualname__,
                    exc_info=True,
                )

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
