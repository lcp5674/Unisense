"""通知服务 Repository（TD §12.9 / FR-16 / FR-17）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notify import EventLog, Notification, SubscriptionPref


class NotifyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_event(self, obj: EventLog) -> EventLog:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def save_notification(self, obj: Notification) -> Notification:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def list_notifications(
        self, subscriber_id: int, status: str | None
    ) -> list[Notification]:
        stmt = select(Notification).where(Notification.subscriber_id == subscriber_id)
        if status:
            stmt = stmt.where(Notification.status == status)
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_notification(self, notif_id: int) -> Notification | None:
        stmt = select(Notification).where(Notification.id == notif_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_subscription(
        self, user_id: int, channel: str, event_type: str
    ) -> SubscriptionPref | None:
        stmt = select(SubscriptionPref).where(
            SubscriptionPref.user_id == user_id,
            SubscriptionPref.channel == channel,
            SubscriptionPref.event_type == event_type,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_subscriptions(self, user_id: int) -> list[SubscriptionPref]:
        stmt = select(SubscriptionPref).where(SubscriptionPref.user_id == user_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_enabled_subscriptions(self, event_type: str) -> list[SubscriptionPref]:
        stmt = select(SubscriptionPref).where(
            SubscriptionPref.event_type == event_type,
            SubscriptionPref.enabled.is_(True),
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def save_subscription(self, obj: SubscriptionPref) -> SubscriptionPref:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def list_event_logs(self, event_type: str | None, limit: int) -> list[EventLog]:
        stmt = select(EventLog)
        if event_type:
            stmt = stmt.where(EventLog.event_type == event_type)
        rows = (
            (await self._session.execute(stmt.order_by(EventLog.id.desc()).limit(limit)))
            .scalars()
            .all()
        )
        return list(rows)

    async def commit(self) -> None:
        await self._session.commit()
