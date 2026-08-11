"""通知服务（TD §12.9 / FR-16 / FR-17）。

核心能力：
1. 事件发布（EventLog 留痕）+ 按订阅偏好广播（Notification 扇出）。
2. 通知查询与状态回写（SENT / FAILED）。
3. 订阅偏好 upsert 与查询。
4. 通知外发渠道：SMTP / Webhook（可配置）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.notify import (
    EventLevel,
    EventLog,
    Notification,
    NotifyStatus,
    SubscriptionPref,
)
from app.services.notify.repository import NotifyRepository
from app.services.notify.schemas import EventPublish, SubscriptionUpsert

logger = logging.getLogger(__name__)


class NotifyService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = NotifyRepository(session)
        self._http_client = httpx.AsyncClient(timeout=10.0)

    async def publish_event(self, data: EventPublish) -> dict[str, int]:
        event = EventLog(
            event_type=data.event_type,
            source=data.source,
            payload=data.payload,
            level=data.level if data.level else EventLevel.INFO.value,
            notified=False,
        )
        await self._repo.save_event(event)
        subs = await self._repo.list_enabled_subscriptions(data.event_type)
        created = 0
        delivered = 0
        for sub in subs:
            notif = Notification(
                subscriber_id=sub.user_id,
                channel=sub.channel,
                template_code=data.event_type,
                title=data.event_type,
                body=json.dumps(data.payload, ensure_ascii=False) if data.payload else None,
                payload=data.payload,
                status=NotifyStatus.PENDING.value,
                ref_type="event",
                ref_id=event.id,
            )
            await self._repo.save_notification(notif)
            created += 1
            # 投递通知
            ok = await self._dispatch(notif, sub.channel)
            notif.status = NotifyStatus.SENT.value if ok else NotifyStatus.FAILED.value
            if ok:
                notif.sent_at = datetime.utcnow()
                delivered += 1
        event.notified = delivered > 0
        await self._repo.commit()
        return {"event_id": event.id, "notifications": created, "delivered": delivered}

    async def _dispatch(self, notif: Notification, channel: str) -> bool:
        """投递通知到指定渠道。

        支持渠道：
        - webhook: HTTP POST 到配置的 URL
        - email: SMTP 发送（待实现）
        - console: 日志输出（开发环境）
        """
        try:
            if channel == "webhook":
                return await self._dispatch_webhook(notif)
            elif channel == "email":
                return await self._dispatch_email(notif)
            elif channel == "console":
                logger.info("通知（console）: %s", notif.body)
                return True
            else:
                logger.warning("未知通知渠道: %s", channel)
                return False
        except Exception as exc:  # noqa: BLE001
            logger.error("通知投递失败: %s", exc)
            return False

    async def _dispatch_webhook(self, notif: Notification) -> bool:
        """Webhook 投递：POST 到配置的 webhook URL。"""
        webhook_url = settings.notify_webhook_url
        if not webhook_url:
            logger.warning("未配置 notify_webhook_url，跳过 webhook 投递")
            return False
        try:
            resp = await self._http_client.post(
                webhook_url,
                json={
                    "event_type": notif.template_code,
                    "title": notif.title,
                    "body": notif.body,
                    "payload": notif.payload,
                    "subscriber_id": notif.subscriber_id,
                    "sent_at": datetime.utcnow().isoformat(),
                },
                headers={"Content-Type": "application/json"},
            )
            return resp.status_code < 300
        except Exception as exc:
            logger.error("Webhook 投递失败: %s", exc)
            return False

    async def _dispatch_email(self, notif: Notification) -> bool:
        """邮件投递（待实现 SMTP）。"""
        logger.warning("邮件通知暂未实现，请配置 SMTP 服务端")
        return False

    async def list_notifications(
        self, subscriber_id: int, status: str | None
    ) -> list[Notification]:
        return await self._repo.list_notifications(subscriber_id, status)

    async def get_notification(self, notif_id: int) -> Notification:
        notif = await self._repo.get_notification(notif_id)
        if notif is None:
            from app.core.exceptions import NotFoundError
            raise NotFoundError(f"通知不存在: {notif_id}")
        return notif

    async def mark_sent(self, notif_id: int) -> Notification:
        return await self._transition(notif_id, NotifyStatus.SENT.value)

    async def mark_failed(self, notif_id: int) -> Notification:
        return await self._transition(notif_id, NotifyStatus.FAILED.value)

    async def _transition(self, notif_id: int, status: str) -> Notification:
        notif = await self.get_notification(notif_id)
        notif.status = status
        if status == NotifyStatus.SENT.value:
            notif.sent_at = datetime.utcnow()
        await self._repo.commit()
        return notif

    async def upsert_subscription(
        self, data: SubscriptionUpsert, actor_id: int | None = None
    ) -> SubscriptionPref:
        # PLAT-2: 以服务端认证身份 actor_id 覆盖 client 传入的 user_id
        user_id = actor_id if actor_id is not None else data.user_id
        existing = await self._repo.find_subscription(user_id, data.channel, data.event_type)
        if existing is not None:
            existing.enabled = data.enabled
            existing.threshold = data.threshold
            await self._repo.commit()
            return existing
        sub = SubscriptionPref(
            user_id=user_id,
            channel=data.channel,
            event_type=data.event_type,
            enabled=data.enabled,
            threshold=data.threshold,
        )
        return await self._repo.save_subscription(sub)

    async def list_subscriptions(self, user_id: int) -> list[SubscriptionPref]:
        return await self._repo.list_subscriptions(user_id)

    async def list_event_logs(self, event_type: str | None, limit: int) -> list[Any]:
        return await self._repo.list_event_logs(event_type, limit)

    async def close(self) -> None:
        await self._http_client.aclose()
