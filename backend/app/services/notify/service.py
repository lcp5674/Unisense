"""通知服务（TD §12.9 / FR-16 / FR-17）。

核心能力：
1. 事件发布（EventLog 留痕）+ 按订阅偏好广播（Notification 扇出）。
2. 通知查询与状态回写（SENT / FAILED）。
3. 订阅偏好 upsert 与查询。
4. 通知外发渠道：SMTP / Webhook（可配置）。

P3: datetime.utcnow() → datetime.now(UTC)。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.config import settings
from app.core.logging import get_logger
from app.models.notify import (
    EventLevel,
    EventLog,
    Notification,
    NotifyStatus,
    SubscriptionPref,
)
from app.services.notify.repository import NotifyRepository
from app.services.notify.schemas import (
    _ALLOWED_LEVELS,
    _ALLOWED_SOURCES,
    EventPublish,
    SubscriptionUpsert,
)

logger = get_logger(__name__)

# 通知外发 HTTP 客户端共享单例：避免按请求实例化导致连接池/文件描述符泄漏。
_HTTP_CLIENT: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(timeout=10.0)
    return _HTTP_CLIENT


class NotifyService(BaseService):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._session = session
        self._repo = NotifyRepository(session)
        self._http_client = _get_http_client()

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
                notif.sent_at = datetime.now(UTC)
                delivered += 1
        event.notified = delivered > 0
        await self._repo.commit()
        return {"event_id": event.id, "notifications": created, "delivered": delivered}

    async def handle_business_event(self, event: dict[str, Any]) -> dict[str, int]:
        """消费 EventBus 业务事件（quality/conflict/governance），落 EventLog 并按订阅扇出投递。

        事件格式兼容 EventBus.publish 的 ``{event_type, payload, actor_id}`` 与业务直发的
        扁平 ``{event_type, ...}``。source 按事件前缀映射（conflict → semantic），
        level 从 payload 提取并做白名单收敛；任何失败仅记日志，不阻断业务主流程（best-effort）。
        """
        event_type = str(event.get("event_type") or "")
        if not event_type:
            return {"event_id": 0, "notifications": 0, "delivered": 0}
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {
                k: v for k, v in event.items() if k not in ("event_type", "actor_id")
            }
        level = str(payload.get("level") or "INFO").upper()
        if level not in _ALLOWED_LEVELS:
            level = "INFO"
        source = event_type.split(".", 1)[0]
        if source == "conflict" or event_type.startswith("conflict_"):
            # 白名单无 conflict，语义域承载冲突事件（兼容 conflict_open 等下划线命名）
            source = "semantic"
        if source not in _ALLOWED_SOURCES:
            source = "system"
        try:
            return await self.publish_event(
                EventPublish(
                    event_type=event_type,
                    source=source,
                    payload=payload or None,
                    level=level,
                )
            )
        except Exception as exc:  # noqa: BLE001 - best-effort 不阻断业务
            logger.error("业务事件处理失败（best-effort 跳过）: %s", exc)
            return {"event_id": 0, "notifications": 0, "delivered": 0}

    async def _dispatch(self, notif: Notification, channel: str) -> bool:
        """投递通知到指定渠道。

        支持渠道：
        - webhook: HTTP POST 到配置的 URL
        - email: SMTP 发送
        - dingtalk: 钉钉 Webhook 机器人
        - console: 日志输出（开发环境）

        channel 大小写归一化：DB 中 EMAIL/SMS/WEBHOOK/IN_APP/DINGTALK 为大写枚举值，
        console 为小写值；统一转小写比较，避免大小写漂移导致渠道永远无法命中。
        """
        channel_key = (channel or "").strip().lower()
        try:
            if channel_key == "webhook":
                return await self._dispatch_webhook(notif)
            elif channel_key == "email":
                return await self._dispatch_email(notif)
            elif channel_key == "dingtalk":
                return await self._dispatch_dingtalk(notif)
            elif channel_key == "console":
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
                    "sent_at": datetime.now(UTC).isoformat(),
                },
                headers={"Content-Type": "application/json"},
            )
            return resp.status_code < 300
        except Exception as exc:
            logger.error("Webhook 投递失败: %s", exc)
            return False

    async def _dispatch_dingtalk(self, notif: Notification) -> bool:
        """钉钉 Webhook 投递：POST 到配置的钉钉机器人 Webhook URL。

        消息模板根据事件类型选择：
        - 质量异常：告警卡片样式
        - 审核待办：待办提醒样式
        - 冲突升级：紧急提醒样式
        - 默认：文本消息
        """
        webhook_url = settings.notify_dingtalk_webhook
        if not webhook_url:
            logger.warning("未配置 UNISENSE_NOTIFY_DINGTALK_WEBHOOK，跳过钉钉投递")
            return False

        # 构建钉钉消息体
        event_type = notif.template_code or ""
        title = notif.title or "Unisense 通知"

        if "quality" in event_type or "anomaly" in event_type:
            # 质量异常告警
            message_body: dict[str, Any] = {
                "msgtype": "markdown",
                "markdown": {
                    "title": f"【质量异常告警】{title}",
                    "text": (
                        f"### 质量异常告警\n\n"
                        f"**事件类型**：{event_type}\n\n"
                        f"**详情**：{notif.body or '无'}\n\n"
                        f"**时间**：{datetime.now(UTC).isoformat()}\n\n"
                        f"> 请及时处理"
                    ),
                },
            }
        elif "review" in event_type or "pending" in event_type:
            # 审核待办
            message_body = {
                "msgtype": "markdown",
                "markdown": {
                    "title": f"【审核待办】{title}",
                    "text": (
                        f"### 审核待办提醒\n\n"
                        f"**事件类型**：{event_type}\n\n"
                        f"**详情**：{notif.body or '无'}\n\n"
                        f"**时间**：{datetime.now(UTC).isoformat()}\n\n"
                        f"> 请尽快审核"
                    ),
                },
            }
        elif "conflict" in event_type or "escalate" in event_type:
            # 冲突升级
            message_body = {
                "msgtype": "markdown",
                "markdown": {
                    "title": f"【冲突升级】{title}",
                    "text": (
                        f"### 冲突升级紧急提醒\n\n"
                        f"**事件类型**：{event_type}\n\n"
                        f"**详情**：{notif.body or '无'}\n\n"
                        f"**时间**：{datetime.now(UTC).isoformat()}\n\n"
                        f"> 需要立即处理"
                    ),
                },
            }
        else:
            # 默认文本消息
            message_body = {
                "msgtype": "text",
                "text": {
                    "content": f"【Unisense通知】{title}\n{notif.body or ''}",
                },
            }

        try:
            resp = await self._http_client.post(
                webhook_url,
                json=message_body,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code < 300:
                logger.info("dingtalk_dispatch_ok", notif_id=notif.id, status=resp.status_code)
                return True
            else:
                logger.error(
                    "dingtalk_dispatch_failed",
                    notif_id=notif.id,
                    status=resp.status_code,
                    body=resp.text[:500],
                )
                return False
        except Exception as exc:
            logger.error("钉钉 Webhook 投递失败: %s", exc)
            return False

    async def _dispatch_email(self, notif: Notification) -> bool:
        """邮件投递：通过 aiosmtplib 发送 SMTP 邮件。

        使用 settings.notify_smtp_* 配置，发送 HTML 格式邮件。
        """
        smtp_host = settings.notify_smtp_host
        if not smtp_host:
            logger.warning("未配置 UNISENSE_NOTIFY_SMTP_HOST，跳过邮件投递")
            return False

        try:
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            import aiosmtplib

            smtp_port = settings.notify_smtp_port
            smtp_user = settings.notify_smtp_user
            smtp_password = settings.notify_smtp_password

            # 构建邮件
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"[Unisense] {notif.title or '通知'}"
            msg["From"] = smtp_user or "unisense-noreply@unisense.local"
            msg["To"] = (
                smtp_user or "admin@unisense.local"
            )  # Placeholder; real impl uses subscriber email

            event_type = notif.template_code or ""
            # HTML 邮件模板
            html_body = (
                "<div style='font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;'>"
                "<div style='background: #1890ff; color: white;"
                " padding: 16px; border-radius: 8px 8px 0 0;'>"
                f"<h2 style='margin: 0;'>{notif.title or 'Unisense 通知'}</h2>"
                "</div>"
                "<div style='padding: 16px; border: 1px solid #e8e8e8; border-top: none;'>"
                f"<p><strong>事件类型：</strong>{event_type}</p>"
                f"<p><strong>详情：</strong>{notif.body or '无'}</p>"
                f"<p><strong>时间：</strong>{datetime.now(UTC).isoformat()}</p>"
                "<hr style='border: none; border-top: 1px solid #e8e8e8; margin: 16px 0;'/>"
                "<p style='color: #999; font-size: 12px;'>"
                "此邮件由 Unisense 指标语义中台自动发送</p>"
                "</div></div>"
            )
            text_body = (
                f"{notif.title or 'Unisense 通知'}\n\n"
                f"事件类型: {event_type}\n"
                f"详情: {notif.body or '无'}\n"
                f"时间: {datetime.now(UTC).isoformat()}"
            )

            msg.attach(MIMEText(text_body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            # SMTP 发送
            await aiosmtplib.send(
                msg,
                hostname=smtp_host,
                port=smtp_port,
                username=smtp_user or None,
                password=smtp_password or None,
                use_tls=smtp_port == 587,
            )
            logger.info("email_dispatch_ok", notif_id=notif.id)
            return True
        except ImportError:
            logger.warning("aiosmtplib 未安装，跳过邮件投递")
            return False
        except Exception as exc:
            logger.error("邮件投递失败: %s", exc)
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
            notif.sent_at = datetime.now(UTC)
        await self._repo.commit()
        return notif

    async def upsert_subscription(
        self, data: SubscriptionUpsert, actor_id: int | None = None
    ) -> SubscriptionPref:
        # PLAT-2: 以服务端认证身份 actor_id 覆盖 client 传入的 user_id
        user_id = actor_id if actor_id is not None else data.user_id
        if user_id is None:
            raise ValueError("user_id 缺失：服务端认证身份与请求体均未提供")
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
        """关闭共享 HTTP 客户端（应用关停时调用一次即可，幂等）。"""
        global _HTTP_CLIENT
        if _HTTP_CLIENT is not None:
            await _HTTP_CLIENT.aclose()
            _HTTP_CLIENT = None
