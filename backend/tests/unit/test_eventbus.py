"""统一事件总线单测（app/core/eventbus.py）。

覆盖：
- publish 本地订阅者（同步/异步回调）、回调失败 best-effort 不阻断后续。
- Redis Pub/Sub 发布成功 / 失败 best-effort。
- subscribe / unsubscribe 注册表管理。
- get_eventbus 单例与 init_eventbus 生命周期注入。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from app.core import eventbus as eb_module
from app.core.eventbus import EventBus


class TestLocalSubscribers:
    async def test_publish_calls_sync_handler(self) -> None:
        bus = EventBus()
        received: list[dict] = []

        def handler(event: dict) -> None:
            received.append(event)

        bus.subscribe("metric.created", handler)
        await bus.publish("metric.created", {"code": "a"}, actor_id="1")
        await bus.drain()  # R3：订阅者后台执行，等待完成后再断言

        assert len(received) == 1
        assert received[0]["event_type"] == "metric.created"
        assert received[0]["payload"] == {"code": "a"}
        assert received[0]["actor_id"] == "1"

    async def test_publish_awaits_async_handler(self) -> None:
        bus = EventBus()
        done: list[str] = []

        async def handler(event: dict) -> None:
            done.append(event["event_type"])

        bus.subscribe("x", handler)
        await bus.publish("x", {})
        await bus.drain()  # R3：等待后台订阅者任务完成
        assert done == ["x"]

    async def test_handler_failure_does_not_block_others(self) -> None:
        bus = EventBus()
        calls: list[int] = []

        def bad(_event: dict) -> None:
            raise RuntimeError("boom")

        def good(_event: dict) -> None:
            calls.append(1)

        bus.subscribe("x", bad)
        bus.subscribe("x", good)
        # 不应抛出异常，且后续 handler 仍被执行
        await bus.publish("x", {})
        await bus.drain()  # R3：等待后台订阅者任务完成
        assert calls == [1]

    async def test_no_subscribers_is_noop(self) -> None:
        bus = EventBus()
        await bus.publish("no_subscribers", {})  # 不应抛异常


class TestRedisPublish:
    async def test_publish_to_redis_channel(self) -> None:
        redis = AsyncMock()
        redis.publish = AsyncMock(return_value=1)
        bus = EventBus(redis_pool=redis)

        await bus.publish("quality.anomaly", {"metric": "m1"}, actor_id="7")

        redis.publish.assert_awaited_once()
        channel = redis.publish.call_args[0][0]
        assert channel == "unisense:events:quality.anomaly"
        payload = json.loads(redis.publish.call_args[0][1])
        assert payload["event_type"] == "quality.anomaly"
        assert payload["actor_id"] == "7"

    async def test_redis_failure_is_best_effort(self) -> None:
        redis = AsyncMock()
        redis.publish = AsyncMock(side_effect=RuntimeError("redis down"))
        bus = EventBus(redis_pool=redis)
        # 发布失败仅告警，不抛异常
        await bus.publish("x", {})
        redis.publish.assert_awaited_once()


class TestDlqWiring:
    """TECH-04：Redis 发布失败 → 事件进入死信队列（DLQ 重放跳过，避免循环）。"""

    async def test_redis_failure_enqueues_dlq(self, monkeypatch) -> None:
        from app.core import dlq as dlq_module

        # send_to_dlq 为同步方法，用普通 MagicMock（避免未 await 协程告警）
        fake_dlq = MagicMock()
        monkeypatch.setattr(dlq_module, "get_dlq", lambda: fake_dlq)

        redis = AsyncMock()
        redis.publish = AsyncMock(side_effect=RuntimeError("redis down"))
        bus = EventBus(redis_pool=redis)
        await bus.publish("metric.created", {"code": "a"})

        fake_dlq.send_to_dlq.assert_called_once_with(
            "metric.created", {"code": "a"}, "eventbus.redis_publish_failed"
        )

    async def test_skip_dlq_avoids_loop(self, monkeypatch) -> None:
        from app.core import dlq as dlq_module

        fake_dlq = MagicMock()
        monkeypatch.setattr(dlq_module, "get_dlq", lambda: fake_dlq)

        redis = AsyncMock()
        redis.publish = AsyncMock(side_effect=RuntimeError("redis down"))
        bus = EventBus(redis_pool=redis)
        # DLQ 重放路径传 _skip_dlq=True，失败不重新入队（防循环）
        await bus.publish("x", {}, _skip_dlq=True)
        fake_dlq.send_to_dlq.assert_not_called()

    async def test_redis_ok_no_dlq(self, monkeypatch) -> None:
        from app.core import dlq as dlq_module

        fake_dlq = MagicMock()
        monkeypatch.setattr(dlq_module, "get_dlq", lambda: fake_dlq)

        redis = AsyncMock()
        redis.publish = AsyncMock(return_value=1)
        bus = EventBus(redis_pool=redis)
        await bus.publish("x", {})
        fake_dlq.send_to_dlq.assert_not_called()

    async def test_dlq_enqueue_failure_best_effort(self, monkeypatch) -> None:
        from app.core import dlq as dlq_module

        def _boom() -> None:
            raise RuntimeError("dlq full")

        monkeypatch.setattr(dlq_module, "get_dlq", _boom)

        redis = AsyncMock()
        redis.publish = AsyncMock(side_effect=RuntimeError("redis down"))
        bus = EventBus(redis_pool=redis)
        # DLQ 入队失败也不阻断主流程
        await bus.publish("x", {})


class TestSubscriberRegistry:
    def test_subscribe_and_unsubscribe(self) -> None:
        bus = EventBus()
        handler = lambda event: None  # noqa: E731
        bus.subscribe("x", handler)
        assert bus._subscribers["x"] == [handler]

        bus.unsubscribe("x", handler)
        assert bus._subscribers["x"] == []

    def test_unsubscribe_missing_handler_is_noop(self) -> None:
        bus = EventBus()
        bus.unsubscribe("x", lambda event: None)  # 不应抛异常
        bus.unsubscribe("never_subscribed", lambda event: None)

    def test_multiple_handlers_same_type(self) -> None:
        bus = EventBus()
        h1 = lambda event: None  # noqa: E731
        h2 = lambda event: None  # noqa: E731
        bus.subscribe("x", h1)
        bus.subscribe("x", h2)
        assert len(bus._subscribers["x"]) == 2
        bus.unsubscribe("x", h1)
        assert bus._subscribers["x"] == [h2]


class TestSingleton:
    def test_get_eventbus_returns_singleton(self, monkeypatch) -> None:
        old = eb_module._eventbus
        monkeypatch.setattr(eb_module, "_eventbus", None)
        try:
            b1 = eb_module.get_eventbus()
            b2 = eb_module.get_eventbus()
            assert b1 is b2
            # 未 init 时无 Redis（仅本地订阅者）
            assert b1._redis is None
        finally:
            monkeypatch.setattr(eb_module, "_eventbus", old)

    def test_init_eventbus_replaces_singleton(self, monkeypatch) -> None:
        old = eb_module._eventbus
        monkeypatch.setattr(eb_module, "_eventbus", None)
        try:
            redis = AsyncMock()
            inst = eb_module.init_eventbus(redis)
            assert eb_module.get_eventbus() is inst
            assert inst._redis is redis
        finally:
            monkeypatch.setattr(eb_module, "_eventbus", old)
