"""通知服务 Repository（TD §12.9 / FR-16 / FR-17）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notify import EventLog, Notification, SubscriptionPref
from app.models.user import User


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
        self,
        subscriber_id: int,
        status: str | None,
        limit: int = 200,
    ) -> list[Notification]:
        """列出订阅者通知；强制行数上限，防高活跃订阅者收件箱全量物化（D4）。"""
        stmt = (
            select(Notification)
            .where(Notification.subscriber_id == subscriber_id)
            .order_by(Notification.id.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(Notification.status == status)
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_notifications_page(
        self,
        subscriber_id: int,
        status: str | None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Notification], int]:
        """订阅者通知分页查询，返回 ``(items, total)``。

        page/page_size 由 API 层做边界约束（page>=1、page_size<=200），
        此处按 offset/limit 精确切页；total 供前端分页器计算总页数。
        """
        base = select(Notification).where(Notification.subscriber_id == subscriber_id)
        if status:
            base = base.where(Notification.status == status)
        total_stmt = select(func.count()).select_from(base.subquery())
        total = int((await self._session.execute(total_stmt)).scalar_one() or 0)
        offset = max(page - 1, 0) * page_size
        stmt = base.order_by(Notification.id.desc()).offset(offset).limit(page_size)
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows), total

    async def mark_all_read(self, subscriber_id: int) -> int:
        """将订阅者全部未读通知置为已读，返回更新条数。"""
        now = datetime.now(UTC)
        stmt = (
            update(Notification)
            .where(Notification.subscriber_id == subscriber_id, Notification.read_at.is_(None))
            .values(read_at=now)
        )
        res = await self._session.execute(stmt)
        return int(res.rowcount or 0)

    async def delete_notification(self, obj: Notification) -> None:
        await self._session.delete(obj)

    async def delete_all(self, subscriber_id: int) -> int:
        """删除订阅者全部通知（收件箱清空），返回删除条数。"""
        stmt = delete(Notification).where(Notification.subscriber_id == subscriber_id)
        res = await self._session.execute(stmt)
        return int(res.rowcount or 0)

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

    async def get_user_email(self, user_id: int) -> str | None:
        """按用户 ID 解析收件邮箱（订阅人为邮件投递真实收件人）。

        缺失或邮箱为空时返回 None，由调用方降级到配置的发件人/占位地址。
        """
        stmt = select(User.email).where(User.id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

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
