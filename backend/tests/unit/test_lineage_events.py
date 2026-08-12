"""lineage events 发布器单测（熔断降级 / Redis 失败不阻塞）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.core.resilience import CircuitBreaker
from app.services.lineage.events import LineageEventPublisher


class _FakeBreaker:
    def __init__(self, allow: bool = True) -> None:
        self._allow = allow
        self.success = 0
        self.failure = 0

    def allow(self) -> bool:
        return self._allow

    def record_success(self) -> None:
        self.success += 1

    def record_failure(self) -> None:
        self.failure += 1


async def test_publish_success() -> None:
    redis = MagicMock()
    redis.publish = AsyncMock()
    breaker = _FakeBreaker(allow=True)
    pub = LineageEventPublisher(redis, breaker=breaker)
    ok = await pub.publish("lineage_parsed", {"table_edges": 2})
    assert ok is True
    redis.publish.assert_awaited_once()
    assert breaker.success == 1


async def test_publish_breaker_open_returns_false() -> None:
    redis = MagicMock()
    redis.publish = AsyncMock()
    breaker = _FakeBreaker(allow=False)
    pub = LineageEventPublisher(redis, breaker=breaker)
    ok = await pub.publish("lineage_parsed", {})
    assert ok is False
    redis.publish.assert_not_awaited()


async def test_publish_redis_error_returns_false() -> None:
    redis = MagicMock()
    redis.publish = AsyncMock(side_effect=ConnectionError("redis down"))
    breaker = _FakeBreaker(allow=True)
    pub = LineageEventPublisher(redis, breaker=breaker)
    ok = await pub.publish("lineage_parsed", {})
    assert ok is False
    assert breaker.failure == 1


async def test_publish_payload_serialized_with_event_type() -> None:
    redis = MagicMock()
    redis.publish = AsyncMock()
    breaker = _FakeBreaker(allow=True)
    pub = LineageEventPublisher(redis, breaker=breaker)
    await pub.publish("lineage_deleted", {"node": "table:a"})
    args = redis.publish.await_args
    channel, msg = args.args
    assert channel == "lineage_events"
    import json

    payload = json.loads(msg)
    assert payload["event_type"] == "lineage_deleted"
    assert payload["node"] == "table:a"


def test_default_breaker_used_when_not_provided() -> None:
    redis = MagicMock()
    pub = LineageEventPublisher(redis)
    assert isinstance(pub._breaker, CircuitBreaker)
