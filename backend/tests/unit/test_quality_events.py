"""质量事件发布器单测（补齐 coverage ≥85%）。

覆盖 events.py 全部分支：
- publish: EventBus 发布/降级/熔断丢弃/HTTP 发送成功与失败
- _send: 无 notify_url 静默、有 URL 建客户端并 POST、客户端复用
- close: 无客户端 no-op / 关闭并复位
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.quality import events


@pytest.fixture
def breaker() -> MagicMock:
    b = MagicMock()
    b.allow.return_value = True
    b.record_success = MagicMock()
    b.record_failure = MagicMock()
    return b


@pytest.fixture
def publisher(breaker: MagicMock, monkeypatch: pytest.MonkeyPatch) -> events.QualityEventPublisher:
    pub = events.QualityEventPublisher(notify_url="http://notify")
    # 模块级 _BREAKER 是真实熔断器单例，测试中替换为可控替身
    # （publish 内 record_success/record_failure 直接引用模块全局）
    monkeypatch.setattr(events, "_BREAKER", breaker)
    return pub


def _patch_eventbus(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    bus = MagicMock()
    bus.publish = AsyncMock()
    monkeypatch.setattr("app.core.eventbus.get_eventbus", lambda: bus)
    return bus


class TestPublish:
    async def test_publish_sends_eventbus_and_http(
        self,
        monkeypatch: pytest.MonkeyPatch,
        publisher: events.QualityEventPublisher,
        breaker: MagicMock,
    ) -> None:
        bus = _patch_eventbus(monkeypatch)
        send = AsyncMock()
        publisher._send = send

        await publisher.publish(
            {"event_type": "quality.anomaly", "metric_code": "M1", "level": "P1"}
        )

        # EventBus 收到剥离 event_type 的 payload
        bus.publish.assert_awaited_once_with(
            "quality.anomaly", {"metric_code": "M1", "level": "P1"}
        )
        send.assert_awaited_once()
        breaker.record_success.assert_called_once()

    async def test_publish_without_event_type_skips_eventbus(
        self,
        monkeypatch: pytest.MonkeyPatch,
        publisher: events.QualityEventPublisher,
        breaker: MagicMock,
    ) -> None:
        bus = _patch_eventbus(monkeypatch)
        send = AsyncMock()
        publisher._send = send

        await publisher.publish({"metric_code": "M1"})

        bus.publish.assert_not_called()
        send.assert_awaited_once()
        breaker.record_success.assert_called_once()

    async def test_publish_eventbus_failure_degrades_but_sends_http(
        self,
        monkeypatch: pytest.MonkeyPatch,
        publisher: events.QualityEventPublisher,
        breaker: MagicMock,
    ) -> None:
        bus = MagicMock()
        bus.publish = AsyncMock(side_effect=RuntimeError("eventbus down"))
        monkeypatch.setattr("app.core.eventbus.get_eventbus", lambda: bus)
        send = AsyncMock()
        publisher._send = send

        # 不应抛异常（best-effort 降级）
        await publisher.publish({"event_type": "quality.anomaly"})

        send.assert_awaited_once()
        breaker.record_success.assert_called_once()

    async def test_publish_breaker_open_drops_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        publisher: events.QualityEventPublisher,
        breaker: MagicMock,
    ) -> None:
        _patch_eventbus(monkeypatch)
        breaker.allow.return_value = False
        send = AsyncMock()
        publisher._send = send

        await publisher.publish({"event_type": "quality.anomaly"})

        send.assert_not_called()
        breaker.record_failure.assert_not_called()

    async def test_publish_http_failure_records_breaker_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        publisher: events.QualityEventPublisher,
        breaker: MagicMock,
    ) -> None:
        _patch_eventbus(monkeypatch)
        publisher._send = AsyncMock(side_effect=ConnectionError("notify down"))

        # 不应抛异常（降级）
        await publisher.publish({"event_type": "quality.anomaly"})

        breaker.record_failure.assert_called_once()
        breaker.record_success.assert_not_called()


class TestSend:
    async def test_send_without_url_is_silent_noop(self) -> None:
        pub = events.QualityEventPublisher(notify_url=None)
        # 无 URL → 直接 return，不触发 httpx
        await pub._send({"event_type": "quality.anomaly"})
        assert pub._http_client is None

    @staticmethod
    def _client(post_side_effect: Any = None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        client = MagicMock()
        client.post = AsyncMock(return_value=resp, side_effect=post_side_effect)
        return client

    async def test_send_with_url_creates_client_and_posts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client()

        monkeypatch.setattr("httpx.AsyncClient", lambda timeout: client)

        pub = events.QualityEventPublisher(notify_url="http://notify")
        await pub._send({"event_type": "quality.anomaly"})

        assert pub._http_client is client
        client.post.assert_awaited_once_with(
            "http://notify/api/v1/notify/events", json={"event_type": "quality.anomaly"}
        )

    async def test_send_reuses_existing_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._client()

        monkeypatch.setattr("httpx.AsyncClient", lambda timeout: client)

        pub = events.QualityEventPublisher(notify_url="http://notify")
        await pub._send({"event_type": "quality.anomaly"})
        await pub._send({"event_type": "quality.anomaly"})

        # 只创建一个客户端，post 调用两次（未被 aclose 复用）
        assert pub._http_client is client
        assert client.post.await_count == 2

    async def test_send_raises_on_non_2xx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = MagicMock()
        resp.status_code = 404
        resp.text = "not found"
        client = MagicMock()
        client.post = AsyncMock(return_value=resp)

        monkeypatch.setattr("httpx.AsyncClient", lambda timeout: client)

        pub = events.QualityEventPublisher(notify_url="http://notify")
        with pytest.raises(RuntimeError, match="404"):
            await pub._send({"event_type": "quality.anomaly"})


class TestClose:
    async def test_close_without_client_noop(self) -> None:
        pub = events.QualityEventPublisher(notify_url=None)
        await pub.close()  # 不应抛异常

    async def test_close_closes_and_resets_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = MagicMock()
        resp.status_code = 200
        client = MagicMock()
        client.post = AsyncMock(return_value=resp)
        client.aclose = AsyncMock()
        monkeypatch.setattr("httpx.AsyncClient", lambda timeout: client)

        pub = events.QualityEventPublisher(notify_url="http://notify")
        await pub._send({"event_type": "quality.anomaly"})
        assert pub._http_client is client

        await pub.close()
        client.aclose.assert_awaited_once()
        assert pub._http_client is None

    async def test_close_suppresses_aclose_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = MagicMock()
        resp.status_code = 200
        client = MagicMock()
        client.post = AsyncMock(return_value=resp)
        client.aclose = AsyncMock(side_effect=RuntimeError("close boom"))
        monkeypatch.setattr("httpx.AsyncClient", lambda timeout: client)

        pub = events.QualityEventPublisher(notify_url="http://notify")
        await pub._send({"event_type": "quality.anomaly"})

        await pub.close()  # aclose 异常被吞
        assert pub._http_client is None
