"""通知服务单元测试（TD §12.9 / FR-16 / FR-17）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.models.notify import EventLog, SubscriptionPref
from app.services.notify.schemas import EventPublish, SubscriptionUpsert
from app.services.notify.service import NotifyService


def _svc() -> tuple[NotifyService, MagicMock]:
    db = MagicMock()
    svc = NotifyService(db)
    repo = MagicMock()
    repo.save_event = AsyncMock(side_effect=lambda e: _stamp(e))
    repo.list_enabled_subscriptions = AsyncMock(return_value=[])
    repo.save_notification = AsyncMock(side_effect=lambda n: n)
    repo.find_subscription = AsyncMock(return_value=None)
    repo.save_subscription = AsyncMock(side_effect=lambda s: s)
    repo.list_subscriptions = AsyncMock(return_value=[])
    repo.commit = AsyncMock()
    svc._repo = repo  # noqa: SLF001
    return svc, repo


def _stamp(e: EventLog) -> EventLog:
    e.id = 1
    return e


async def test_publish_event_no_subscribers() -> None:
    svc, repo = _svc()
    out = await svc.publish_event(EventPublish(event_type="q.refresh", source="scheduler"))
    assert out["event_id"] == 1
    assert out["notifications"] == 0
    repo.save_event.assert_awaited()
    repo.commit.assert_awaited()


async def test_publish_event_fanout() -> None:
    svc, repo = _svc()
    repo.list_enabled_subscriptions = AsyncMock(
        return_value=[
            SubscriptionPref(user_id=10, channel="EMAIL", event_type="q.refresh", enabled=True),
            SubscriptionPref(user_id=11, channel="SMS", event_type="q.refresh", enabled=True),
        ]
    )
    out = await svc.publish_event(EventPublish(event_type="q.refresh"))
    assert out["notifications"] == 2
    assert repo.save_notification.await_count == 2


async def test_upsert_subscription_creates() -> None:
    svc, repo = _svc()
    out = await svc.upsert_subscription(
        SubscriptionUpsert(user_id=7, channel="EMAIL", event_type="q.refresh", enabled=True)
    )
    assert isinstance(out, SubscriptionPref)
    repo.save_subscription.assert_awaited()
