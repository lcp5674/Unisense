"""通知服务（TD §12.9 / FR-16 / FR-17）。

核心能力：
1. 事件发布（EventLog 留痕）+ 按订阅偏好广播（Notification 扇出）。
2. 通知查询与状态回写（SENT / FAILED）。
3. 订阅偏好 upsert 与查询。
4. 通知外发渠道：SMTP / Webhook（可配置）。

P3: datetime.utcnow() → datetime.now(UTC)。
"""

from __future__ import annotations

import asyncio
import enum
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.config import settings
from app.core.exceptions import AuthError
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

# ---------------------------------------------------------------------------
# 业务化：事件类型 → 中文标题、payload 字段 → 中文标签
# 目标：通知的标题/内容从源头就是用户可读的业务术语，而非英文码 / JSON。
# ---------------------------------------------------------------------------

_EVENT_TITLE_CN: dict[str, str] = {
    "metric.created": "指标创建",
    "metric.submitted": "指标待审核",
    "metric.approved": "指标已通过",
    "metric.rejected": "指标已驳回",
    "metric.deprecated": "指标废弃",
    "metric.promoted": "指标已发布",
    "metric.rolled_back": "指标已回滚",
    "metric.emergency_published": "指标紧急发布",
    "metric.health_critical": "指标健康度严重",
    "conflict_open": "口径冲突待处理",
    "conflict_ruled": "口径冲突已裁决",
    "conflict_escalated": "口径冲突已升级",
    "pii_conflict": "PII 冲突",
    "quality.anomaly": "数据质量异常告警",
    "reconciliation.alert": "对账告警",
    "grant.granted": "权限已授予",
    "grant.revoked": "权限已收回",
    "grant.expired": "权限已过期",
    "benchmark.imported": "参照基准已导入",
    "pii.propagated": "敏感数据已扩散",
    "pii.reviewed": "敏感数据已复核",
    "classification.changed": "数据分类变更",
    "classification.done": "数据分类完成",
    "escalation.triggered": "告警升级已触发",
    # 走 EventBus 的可接入业务事件（TD §5.5 通知闭环）
    "feedback.status_updated": "反馈状态更新",
    "nps.submitted": "满意度已提交",
    "audit.capacity_warning": "审计容量告警",
}

_SOURCE_CN: dict[str, str] = {
    "metric": "指标",
    "lineage": "血缘",
    "quality": "数据质量",
    "governance": "治理合规",
    "semantic": "指标口径",
    "system": "系统",
    "scheduler": "定时任务",
    "conflict": "口径冲突",
    "grant": "权限",
    "pii": "敏感数据",
    "benchmark": "参照基准",
    "orphan": "孤立实体",
    "review": "审核",
}

_ACTION_CN: dict[str, str] = {
    "created": "创建",
    "updated": "更新",
    "published": "发布",
    "submitted": "待审核",
    "approved": "已通过",
    "rejected": "已驳回",
    "deprecated": "废弃",
    "detected": "检测",
    "alert": "告警",
    "open": "待处理",
    "escalated": "升级",
    "imported": "导入",
    "granted": "授予",
    "revoked": "收回",
    "propagated": "扩散",
    "reviewed": "复核",
    "pending": "待办",
    "change": "变更",
    "notice": "公告",
    "anomaly": "异常告警",
}

_PAYLOAD_LABEL: dict[str, str] = {
    "metric_id": "指标ID",
    "metric_code": "指标编码",
    "metric_name": "指标名称",
    "level": "重要程度",
    "severity": "严重级别",
    "rule_type": "规则类型",
    "rule_mode": "规则模式",
    "obs_value": "观测值",
    "threshold": "阈值",
    "window": "统计周期",
    "domain": "业务域",
    "user_id": "用户ID",
    "operator_id": "操作人ID",
    "grant_id": "授权ID",
    "grant_type": "授权类型",
    "expires_at": "到期时间",
    "conflict_id": "冲突编号",
    "note": "说明",
    "reason": "原因",
    "source_table": "源表",
    "target_table": "目标表",
    "pii_columns": "敏感字段",
    "notify_targets": "通知对象",
    "reviewer_id": "审核人ID",
    "reviewer": "审核人",
}

