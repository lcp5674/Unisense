"""通知服务单元测试（TD §12.9 / FR-16 / FR-17）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.models.notify import EventLog, Notification, SubscriptionPref
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
    repo.get_user_display_name = AsyncMock(return_value="操作者")
    repo.find_recent_notification = AsyncMock(return_value=None)
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


async def test_publish_event_recipient_direct_delivery() -> None:
    """反馈处理事件：payload 携带 recipient_user_id 时定向通知提交者（in_app，无需订阅）。"""
    svc, repo = _svc()
    out = await svc.publish_event(
        EventPublish(
            event_type="feedback.status_updated",
            payload={"feedback_id": 1, "status": "adopted", "recipient_user_id": 7},
        )
    )
    assert out["notifications"] == 1
    notif = repo.save_notification.call_args[0][0]
    assert notif.subscriber_id == 7
    assert notif.channel == "in_app"
    assert notif.template_code == "feedback.status_updated"


async def test_publish_event_recipient_skips_if_already_subscribed() -> None:
    """提交者若已通过订阅收到通知，则不重复定向投递。"""
    svc, repo = _svc()
    repo.list_enabled_subscriptions = AsyncMock(
        return_value=[
            SubscriptionPref(
                user_id=7, channel="EMAIL", event_type="feedback.status_updated", enabled=True
            )
        ]
    )
    out = await svc.publish_event(
        EventPublish(
            event_type="feedback.status_updated",
            payload={"recipient_user_id": 7},
        )
    )
    assert out["notifications"] == 1  # 仅订阅者 1 条，recipient 7 已覆盖不重复


async def test_publish_event_title_body_business_terms() -> None:
    """通知标题/正文应从源头业务化——非英文码、非 JSON（TD §12.9 产品化）。"""
    svc, repo = _svc()
    repo.list_enabled_subscriptions = AsyncMock(
        return_value=[
            SubscriptionPref(
                user_id=10, channel="EMAIL", event_type="quality.anomaly", enabled=True
            )
        ]
    )
    await svc.publish_event(
        EventPublish(
            event_type="quality.anomaly",
            source="quality",
            payload={
                "level": "P1",
                "metric_id": 1,
                "rule_type": "COMPLETENESS",
                "obs_value": "50.0",
            },
        )
    )
    notif = repo.save_notification.await_args.args[0]
    # 标题：英文码 → 中文业务标题
    assert notif.title == "数据质量异常告警"
    # 正文：不再是 JSON dump，而是「中文标签：值」多行文本
    assert "{" not in (notif.body or "")
    assert "重要程度：高" in notif.body
    assert "规则类型：完整性" in notif.body
    assert "观测值：50.0" in notif.body


async def test_humanize_event_title_fallback() -> None:
    """未知事件类型按 ``域.动作`` 拆词兜底，仍返回中文而非英文码。"""
    from app.services.notify.service import _humanize_event_title

    assert _humanize_event_title("metric.submitted") == "指标待审核"
    assert _humanize_event_title("grant.revoked") == "权限已收回"
    assert _humanize_event_title("conflict_escalated") == "口径冲突已升级"
    assert _humanize_event_title("scheduler.unknown") == "定时任务 · unknown"
    assert _humanize_event_title("") == "系统通知"


async def test_event_title_contract_covers_subscription_list() -> None:
    """标题映射必须覆盖订阅清单全部事件（通知不直出英文码），且无幽灵项（无发布方的事件不映射）。"""
    from app.services.notify.consumers import BUSINESS_EVENT_TYPES
    from app.services.notify.service import _EVENT_TITLE_CN

    subs = set(BUSINESS_EVENT_TYPES)
    for ev in subs:
        assert ev in _EVENT_TITLE_CN, f"订阅事件缺少标题映射: {ev}"
    for key in _EVENT_TITLE_CN:
        assert key in subs, f"标题映射含幽灵事件（无发布方/未订阅）: {key}"


async def test_event_title_covers_new_connectable_events() -> None:
    """反馈/满意度/审计容量告警（走 EventBus 的真实发布事件）须可接入通知闭环。"""
    from app.services.notify.consumers import BUSINESS_EVENT_TYPES
    from app.services.notify.service import _EVENT_TITLE_CN

    for ev in ("feedback.status_updated", "nps.submitted", "audit.capacity_warning"):
        assert ev in BUSINESS_EVENT_TYPES, f"新可接入事件未加入订阅清单: {ev}"
        assert ev in _EVENT_TITLE_CN, f"新可接入事件缺少标题映射: {ev}"


async def test_upsert_subscription_creates() -> None:
    svc, repo = _svc()
    out = await svc.upsert_subscription(
        SubscriptionUpsert(user_id=7, channel="EMAIL", event_type="q.refresh", enabled=True)
    )
    assert isinstance(out, SubscriptionPref)
    repo.save_subscription.assert_awaited()


# ---- handle_business_event（EventBus 业务事件 → 通知闭环）----


async def test_handle_business_event_quality_anomaly() -> None:
    """quality.anomaly 事件落 EventLog 并按订阅扇出（source 映射 quality）。"""
    svc, repo = _svc()
    repo.list_enabled_subscriptions = AsyncMock(
        return_value=[
            SubscriptionPref(
                user_id=10, channel="CONSOLE", event_type="quality.anomaly", enabled=True
            )
        ]
    )
    out = await svc.handle_business_event(
        {
            "event_type": "quality.anomaly",
            "metric_id": 1,
            "level": "P1",
            "rule_type": "COMPLETENESS",
            "payload": {"obs_value": "0.55"},
        }
    )
    assert out["notifications"] == 1
    assert out["delivered"] == 1
    event_type = repo.save_event.call_args.args[0].event_type
    assert event_type == "quality.anomaly"


async def test_handle_business_event_eventbus_format() -> None:
    """兼容 EventBus.publish 的 {event_type, payload, actor_id} 嵌套格式。"""
    svc, repo = _svc()
    out = await svc.handle_business_event(
        {
            "event_type": "conflict.detected",
            "payload": {"conflict_id": "c1", "level": "WARN"},
            "actor_id": "3",
        }
    )
    assert out["event_id"] == 1
    event = repo.save_event.call_args.args[0]
    assert event.source == "semantic"  # conflict → semantic（白名单映射）


async def test_handle_business_event_empty_type_returns_zero() -> None:
    svc, _ = _svc()
    out = await svc.handle_business_event({"foo": "bar"})
    assert out == {"event_id": 0, "notifications": 0, "delivered": 0}


async def test_handle_business_event_unknown_source_maps_system() -> None:
    """未知来源前缀映射为 system，非法 level 收敛为 INFO。"""
    svc, repo = _svc()
    out = await svc.handle_business_event(
        {"event_type": "weird.event", "level": "PANIC", "payload": {"x": 1}}
    )
    assert out["event_id"] == 1
    event = repo.save_event.call_args.args[0]
    assert event.source == "system"
    assert event.level == "INFO"


class TestDispatchChannelNormalization:
    """回归：DB 渠道为大写枚举值（EMAIL/WEBHOOK/...），_dispatch 曾只匹配小写，导致
    除 console 外的渠道全部投递失败。
    """

    def _notif(self) -> MagicMock:
        n = MagicMock()
        n.template_code = "conflict.escalate"
        n.title = "冲突升级"
        n.body = "{}"
        n.subscriber_id = 1
        n.payload = {}
        return n

    async def test_uppercase_channel_hits_webhook(self) -> None:
        svc, _ = _svc()
        svc._dispatch_webhook = AsyncMock(return_value=True)  # noqa: SLF001
        ok = await svc._dispatch(self._notif(), "WEBHOOK")  # noqa: SLF001
        assert ok is True
        svc._dispatch_webhook.assert_awaited_once()

    async def test_lowercase_channel_hits_webhook(self) -> None:
        svc, _ = _svc()
        svc._dispatch_webhook = AsyncMock(return_value=True)  # noqa: SLF001
        ok = await svc._dispatch(self._notif(), "webhook")  # noqa: SLF001
        assert ok is True
        svc._dispatch_webhook.assert_awaited_once()

    async def test_uppercase_channel_hits_console(self) -> None:
        svc, _ = _svc()
        ok = await svc._dispatch(self._notif(), "console")  # noqa: SLF001
        assert ok is True

    async def test_unknown_channel_returns_false(self) -> None:
        svc, _ = _svc()
        ok = await svc._dispatch(self._notif(), "slack")  # noqa: SLF001
        assert ok is False


async def test_notify_user_direct_delivery() -> None:
    """定向通知指定用户：不依赖订阅、直接为指定 subscriber 创建 IN_APP 通知并投递。"""
    svc, repo = _svc()
    # 模拟站内信渠道投递成功
    svc._dispatch = AsyncMock(return_value=True)  # noqa: SLF001
    notif = await svc.notify_user(
        user_id=7,
        event_type="metric.rename_required",
        title="指标需要改名",
        body="请在详情页改名",
        payload={"metric_code": "sales_gmv_daily", "conflict_id": "CF-1"},
    )
    assert notif.subscriber_id == 7
    assert notif.channel == "in_app"
    assert notif.template_code == "metric.rename_required"
    assert notif.status == "SENT"
    assert notif.sent_at is not None
    # 不调用 list_enabled_subscriptions（定向，非订阅广播）
    repo.list_enabled_subscriptions.assert_not_called()
    repo.commit.assert_awaited()
    svc._dispatch.assert_awaited_once_with(notif, "in_app")  # noqa: SLF001


async def test_notify_user_marks_failed_when_dispatch_fails() -> None:
    """投递失败时标记 FAILED（不误标 SENT）。"""
    svc, repo = _svc()
    svc._dispatch = AsyncMock(return_value=False)  # noqa: SLF001
    notif = await svc.notify_user(user_id=7, event_type="metric.rename_required", title="t")
    assert notif.status == "FAILED"
    assert notif.sent_at is None


# ---- 送达失败处置：retry_delivery / mark_handled / 去重防风暴 ----

async def test_retry_delivery_failed_to_sent() -> None:
    """FAILED 通知重试成功 → SENT + sent_at + last_error 清空。"""
    svc, repo = _svc()
    notif = Notification(
        subscriber_id=7,
        channel="webhook",
        template_code="quality.anomaly",
        title="数据质量异常告警",
        status="FAILED",
        last_error="连接超时",
    )
    repo.get_notification = AsyncMock(return_value=notif)
    svc._dispatch = AsyncMock(return_value=True)  # noqa: SLF001
    out = await svc.retry_delivery(7, actor_id=7)
    assert out.status == "SENT"
    assert out.sent_at is not None
    assert out.last_error is None
    svc._dispatch.assert_awaited_once_with(notif, "webhook")  # noqa: SLF001


async def test_retry_delivery_still_failed_keeps_error() -> None:
    """重试仍失败 → 保持 FAILED，last_error 更新（由 _dispatch 写入）。"""
    svc, repo = _svc()
    notif = Notification(
        subscriber_id=7, channel="webhook", template_code="q.x", title="t", status="FAILED"
    )
    repo.get_notification = AsyncMock(return_value=notif)

    async def _fail(_n, _c):  # noqa: ANN001
        _n.last_error = "网关 500"
        return False

    svc._dispatch = _fail  # noqa: SLF001
    out = await svc.retry_delivery(7, actor_id=7)
    assert out.status == "FAILED"
    assert out.last_error == "网关 500"
    assert out.sent_at is None


async def test_retry_delivery_rejects_non_failed() -> None:
    """非 FAILED 状态重试 → INVALID_TRANSITION。"""
    from app.core.exceptions import UnisenseError

    svc, repo = _svc()
    notif = Notification(
        subscriber_id=7, channel="in_app", template_code="q.x", title="t", status="SENT"
    )
    repo.get_notification = AsyncMock(return_value=notif)
    try:
        await svc.retry_delivery(7, actor_id=7)
        raise AssertionError("应抛 INVALID_TRANSITION")
    except UnisenseError as exc:
        assert exc.error_code == "INVALID_TRANSITION"


async def test_mark_handled_sets_handled_at() -> None:
    """mark_handled → handled_at 落库（幂等：重复调用不覆写）。"""
    svc, repo = _svc()
    notif = Notification(
        subscriber_id=7, channel="in_app", template_code="conflict_open", title="t"
    )
    repo.get_notification = AsyncMock(return_value=notif)
    out = await svc.mark_handled(7, actor_id=7)
    assert out.handled_at is not None
    first = out.handled_at
    out2 = await svc.mark_handled(7, actor_id=7)
    assert out2.handled_at == first


async def test_publish_event_dedup_skips_recent() -> None:
    """去重防风暴：窗口内已存在同类型未处理通知 → 跳过创建新通知。"""
    svc, repo = _svc()
    repo.list_enabled_subscriptions = AsyncMock(
        return_value=[
            SubscriptionPref(
                user_id=10,
                channel="EMAIL",
                event_type="collect.degraded",
                enabled=True,
            )
        ]
    )
    repo.find_recent_notification = AsyncMock(
        return_value=Notification(
            subscriber_id=10, channel="EMAIL", template_code="collect.degraded", title="t"
        )
    )
    out = await svc.publish_event(EventPublish(event_type="collect.degraded", source="collect"))
    assert out["notifications"] == 0
    assert out["delivered"] == 0
    repo.save_notification.assert_not_called()


async def test_purge_expired_delegates_to_repo() -> None:
    """purge_expired → 按保留期计算 cutoff 并委托 repository，提交事务。"""
    svc, repo = _svc()
    repo.purge_old_notifications = AsyncMock(return_value=12)
    repo.purge_old_event_logs = AsyncMock(return_value=34)
    out = await svc.purge_expired(notify_retention_days=90, event_log_retention_days=180)
    assert out == {"notifications": 12, "event_logs": 34}
    # cutoff 由 now - retention 计算：90 天前
    import math
    from datetime import datetime

    cutoff = repo.purge_old_notifications.call_args.args[0]
    assert math.isclose((datetime.now().timestamp() - cutoff.timestamp()) / 86400, 90, abs_tol=0.01)
    # 事件日志保留期独立（180 天）
    event_cutoff = repo.purge_old_event_logs.call_args.args[0]
    assert math.isclose(
        (datetime.now().timestamp() - event_cutoff.timestamp()) / 86400, 180, abs_tol=0.01
    )
    repo.commit.assert_awaited()


async def test_unread_count_delegates_to_repo() -> None:
    """unread_count → 委托 repository 精确计数（全局角标）。"""
    svc, repo = _svc()
    repo.count_unread = AsyncMock(return_value=5)
    count = await svc.unread_count(3)
    assert count == 5
    repo.count_unread.assert_awaited_with(3)
