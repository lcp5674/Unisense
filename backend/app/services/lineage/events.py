"""血缘事件发布器（Redis 发布/订阅，CircuitBreaker 降级）。

对齐 collector.events 的事件总线模式：事件总线不可用时经熔断器降级，
不阻塞主流程。事件仅供下游（影响分析刷新、采集编排）消费，允许丢失。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.logging import get_logger
from app.core.resilience import CircuitBreaker

logger = get_logger("unisense.lineage.events")


class LineageEventPublisher:
    """血缘事件发布器。"""

    def __init__(
        self,
        redis: Any,
        breaker: CircuitBreaker | None = None,
        channel: str = "lineage_events",
    ) -> None:
        self._redis = redis
        self._breaker = breaker or CircuitBreaker()
        self._channel = channel

    async def publish(self, event_type: str, payload: dict[str, Any]) -> bool:
        """发布血缘事件。成功返回 True；熔断器打开或 Redis 不可用时返回 False。"""
        if not self._breaker.allow():
            return False
        try:
            msg = json.dumps({"event_type": event_type, **payload}, ensure_ascii=False)
            await self._redis.publish(self._channel, msg)
            self._breaker.record_success()
            return True
        except Exception as exc:
            self._breaker.record_failure()
            logger.warning("lineage_event_publish_failed", error=str(exc))
            return False