_RULE_TYPE_CN: dict[str, str] = {
    "COMPLETENESS": "完整性",
    "ACCURACY": "准确性",
    "TIMELINESS": "时效性",
    "CONSISTENCY": "一致性",
    "UNIQUENESS": "唯一性",
    "VALIDITY": "有效性",
    "WAVE_DIFF": "波动差异",
    "CROSS_SOURCE": "跨源校验",
}
_RULE_MODE_CN: dict[str, str] = {
    "static": "静态阈值",
    "dynamic_baseline": "动态基线",
    "yoy_woy": "同比环比",
    "cross_source": "跨源对比",
}
_GRANT_TYPE_CN: dict[str, str] = {"READ": "只读", "WRITE": "可写", "READ_WRITE": "读写"}
_LEVEL_CN: dict[str, str] = {
    "P0": "严重",
    "P1": "高",
    "P2": "中",
    "INFO": "提示",
    "WARN": "警告",
    "ERROR": "错误",
    "CRITICAL": "严重",
}
# 内部/冗余字段，正文中不展示（event_type 已体现在标题）
_SKIP_FIELDS = {"event_type", "payload"}


def _humanize_event_title(event_type: str) -> str:
    """事件类型英文码 → 业务标题（已知映射优先，未知按 ``域.动作`` 拆词兜底）。"""
    if not event_type:
        return "系统通知"
    title = _EVENT_TITLE_CN.get(event_type)
    if title:
        return title
    if "." in event_type:
        source, _, action = event_type.partition(".")
        src_cn = _SOURCE_CN.get(source, source)
        act_cn = _ACTION_CN.get(action, action)
        return f"{src_cn} · {act_cn}"
    return event_type


def _humanize_value(key: str, value: Any) -> str:
    """单个 payload 字段值 → 业务可读中文。"""
    if value is None:
        return "无"
    if isinstance(value, bool):
        return "是" if value else "否"
    if key == "level":
        return _LEVEL_CN.get(str(value), str(value))
    if key == "rule_type":
        return _RULE_TYPE_CN.get(str(value), str(value))
    if key == "rule_mode":
        return _RULE_MODE_CN.get(str(value), str(value))
    if key == "grant_type":
        return _GRANT_TYPE_CN.get(str(value), str(value))
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _humanize_payload(payload: dict[str, Any] | None) -> str | None:
    """把通知 payload（JSON）渲染成人类可读的多行文本。

    - 已知字段用中文标签（``_PAYLOAD_LABEL``），未知字段用字段名原样展示；
    - 枚举值（level/rule_type/rule_mode/grant_type）转中文；
    - 事件总线包装的 ``{"payload": {...}}`` 会展开内层；
    - 空 payload 返回 None（不产生空正文）。
    """
    if not payload:
        return None
    inner = payload.get("payload")
    data: dict[str, Any] = inner if isinstance(inner, dict) else payload
    lines: list[str] = []
    for key, value in data.items():
        if key in _SKIP_FIELDS:
            continue
        if key == "payload" and isinstance(value, dict):
            for sub_key, sub_value in value.items():
                lines.append(
                    f"{_PAYLOAD_LABEL.get(sub_key, sub_key)}：{_humanize_value(sub_key, sub_value)}"
                )
            continue
        lines.append(f"{_PAYLOAD_LABEL.get(key, key)}：{_humanize_value(key, value)}")
    return "\n".join(lines) if lines else None


# 通知外发 HTTP 客户端共享单例：避免按请求实例化导致连接池/文件描述符泄漏。
_HTTP_CLIENT: httpx.AsyncClient | None = None

# 投递重试参数：瞬时故障（网络抖动/网关超时）下退避重试，避免偶发失败即丢通知。
# 重试仅针对传输层异常（httpx.HTTPError / SMTPException），4xx/5xx 响应视为终态不重试。
_DELIVERY_MAX_ATTEMPTS = 3
_DELIVERY_BACKOFF_BASE = 0.2  # 秒，指数退避基数
# SMTP 单次投递超时（避免 aiosmtplib 无超时导致协程永久挂起阻塞 fan-out）
_SMTP_TIMEOUT = 10


