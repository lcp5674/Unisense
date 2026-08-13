"""governance 事件发布器单测（best-effort / 熔断降级）。

覆盖 GovernanceEventPublisher 全部分支：
- publish：成功上报 / 失败降级 / 熔断打开静默丢弃
- _send：未配置 notify_url 静默跳过 / 已配置走 httpx 投递
- close：关闭已有 client / 无 client 幂等
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.services.governance import events as events_mod
from app.services.governance.events import GovernanceEventPublisher


@patch("app.services.governance.events._BREAKER")
async def test_publish_success_records_success(mock_breaker: MagicMock) -> None:
    mock_breaker.allow.return_value = True
    pub = GovernanceEventPublisher(notify_url="http://notify")
    pub._send = AsyncMock()  # noqa: SLF001
    await pub.publish({"event_type": "classification.done"})
    pub._send.assert_awaited_once()
    mock_breaker.record_success.assert_called_once()
    mock_breaker.record_failure.assert_not_called()


@patch("app.services.governance.events._BREAKER")
async def test_publish_breaker_open_drops_event(mock_breaker: MagicMock) -> None:
    mock_breaker.allow.return_value = False
    pub = GovernanceEventPublisher(notify_url="http://notify")
    pub._send = AsyncMock()  # noqa: SLF001
    with patch.object(events_mod.logger, "warning") as warn:
        await pub.publish({"event_type": "grant.granted"})
    pub._send.assert_not_awaited()
    mock_breaker.record_success.assert_not_called()
    mock_breaker.record_failure.assert_not_called()
    warn.assert_called_once()


@patch("app.services.governance.events._BREAKER")
async def test_publish_failure_degrades(mock_breaker: MagicMock) -> None:
    mock_breaker.allow.return_value = True
    pub = GovernanceEventPublisher(notify_url="http://notify")
    pub._send = AsyncMock(side_effect=RuntimeError("notify down"))  # noqa: SLF001
    with patch.object(events_mod.logger, "warning") as warn:
        await pub.publish({"event_type": "pii.reviewed"})
    mock_breaker.record_failure.assert_called_once()
    mock_breaker.record_success.assert_not_called()
    warn.assert_called_once()


async def test_send_without_notify_url_silently_skips() -> None:
    pub = GovernanceEventPublisher(notify_url=None)
    await pub._send({"event_type": "classification.changed"})  # noqa: SLF001
    assert pub._http_client is None  # noqa: SLF001


@patch("httpx.AsyncClient")
async def test_send_posts_to_notify_todo(mock_client_cls: MagicMock) -> None:
    inner = MagicMock()
    inner.post = AsyncMock()
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=inner)
    client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = client

    pub = GovernanceEventPublisher(notify_url="http://notify")
    await pub._send({"event_type": "grant.revoked"})  # noqa: SLF001
    mock_client_cls.assert_called_once_with(timeout=2.0)
    inner.post.assert_awaited_once_with(
        "http://notify/api/v1/notify/todo", json={"event_type": "grant.revoked"}
    )


@patch("httpx.AsyncClient")
async def test_send_reuses_http_client(mock_client_cls: MagicMock) -> None:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=MagicMock(post=AsyncMock()))
    client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = client

    pub = GovernanceEventPublisher(notify_url="http://notify")
    await pub._send({"event_type": "a"})  # noqa: SLF001
    await pub._send({"event_type": "b"})  # noqa: SLF001
    mock_client_cls.assert_called_once_with(timeout=2.0)


async def test_close_closes_http_client() -> None:
    pub = GovernanceEventPublisher(notify_url="http://notify")
    client = MagicMock()
    client.aclose = AsyncMock()
    pub._http_client = client  # noqa: SLF001
    await pub.close()
    client.aclose.assert_awaited_once()
    assert pub._http_client is None  # noqa: SLF001


async def test_close_without_client_is_noop() -> None:
    pub = GovernanceEventPublisher()
    await pub.close()
    assert pub._http_client is None  # noqa: SLF001
