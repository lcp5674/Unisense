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
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import AuthError, BusinessError, UnisenseError
from app.core.logging import get_logger
from app.models.data_source import DBCatalog
from app.models.metric import Metric
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
    "metric.resubmitted": "指标重评审待审核",
    "metric.approved": "指标已通过",
    "metric.gray_published": "指标灰度发布",
    "metric.rejected": "指标已驳回",
    "metric.deprecated": "指标废弃",
    "metric.voided": "指标作废",
    "metric.promoted": "指标已发布",
    "metric.rolled_back": "指标已回滚",
    "metric.emergency_published": "指标紧急发布",
    "metric.emergency_reviewed": "紧急发布已补审",
    "metric.health_critical": "指标健康度严重",
    # C1（第七轮）：reactivate_metric 发布 metric.reactivated，标题映射补齐（订阅扇出可见）
    "metric.reactivated": "指标已重新启用",
    "metric.gray_recycled": "灰度超期已回收",
    "metric.source_dropped": "指标数据源已下线",
    "metric.source_recovered": "指标数据源已恢复",
    "metric.rename_required": "指标需要改名",
    "metric.breaking_change_pending": "指标口径变更待确认",
    "metric.breaking_change_promoted": "指标口径变更已生效",
    "conflict_open": "口径冲突待处理",
    "conflict_ruled": "口径冲突已裁决",
    "conflict_escalated": "口径冲突已升级",
    "pii_conflict": "PII 冲突",
    "quality.anomaly": "数据质量异常告警",
    "reconciliation.alert": "对账告警",
    "grant.granted": "权限已授予",
    "grant.revoked": "权限已收回",
    "grant.expired": "权限已过期",
    "grant.expiring_soon": "权限即将到期",
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
    # 采集/血缘断链修复（collector/lineage 双发 EventBus，TD §5.5）
    "catalog_registered": "数据目录已注册",
    "catalog_schema_drifted": "目录 Schema 漂移",
    "lineage_parsed": "血缘已解析",
    "lineage_ingested": "血缘已接入",
    # DDL 变更事件化（lineage/service.py 定向通知受影响资产 Owner 的模板标题）
    "lineage.ddl_changed": "血缘变更影响",
    # 采集定向通知（collector/service.py 经 notify_user 直发源 Owner）
    "catalog.deprecated": "数据目录已废弃",
    "collect.degraded": "采集降级",
    # 血缘变更影响闭环（semantic/service.py 经 notify_user 定向通知受影响指标 Owner）
    "lineage.change_impacted": "血缘变更影响",
    # 指标血缘注册失败（metrics.py/semantic/service.py best-effort 路径，C7 告警）
    "lineage.metric_register_failed": "指标血缘注册失败",
    # 核心依赖降级（core/degradation.py，运维感知全平台健康）
    "degradation.state_changed": "系统依赖状态变更",
    # 冲突重开（conflict/service.py 经 _safe_publish 发布）
    "conflict_reopened": "口径冲突已重开",
    # 账号安全/组织（users.py/organizations.py 经 notify_user 定向通知）
    "user.created": "账号已创建",
    "user.status_changed": "账号状态变更",
    "user.password_reset": "密码已重置",
    "org.status_changed": "组织状态变更",
    # 采集异常定向（collector 源 Owner）
    "collect.failed": "采集任务失败",
    "catalog.connection_failed": "数据源连接失败",
    # PII 复核待办（catalog 标 NEEDS_REVIEW 定向 compliance_officer）
    "pii.review_pending": "PII 复核待办",
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
    # 三梯队通知接入新增来源
    "catalog": "数据目录",
    "collect": "采集",
    "user": "账号",
    "org": "组织",
    "degradation": "系统依赖",
    "dict": "系统字典",
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
    "dict_type": "字典类型",
    "value": "未收录值",
    "value_key": "值指纹",
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
_SKIP_FIELDS = {"event_type", "payload", "recipient_user_id"}


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
# 去重防风暴窗口（秒）：同类型通知在窗口内对同一订阅人只保留一条。
# 采集降级/质量告警等高频事件反复触发时，避免逐条刷屏（TD §12.9 通知风暴治理）。
_DEDUP_WINDOW_SECONDS = 60
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
        # 操作人姓名快照：一次解析复用（EventLog + 全部 Notification），
        # 避免每个通知重复反查用户表。actor_id 缺失（系统/定时任务）时为 None。
        actor_id = data.actor_id
        actor_name = data.actor_name
        if actor_id is not None and not actor_name:
            actor_name = await self._repo.get_user_display_name(actor_id)
        event = EventLog(
            event_type=data.event_type,
            source=data.source,
            payload=data.payload,
            level=data.level if data.level else EventLevel.INFO.value,
            notified=False,
            actor_id=actor_id,
            actor_name=actor_name,
        )
        await self._repo.save_event(event)
        subs = await self._repo.list_enabled_subscriptions(data.event_type)
        # P2 资产订阅（按指标/源表 watch）：事件 payload 携带资产引用时，
        # 额外匹配"关注了该资产"的用户，与事件订阅者合并（同用户×渠道去重，
        # 事件订阅优先，避免同一通知重复投递）。
        # best-effort：资产匹配失败（repo 异常/数据异常）仅告警，绝不阻断事件订阅扇出。
        asset_subs: list[SubscriptionPref] = []
        try:
            for asset_type, asset_id in self._extract_asset_keys(data.payload):
                asset_subs.extend(await self._repo.list_asset_subscribers(asset_type, asset_id))
        except Exception:
            logger.warning("notify_asset_subscriber_match_failed", exc_info=True)
            asset_subs = []
        if asset_subs:
            merged: dict[tuple[int, str], SubscriptionPref] = {}
            for s in subs:
                merged[(s.user_id, s.channel)] = s
            for s in asset_subs:
                merged.setdefault((s.user_id, s.channel), s)
            subs = list(merged.values())
        created = 0
        delivered = 0
        for sub in subs:
            # 去重防风暴：窗口内已存在同类型未处理通知则跳过（高频事件只保留一条）
            recent = await self._repo.find_recent_notification(
                sub.user_id, data.event_type, _DEDUP_WINDOW_SECONDS
            )
            if recent is not None:
                logger.info(
                    "notify_dedup_skipped",
                    subscriber_id=sub.user_id,
                    event_type=data.event_type,
                    recent_id=recent.id,
                )
                continue
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
                actor_id=actor_id,
                actor_name=actor_name,
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
        # 指定接收人定向投递（如反馈提交者）：不依赖订阅，额外通知该用户。
        # 已通过订阅收到通知的用户不重复发。
        recipient_raw = (data.payload or {}).get("recipient_user_id")
        if recipient_raw is not None:
            try:
                recipient_id = int(recipient_raw)
            except (TypeError, ValueError):
                recipient_id = 0
            subscriber_ids = {s.user_id for s in subs}
            if recipient_id and recipient_id not in subscriber_ids:
                # 定向投递同样受去重窗口约束：窗口内已收到同类型通知则不重复打扰
                recent = await self._repo.find_recent_notification(
                    recipient_id, data.event_type, _DEDUP_WINDOW_SECONDS
                )
                if recent is not None:
                    logger.info(
                        "notify_dedup_skipped_recipient",
                        subscriber_id=recipient_id,
                        event_type=data.event_type,
                        recent_id=recent.id,
                    )
                else:
                    notif = Notification(
                        subscriber_id=recipient_id,
                        channel="in_app",
                        template_code=data.event_type,
                        title=_humanize_event_title(data.event_type),
                        body=_humanize_payload(data.payload),
                        payload=data.payload,
                        status=NotifyStatus.PENDING.value,
                        ref_type="event",
                        ref_id=event.id,
                        actor_id=actor_id,
                        actor_name=actor_name,
                    )
                    await self._repo.save_notification(notif)
                    created += 1
                    ok = await self._dispatch(notif, "in_app")
                    notif.status = NotifyStatus.SENT.value if ok else NotifyStatus.FAILED.value
                    if ok:
                        notif.sent_at = datetime.now(UTC)
                        delivered += 1
        await self._repo.commit()
        return {"event_id": event.id, "notifications": created, "delivered": delivered}

    @staticmethod
    def _extract_asset_keys(payload: dict[str, Any] | None) -> list[tuple[str, str]]:
        """从事件 payload 提取资产引用键（(type, id) 列表），供资产订阅匹配。

        支持（优先级从高到低）：
        - ``asset_keys``：显式多资产列表（[{"type": "TABLE", "id": "db.t"}...]，DDL 影响多表）；
        - ``asset_type`` + ``asset_id``：显式单资产；
        - ``metric_code`` → (METRIC, code)、``table``/``table_name`` → (TABLE, name) 常见单键。
        无法识别时返回空列表（事件仍按 event_type 订阅匹配，行为不变）。
        """
        if not payload:
            return []
        keys: list[tuple[str, str]] = []
        raw_keys = payload.get("asset_keys")
        if isinstance(raw_keys, list):
            for item in raw_keys:
                if isinstance(item, dict) and item.get("type") and item.get("id"):
                    keys.append((str(item["type"]).upper(), str(item["id"])))
        if payload.get("asset_type") and payload.get("asset_id"):
            keys.append((str(payload["asset_type"]).upper(), str(payload["asset_id"])))
        if payload.get("metric_code"):
            keys.append(("METRIC", str(payload["metric_code"])))
        table = payload.get("table") or payload.get("table_name")
        if table:
            keys.append(("TABLE", str(table)))
        seen: set[tuple[str, str]] = set()
        return [k for k in keys if not (k in seen or seen.add(k))]

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
        # 操作人：EventBus 事件顶层携带 actor_id（{event_type, payload, actor_id}），
        # 补齐「谁发起的操作」审计语义；缺失（系统/定时任务）时为 None。
        try:
            actor_id = int(event.get("actor_id") or 0) or None
        except (TypeError, ValueError):
            actor_id = None
        try:
            return await self.publish_event(
                EventPublish(
                    event_type=event_type,
                    source=source,
                    payload=payload or None,
                    level=level,
                    actor_id=actor_id,
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
                notif.last_error = f"未知通知渠道: {channel}"
                return False
        except Exception as exc:  # noqa: BLE001
            # 记录失败原因供 FAILED 卡片展示与运营定位（重试成功后由 retry_delivery 清空）
            notif.last_error = str(exc)[:500]
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

    async def notify_user(
        self,
        user_id: int,
        event_type: str,
        title: str,
        body: str | None = None,
        payload: dict[str, Any] | None = None,
        *,
        channel: str = "IN_APP",
    ) -> Notification:
        """定向通知指定用户（不依赖订阅偏好）：直接为该用户创建并投递通知。

        与 ``publish_event``（按订阅扇出）不同，本方法面向"必须送达特定角色/Owner"
        的场景（如冲突仲裁要求指标 Owner 改名）——订阅偏好缺省时也会送达，
        保证关键治理动作不被订阅遗漏。

        Args:
            user_id: 目标用户 ID（订阅人）。
            event_type: 事件类型（模板编码）。
            title: 通知标题（业务可读）。
            body: 正文（可选）。
            payload: 扩展负载（可选）。
            channel: 通知渠道，默认站内信（IN_APP，入站即达）。

        Returns:
            已创建的 Notification。
        """
        notif = Notification(
            subscriber_id=user_id,
            channel=(channel or "IN_APP").strip().lower(),
            template_code=event_type,
            title=title,
            body=body,
            payload=payload,
            status=NotifyStatus.PENDING.value,
            ref_type="event",
        )
        await self._repo.save_notification(notif)
        ok = await self._dispatch(notif, notif.channel)
        notif.status = NotifyStatus.SENT.value if ok else NotifyStatus.FAILED.value
        if ok:
            notif.sent_at = datetime.now(UTC)
        await self._repo.commit()
        return notif

    async def list_notifications(
        self, subscriber_id: int, status: str | None
    ) -> list[Notification]:
        return await self._repo.list_notifications(subscriber_id, status)

    async def list_notifications_page(
        self,
        subscriber_id: int,
        status: str | None,
        read_state: str | None = None,
        template_code: str | None = None,
        todo_only: bool = False,
        days: int | None = None,
        page: int = 1,
        page_size: int = 20,
        object_key: str | None = None,
    ) -> tuple[list[Notification], int]:
        return await self._repo.list_notifications_page(
            subscriber_id,
            status,
            read_state,
            template_code,
            todo_only,
            days,
            page,
            page_size,
            object_key,
        )

    async def get_notification(self, notif_id: int) -> Notification:
        notif = await self._repo.get_notification(notif_id)
        if notif is None:
            from app.core.exceptions import NotFoundError

            raise NotFoundError(f"通知不存在: {notif_id}")
        return notif

    async def mark_sent(
        self, notif_id: int, actor_id: int, role: str = "", roles: list[str] | None = None
    ) -> Notification:
        return await self._transition(notif_id, NotifyStatus.SENT.value, actor_id, role, roles)

    async def mark_failed(
        self, notif_id: int, actor_id: int, role: str = "", roles: list[str] | None = None
    ) -> Notification:
        return await self._transition(notif_id, NotifyStatus.FAILED.value, actor_id, role, roles)

    async def retry_delivery(
        self, notif_id: int, actor_id: int, role: str = "", roles: list[str] | None = None
    ) -> Notification:
        """重试投递失败的站内通知（送达失败处置）。

        仅 FAILED 状态可重试（PENDING/SENT 重试无意义）；按存储的渠道与 payload
        重新投递，成功 → SENT + sent_at + 清空 last_error，失败 → 保持 FAILED + 更新原因。
        """
        notif = await self.get_notification(notif_id)
        self._assert_owner(notif, actor_id, role, roles)
        if notif.status != NotifyStatus.FAILED.value:
            raise UnisenseError(
                f"仅发送失败的通知可重试（当前 {notif.status}）",
                error_code="INVALID_TRANSITION",
                ctx={"notif_id": notif.id, "status": notif.status},
            )
        ok = await self._dispatch(notif, notif.channel)
        notif.status = NotifyStatus.SENT.value if ok else NotifyStatus.FAILED.value
        if ok:
            notif.sent_at = datetime.now(UTC)
            notif.last_error = None
        else:
            # 失败原因已由 _dispatch 写入 last_error（渠道未配置/HTTP 状态/异常）
            logger.error(
                "notify_retry_failed",
                notif_id=notif.id,
                channel=notif.channel,
                error=notif.last_error,
            )
        await self._repo.commit()
        return notif

    async def mark_handled(
        self, notif_id: int, actor_id: int, role: str = "", roles: list[str] | None = None
    ) -> Notification:
        """标记待办类通知为「已处理」（待办闭环）。

        用户点「去仲裁/去审批」等行动按钮处理完成后，回标记办结——通知不再出现在
        「仅待处理」筛选，避免处理完仍提示待办的快照残留。
        """
        notif = await self.get_notification(notif_id)
        self._assert_owner(notif, actor_id, role, roles)
        if notif.handled_at is None:
            notif.handled_at = datetime.now(UTC)
        await self._repo.commit()
        return notif

    async def mark_read(
        self, notif_id: int, actor_id: int, role: str = "", roles: list[str] | None = None
    ) -> Notification:
        """单条通知标记已读（幂等：已读不再覆写时间）。"""
        notif = await self.get_notification(notif_id)
        self._assert_owner(notif, actor_id, role, roles)
        if notif.read_at is None:
            notif.read_at = datetime.now(UTC)
        await self._repo.commit()
        return notif

    async def mark_all_read(self, actor_id: int) -> int:
        """当前用户全部通知标记已读，返回更新条数。"""
        return await self._repo.mark_all_read(actor_id)

    async def unread_count(self, actor_id: int) -> int:
        """当前用户未读通知总数（全局角标，精确计数而非列表近似）。"""
        return await self._repo.count_unread(actor_id)

    async def delete_notification(
        self, notif_id: int, actor_id: int, role: str = "", roles: list[str] | None = None
    ) -> None:
        """删除单条通知（物理删除；仅通知归属者本人或平台管理员可操作）。"""
        notif = await self.get_notification(notif_id)
        self._assert_owner(notif, actor_id, role, roles)
        await self._repo.delete_notification(notif)
        await self._repo.commit()

    async def delete_all(self, actor_id: int) -> int:
        """当前用户清空全部通知（按 subscriber 限定，天然隔离），返回删除条数。"""
        return await self._repo.delete_all(actor_id)

    def _assert_owner(
        self,
        notif: Notification,
        actor_id: int,
        role: str = "",
        roles: list[str] | None = None,
    ) -> None:
        """IDOR 防护：仅通知归属者本人或平台管理员可操作，其余角色一律拒绝。

        方案 A 多角色：``roles`` 携带用户全部角色（主角色 + user_role 扩展），
        任一角色为 platform_admin 即豁免；缺省回退主角色（``role``）。
        """
        all_roles = roles or ([str(role)] if role else [])
        if "platform_admin" in all_roles or notif.subscriber_id == actor_id:
            return
        raise AuthError(
            "无权修改他人通知状态",
            error_code="FORBIDDEN",
            ctx={"notif_id": notif.id, "actor_id": actor_id, "owner_id": notif.subscriber_id},
        )

    async def _transition(
        self,
        notif_id: int,
        status: str,
        actor_id: int,
        role: str = "",
        roles: list[str] | None = None,
    ) -> Notification:
        notif = await self.get_notification(notif_id)
        self._assert_owner(notif, actor_id, role, roles)
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
        is_asset = bool(data.asset_type)
        if is_asset:
            # 资产订阅：event_type 与 asset 二选一（资产行 event_type 置 NULL）
            if not data.asset_id:
                raise BusinessError(
                    "资产订阅必须提供 asset_id", error_code=ErrorCode.VALIDATION_ERROR
                )
            await self._assert_asset_exists(data.asset_type, data.asset_id)
            existing = await self._repo.find_asset_subscription(
                user_id, data.channel, data.asset_type, data.asset_id
            )
        else:
            if not data.event_type:
                raise BusinessError(
                    "订阅必须提供 event_type 或 asset_type+asset_id",
                    error_code=ErrorCode.VALIDATION_ERROR,
                )
            existing = await self._repo.find_subscription(
                user_id, data.channel, data.event_type
            )
        if existing is not None:
            existing.enabled = data.enabled
            existing.threshold = data.threshold
            await self._repo.commit()
            return existing
        sub = SubscriptionPref(
            user_id=user_id,
            channel=data.channel,
            event_type=None if is_asset else data.event_type,
            asset_type=data.asset_type,
            asset_id=data.asset_id,
            enabled=data.enabled,
            threshold=data.threshold,
        )
        return await self._repo.save_subscription(sub)

    async def _assert_asset_exists(self, asset_type: str, asset_id: str) -> None:
        """校验资产订阅的目标资产真实存在（防订阅幽灵资产）。

        METRIC → Metric.metric_code；TABLE → DBCatalog.entity_name（库.表）。
        不存在则拒绝创建订阅（4xx），避免「关注了不存在的东西」。
        """
        if asset_type == "METRIC":
            stmt = select(Metric.id).where(
                Metric.metric_code == asset_id, Metric.deleted_at.is_(None)
            )
        elif asset_type == "TABLE":
            stmt = select(DBCatalog.id).where(
                DBCatalog.entity_name == asset_id, DBCatalog.deleted_at.is_(None)
            )
        else:  # pragma: no cover - schema validator 已拦截
            raise BusinessError(
                f"非法资产类型: {asset_type}", error_code=ErrorCode.VALIDATION_ERROR
            )
        row = await self._db.execute(stmt)
        if row.scalar_one_or_none() is None:
            raise BusinessError(
                f"资产不存在: {asset_type}:{asset_id}",
                error_code=ErrorCode.NOT_FOUND,
            )

    async def list_subscriptions(self, user_id: int) -> list[SubscriptionPref]:
        return await self._repo.list_subscriptions(user_id)

    async def list_event_logs(self, event_type: str | None, limit: int) -> list[Any]:
        return await self._repo.list_event_logs(event_type, limit)

    async def purge_expired(
        self,
        notify_retention_days: int,
        event_log_retention_days: int,
    ) -> dict[str, int]:
        """按保留策略物理清理过期通知与事件日志（每日定时任务调用）。

        通知：已读/已办结且非 FAILED 且超过保留期 → 删除（未读与待重试保留）；
        事件日志：超过保留期 → 删除。返回两类删除条数。
        """
        now = datetime.now(UTC)
        notify_cutoff = now - timedelta(days=notify_retention_days)
        event_cutoff = now - timedelta(days=event_log_retention_days)
        notifications = await self._repo.purge_old_notifications(notify_cutoff)
        event_logs = await self._repo.purge_old_event_logs(event_cutoff)
        await self._repo.commit()
        return {"notifications": notifications, "event_logs": event_logs}

    async def close(self) -> None:
        """关闭共享 HTTP 客户端（应用关停时调用一次即可，幂等）。"""
        global _HTTP_CLIENT
        if _HTTP_CLIENT is not None:
            await _HTTP_CLIENT.aclose()
            _HTTP_CLIENT = None