def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(timeout=10.0)
    return _HTTP_CLIENT


async def _deliver_with_retry(
    send: Callable[[], Awaitable[bool]],
    *,
    operation: str,
    retry_on: tuple[type[Exception], ...],
) -> bool:
    """投递重试包装：对传输层瞬时异常退避重试，终态返回/明确失败不重试。

    Args:
        send: 执行单次投递的协程工厂，返回 True 表示成功（不重试）。
        operation: 渠道名（日志用）。
        retry_on: 触发重试的异常类型（仅传输层，不含业务终态）。
    """
    last_exc: Exception | None = None
    for attempt in range(1, _DELIVERY_MAX_ATTEMPTS + 1):
        try:
            # 业务终态（如 4xx/5xx 响应、渠道未配置）不重试：直接返回投递结果
            return await send()
        except retry_on as exc:
            last_exc = exc
            logger.warning(
                "notify_delivery_retryable",
                operation=operation,
                attempt=attempt,
                error=str(exc),
            )
            if attempt < _DELIVERY_MAX_ATTEMPTS:
                await asyncio.sleep(_DELIVERY_BACKOFF_BASE * (2 ** (attempt - 1)))
    logger.error(
        "notify_delivery_exhausted",
        operation=operation,
        error=str(last_exc),
    )
    return False


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
                title=_humanize_event_title(data.event_type),
                body=_humanize_payload(data.payload),
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
            payload = {k: v for k, v in event.items() if k not in ("event_type", "actor_id")}
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
        - in_app: 站内信（通知已写入 notification 表、用户可见即视为送达）
        - sms: 短信（网关未配置时降级为 SENT，不误标 FAILED）
        - webhook: HTTP POST 到配置的 URL
        - email: SMTP 发送
        - dingtalk: 钉钉 Webhook 机器人
        - console: 日志输出（开发环境）

        channel 大小写归一化：DB 中 EMAIL/SMS/WEBHOOK/IN_APP/DINGTALK 为大写枚举值，
        console 为小写值；统一转小写比较，避免大小写漂移导致渠道永远无法命中。
        """
        channel_key = (channel or "").strip().lower()
        try:
            if channel_key == "in_app":
                # 入站即达：notification 记录已持久化，用户登录即可见，视为送达
                logger.info("通知（in_app）: %s", notif.title)
                return True
            elif channel_key == "sms":
                # SMS 渠道无短信网关实现，明确降级为 SENT（不误标 FAILED）
                logger.warning("SMS 渠道未配置网关，降级为站内已送达: %s", notif.title)
                return True
            elif channel_key == "webhook":
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
        """Webhook 投递：POST 到配置的 webhook URL（传输层异常退避重试）。"""
        webhook_url = settings.notify_webhook_url
        if not webhook_url:
            logger.warning("未配置 notify_webhook_url，跳过 webhook 投递")
            return False

        async def _send() -> bool:
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
            # 4xx/5xx 为业务终态，不重试（由 _deliver_with_retry 直接返回 False）
            return resp.status_code < 300

        return await _deliver_with_retry(
            _send,
            operation="webhook",
            retry_on=(httpx.HTTPError,),
        )

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

        async def _send() -> bool:
            resp = await self._http_client.post(
                webhook_url,
                json=message_body,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code >= 300:
                logger.error(
                    "dingtalk_dispatch_failed",
                    notif_id=notif.id,
                    status=resp.status_code,
                    body=resp.text[:500],
                )
                return False
            logger.info("dingtalk_dispatch_ok", notif_id=notif.id, status=resp.status_code)
            return True

        return await _deliver_with_retry(
            _send,
            operation="dingtalk",
            retry_on=(httpx.HTTPError,),
        )

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
            # 真实收件人：按订阅人 ID 解析其注册邮箱；解析失败/缺邮箱时不投递
            # （D3：不得回退到发件人/占位地址并标记 SENT——真实收件人永远收不到，
            # 且通知被错误标记为已送达）。
            recipient = None
            if notif.subscriber_id:
                try:
                    resolved = await self._repo.get_user_email(notif.subscriber_id)
                    if isinstance(resolved, str) and resolved:
                        recipient = resolved
                except Exception as exc:  # noqa: BLE001 - 收件人解析失败按缺收件人处理
                    logger.warning(
                        "notify_resolve_recipient_failed",
                        notif_id=notif.id,
                        error=str(exc),
                    )
            if not recipient:
                logger.warning(
                    "notify_email_no_recipient",
                    notif_id=notif.id,
                    subscriber_id=notif.subscriber_id,
                )
                return False
            msg["To"] = recipient

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

            async def _send() -> bool:
                # SMTP 单次投递超时（防无超时协程永久挂起阻塞 fan-out）
                await aiosmtplib.send(
                    msg,
                    hostname=smtp_host,
                    port=smtp_port,
                    username=smtp_user or None,
                    password=smtp_password or None,
                    use_tls=smtp_port == 587,
                    timeout=_SMTP_TIMEOUT,
                )
                logger.info("email_dispatch_ok", notif_id=notif.id)
                return True

            return await _deliver_with_retry(
                _send,
                operation="email",
                retry_on=(aiosmtplib.SMTPException,),
            )
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

    async def list_notifications_page(
        self,
        subscriber_id: int,
        status: str | None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Notification], int]:
        return await self._repo.list_notifications_page(subscriber_id, status, page, page_size)

    async def get_notification(self, notif_id: int) -> Notification:
        notif = await self._repo.get_notification(notif_id)
        if notif is None:
            from app.core.exceptions import NotFoundError

            raise NotFoundError(f"通知不存在: {notif_id}")
        return notif

    async def mark_sent(self, notif_id: int, actor_id: int, role: str = "") -> Notification:
        return await self._transition(notif_id, NotifyStatus.SENT.value, actor_id, role)

    async def mark_failed(self, notif_id: int, actor_id: int, role: str = "") -> Notification:
        return await self._transition(notif_id, NotifyStatus.FAILED.value, actor_id, role)

    async def mark_read(self, notif_id: int, actor_id: int, role: str = "") -> Notification:
        """单条通知标记已读（幂等：已读不再覆写时间）。"""
        notif = await self.get_notification(notif_id)
        self._assert_owner(notif, actor_id, role)
        if notif.read_at is None:
            notif.read_at = datetime.now(UTC)
        await self._repo.commit()
        return notif

    async def mark_all_read(self, actor_id: int) -> int:
        """当前用户全部通知标记已读，返回更新条数。"""
        return await self._repo.mark_all_read(actor_id)

    async def delete_notification(
        self, notif_id: int, actor_id: int, role: str = ""
    ) -> None:
        """删除单条通知（物理删除；仅通知归属者本人或平台管理员可操作）。"""
        notif = await self.get_notification(notif_id)
        self._assert_owner(notif, actor_id, role)
        await self._repo.delete_notification(notif)
        await self._repo.commit()

    async def delete_all(self, actor_id: int) -> int:
        """当前用户清空全部通知（按 subscriber 限定，天然隔离），返回删除条数。"""
        return await self._repo.delete_all(actor_id)

    def _assert_owner(self, notif: Notification, actor_id: int, role: str = "") -> None:
        """IDOR 防护：仅通知归属者本人或平台管理员可操作，其余角色一律拒绝。"""
        role_val = role.value if isinstance(role, enum.Enum) else str(role or "")
        if not (role_val == "platform_admin" or notif.subscriber_id == actor_id):
            raise AuthError(
                "无权修改他人通知状态",
                error_code="FORBIDDEN",
                ctx={"notif_id": notif.id, "actor_id": actor_id, "owner_id": notif.subscriber_id},
            )

    async def _transition(
        self, notif_id: int, status: str, actor_id: int, role: str = ""
    ) -> Notification:
        notif = await self.get_notification(notif_id)
        self._assert_owner(notif, actor_id, role)
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
