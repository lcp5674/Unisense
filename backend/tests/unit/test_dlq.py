"""死信队列单测（TECH-04: 事件总线指数退避 + 死信队列）。

覆盖：
- send_to_dlq / get_pending / get_all / size
- mark_retried 成功/失败/EXHAUSTED 流转
- retry_dlq 成功重放/失败重试/空队列
- start_replay_loop / stop
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.dlq import _DLQ_MAX_SIZE, DeadLetterQueue


class TestSendAndQuery:
    def test_send_and_size(self) -> None:
        q = DeadLetterQueue()
        q.send_to_dlq("quality.anomaly", {"metric": "m1"}, "redis down")
        assert q.size == 1
        assert q.get_all()[0].event_type == "quality.anomaly"
        assert q.get_all()[0].status == "PENDING"

    def test_get_pending_returns_pending_only(self) -> None:
        q = DeadLetterQueue()
        q.send_to_dlq("a", {}, "r1")
        q.send_to_dlq("b", {}, "r2")
        # 手工标记第一条为 EXHAUSTED
        event = q.get_all()[0]
        event.status = "EXHAUSTED"
        pending = q.get_pending(10)
        assert [e.event_type for e in pending] == ["b"]

    def test_get_pending_respects_limit(self) -> None:
        q = DeadLetterQueue()
        for i in range(5):
            q.send_to_dlq(f"t{i}", {}, "r")
        assert len(q.get_pending(2)) == 2

    def test_max_size_caps_queue(self) -> None:
        q = DeadLetterQueue(max_size=3)
        for i in range(6):
            q.send_to_dlq(f"t{i}", {}, "r")
        assert q.size == 3


class TestMarkRetried:
    def test_mark_success_sets_retried(self) -> None:
        q = DeadLetterQueue()
        q.send_to_dlq("a", {}, "r")
        event = q.get_all()[0]
        q.mark_retried(event, success=True)
        assert event.status == "RETRIED"
        assert event.last_retry_at is not None

    def test_mark_failure_increments_retry_count(self) -> None:
        q = DeadLetterQueue()
        q.send_to_dlq("a", {}, "r")
        event = q.get_all()[0]
        q.mark_retried(event, success=False)
        assert event.retry_count == 1
        assert event.status == "PENDING"

    def test_mark_failure_exhausted_after_max(self) -> None:
        q = DeadLetterQueue()
        q.send_to_dlq("a", {}, "r")
        event = q.get_all()[0]
        for _ in range(5):
            q.mark_retried(event, success=False)
        assert event.retry_count == 5
        assert event.status == "EXHAUSTED"


class TestRetryDlq:
    async def test_retry_empty_queue_returns_zero(self) -> None:
        q = DeadLetterQueue()
        n = await q.retry_dlq()
        assert n == 0

    async def test_retry_publishes_and_marks_success(self) -> None:
        q = DeadLetterQueue()
        q.send_to_dlq("metric.created", {"code": "a"}, "redis down")
        fake_bus = AsyncMock()
        # retry_dlq 内部为局部 import：from app.core.eventbus import get_eventbus
        with patch("app.core.eventbus.get_eventbus", return_value=fake_bus):
            n = await q.retry_dlq()
        assert n == 1
        fake_bus.publish.assert_awaited_once()
        # 重放路径必须 _skip_dlq=True，避免失败重新入队（防循环）
        assert fake_bus.publish.call_args.kwargs.get("_skip_dlq") is True
        assert q.get_all()[0].status == "RETRIED"

    async def test_retry_failure_marks_not_success(self) -> None:
        q = DeadLetterQueue()
        q.send_to_dlq("a", {}, "redis down")
        fake_bus = AsyncMock()
        fake_bus.publish = AsyncMock(side_effect=RuntimeError("still down"))
        with patch("app.core.eventbus.get_eventbus", return_value=fake_bus):
            n = await q.retry_dlq()
        assert n == 0
        assert q.get_all()[0].retry_count == 1
        assert q.get_all()[0].status == "PENDING"


class TestReplayLoop:
    @pytest.mark.asyncio
    async def test_start_and_stop(self) -> None:
        q = DeadLetterQueue()
        await q.start_replay_loop()
        assert q._replay_task is not None
        await q.stop()
        assert q._replay_task is None


class TestSingleton:
    def test_get_dlq_returns_singleton(self, monkeypatch) -> None:
        from app.core import dlq as dlq_module

        old = dlq_module._dlq
        monkeypatch.setattr(dlq_module, "_dlq", None)
        try:
            a = dlq_module.get_dlq()
            b = dlq_module.get_dlq()
            assert a is b
        finally:
            monkeypatch.setattr(dlq_module, "_dlq", old)

    def test_init_dlq_replaces_singleton(self, monkeypatch) -> None:
        from app.core import dlq as dlq_module

        old = dlq_module._dlq
        monkeypatch.setattr(dlq_module, "_dlq", None)
        try:
            inst = dlq_module.init_dlq()
            assert dlq_module.get_dlq() is inst
        finally:
            monkeypatch.setattr(dlq_module, "_dlq", old)


def test_constant_defaults() -> None:
    assert _DLQ_MAX_SIZE == 10_000
