"""采集事件发布（对齐 TD §12.1「Redis 发布」/ DEV_GUIDE §17.2）。

一致性模型（C1/C2 澄清）：采集事件进入通知闭环的**主通道**是 EventBus 本地
订阅者——API 与 worker 两个进程各自注册 notify 消费者，各自处理本进程发布
的事件（worker 侧此前从未注册导致事件全丢，C1 已修复）。本 Redis 裸通道
（``collector_events``）为 **best-effort 冗余广播**，供未来可选的跨进程
下游（影响分析刷新、采集编排）消费，当前无消费方，允许丢失——不作为一致性
保障，不阻断采集主流程。

Redis 属可选依赖；发布经 CircuitBreaker 包裹：Redis 不可用时熔断打开，
发布静默降级（返回 False），**不**阻断主流程（舱壁隔离）。
"""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis

from app.core.logging import get_logger
from app.core.resilience import CircuitBreaker

logger = get_logger("unisense.collector.events")

_CHANNEL = "collector_events"


class CatalogEventPublisher:
    """采集事件发布器（可选依赖 + 熔断）。"""

    def __init__(self, redis: Redis | None, breaker: CircuitBreaker | None = None) -> None:
        self._redis = redis
        self._breaker = breaker or CircuitBreaker()
        self._enabled = redis is not None

    async def publish(self, event_type: str, payload: dict[str, Any]) -> bool:
        """发布事件。可选依赖不可用或熔断打开时返回 False（非阻断）。"""
        if not self._enabled or not self._breaker.allow():
            return False
        message = json.dumps({"event_type": event_type, **payload}, ensure_ascii=False)
        try:
            await self._redis.publish(_CHANNEL, message)  # type: ignore[union-attr]
            return True
        except Exception:
            self._breaker.record_failure()
            logger.warning("catalog_event_publish_failed", event_type=event_type)
            return False

    async def publish_batch(self, event_type: str, payloads: list[dict[str, Any]]) -> bool:
        """批量发布事件（单次 Redis publish 含多个事件，FR-024）。

        将多个事件打包为一条 Redis 消息发布，减少网络往返。

        Args:
            event_type: 事件类型。
            payloads: 事件负载列表。

        Returns:
            True 如果发布成功；False 如果 Redis 不可用或熔断。
        """
        if not self._enabled or not self._breaker.allow():
            return False
        if not payloads:
            return True
        message = json.dumps(
            {
                "event_type": event_type,
                "batch": True,
                "items": payloads,
            },
            ensure_ascii=False,
        )
        try:
            await self._redis.publish(_CHANNEL, message)  # type: ignore[union-attr]
            return True
        except Exception:
            self._breaker.record_failure()
            logger.warning("catalog_event_publish_batch_failed", event_type=event_type)
            return False
