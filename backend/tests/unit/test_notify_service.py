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
