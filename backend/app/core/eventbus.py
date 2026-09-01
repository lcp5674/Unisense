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

    - publish: 发布事件到 Redis 频道 + 调度本地订阅者
    - subscribe / unsubscribe: 管理本地订阅者
    - best-effort: publish 失败时仅记录告警日志，不影响主流程
    - TECH-04: 本地订阅者调用失败时指数退避重试，3 次后写 DLQ
    - R3（审查修复）：本地订阅者调用移入后台任务（fire-and-forget + 强引用），
      请求路径不再被订阅者失败重试（1+2+4=7s）同步阻塞——事件投递不占请求线程。
    """

    def __init__(self, redis_pool: aioredis.Redis | None = None) -> None:
        self._redis = redis_pool
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        # R3/R4（审查修复）：在途后台任务强引用集合——防止 create_task 返回值
        # 被 GC 回收导致事件静默丢失（与 degradation.py 的 _in_flight_tasks 同范式）
        self._in_flight_tasks: set[asyncio.Task[Any]] = set()

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
        # 1. 调度本地订阅者（R3：后台任务执行，不阻塞请求路径；重试/DLQ 在后台完成）
        handlers = self._subscribers.get(event_type, [])
        for handler in handlers:
            self._spawn_subscriber_task(handler, event, event_type)

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

    def _spawn_subscriber_task(
        self, handler: Handler, event: dict[str, Any], event_type: str
    ) -> None:
        """将订阅者调用投递到后台任务并保存强引用（R3/R4）。

        无运行事件循环（CLI/极少数同步场景）时降级为同步调用——此时退避重试
        仍会阻塞，但该场景无并发请求可阻塞，可接受。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "eventbus.no_running_loop_sync_fallback",
                event_type=event_type,
                handler=handler.__qualname__,
            )
            # 无事件循环（CLI/极少数同步场景）：仅同步调用；异步 handler 无法运行，
            # 记录告警后丢弃（该场景无并发请求，事件语义由调用方兜底）。
            try:
                result = handler(event)
                if hasattr(result, "__await__"):
                    logger.warning(
                        "eventbus.async_handler_dropped_no_loop",
                        event_type=event_type,
                        handler=handler.__qualname__,
                    )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "eventbus.sync_handler_failed",
                    event_type=event_type,
                    handler=handler.__qualname__,
                    exc_info=True,
                )
            return

        task = loop.create_task(self._invoke_with_retry(handler, event, event_type))
        self._in_flight_tasks.add(task)
        task.add_done_callback(self._in_flight_tasks.discard)

    async def drain(self) -> None:
        """等待全部在途订阅者任务完成（测试与优雅关闭用）。

        事件投递改为后台任务后，测试需先 ``await bus.drain()`` 再断言订阅者
        副作用；lifespan 关闭时亦应调用以确保事件不丢失。
        """
        if not self._in_flight_tasks:
            return
        await asyncio.gather(*list(self._in_flight_tasks), return_exceptions=True)

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
