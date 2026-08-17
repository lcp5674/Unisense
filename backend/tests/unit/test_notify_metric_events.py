"""P0-1/P0-2：metric.* 事件接入通知闭环 + 命名统一。

验收：
1. ``notify.consumers.BUSINESS_EVENT_TYPES``（通知订阅集合）包含全部 9 种真实 metric.* 事件；
2. 幽灵事件 ``metric.published`` 不得出现在订阅集合 / notify 标题映射中；
3. notify 标题映射覆盖全部 9 种真实事件（_humanize_event_title 命中业务标题）；
4. metric.approved 事件经 handle_business_event 走通 notify 扇出（落 EventLog + Notification）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.models.notify import EventLog, SubscriptionPref
from app.services.notify.consumers import BUSINESS_EVENT_TYPES
from app.services.notify.service import _EVENT_TITLE_CN, NotifyService, _humanize_event_title

#: 语义服务真实发布的全部 metric.* 事件（services/semantic/service.py）
_REAL_METRIC_EVENTS = (
    "metric.created",
    "metric.submitted",
    "metric.approved",
    "metric.rejected",
    "metric.deprecated",
    "metric.promoted",
    "metric.rolled_back",
    "metric.emergency_published",
    "metric.health_critical",
)


def test_all_real_metric_events_in_subscription_set() -> None:
    """9 种真实 metric.* 事件全部进入通知订阅集合（EventBus 精确匹配，缺一不可）。"""
    missing = [e for e in _REAL_METRIC_EVENTS if e not in BUSINESS_EVENT_TYPES]
    assert missing == [], f"以下 metric.* 事件未订阅，事件发布后无人消费: {missing}"


def test_ghost_metric_published_not_in_subscription_set() -> None:
    """幽灵事件 metric.published 后端从不发布，不得出现在订阅集合。"""
    assert "metric.published" not in BUSINESS_EVENT_TYPES


def test_ghost_metric_published_not_in_title_map() -> None:
    """notify 标题映射不再引用 metric.published（统一为真实 9 种事件）。"""
    assert "metric.published" not in _EVENT_TITLE_CN
    # 覆盖真实 9 种事件的标题（防幽灵映射替换后漏掉真实事件）
    missing = [e for e in _REAL_METRIC_EVENTS if e not in _EVENT_TITLE_CN]
    assert missing == [], f"标题映射缺少真实 metric.* 事件: {missing}"


def test_humanize_title_covers_all_real_metric_events() -> None:
    """_humanize_event_title 对全部真实 metric.* 事件返回业务中文标题（非英文码兜底）。"""
    for event_type in _REAL_METRIC_EVENTS:
        title = _humanize_event_title(event_type)
        # 命中映射 → 中文标题；未命中映射会走 ``域.动作`` 兜底（仍是中文，不理想）
        assert title in _EVENT_TITLE_CN.values() or "·" not in title, (
            f"{event_type} 未命中业务标题映射，落入拆词兜底: {title}"
        )


def _svc() -> tuple[NotifyService, MagicMock]:
    db = MagicMock()
    svc = NotifyService(db)
    repo = MagicMock()

    def _stamp(e: EventLog) -> EventLog:
        e.id = 1
        return e

    repo.save_event = AsyncMock(side_effect=_stamp)
    repo.list_enabled_subscriptions = AsyncMock(
        return_value=[
            SubscriptionPref(
                user_id=10, channel="IN_APP", event_type="metric.approved", enabled=True
            )
        ]
    )
    # 去重防风暴（344ef47 引入）：窗口内无同类型通知 → 放行
    repo.find_recent_notification = AsyncMock(return_value=None)
    repo.save_notification = AsyncMock(side_effect=lambda n: n)
    repo.get_user_display_name = AsyncMock(return_value="审核员")
    repo.commit = AsyncMock()
    svc._repo = repo  # noqa: SLF001
    return svc, repo


async def test_metric_approved_flows_through_notify() -> None:
    """metric.approved 事件经 handle_business_event 落 EventLog 并扇出 Notification。"""
    svc, repo = _svc()
    out = await svc.handle_business_event(
        {
            "event_type": "metric.approved",
            "payload": {
                "metric_code": "sales_gmv_daily",
                "version": 1,
                "domain": "sales",
            },
            "actor_id": "3",
        }
    )
    assert out["notifications"] == 1
    assert out["delivered"] == 1
    # EventLog 事件类型保留原名（metric.approved），source 映射为 metric
    event = repo.save_event.call_args.args[0]
    assert event.event_type == "metric.approved"
    assert event.source == "metric"
    # 操作人链路：actor_id 透传 + 姓名快照反查（「谁在何时做了什么」审计语义）
    assert event.actor_id == 3
    assert event.actor_name == "审核员"
    repo.get_user_display_name.assert_awaited_once_with(3)
    # Notification 标题为业务中文（指标已通过），模板编码为事件名
    notif = repo.save_notification.call_args.args[0]
    assert notif.template_code == "metric.approved"
    assert notif.title == "指标已通过"
    assert notif.actor_id == 3
    assert notif.actor_name == "审核员"


async def test_system_event_without_actor_no_actor_name_resolution() -> None:
    """系统/定时任务事件无 actor_id：不反查姓名，EventLog/Notification actor 为 None。"""
    svc, repo = _svc()
    out = await svc.handle_business_event(
        {
            "event_type": "metric.health_critical",
            "payload": {"metric_code": "sales_gmv_daily", "score": 60, "level": "WARN"},
        }
    )
    assert out["notifications"] == 1
    event = repo.save_event.call_args.args[0]
    assert event.actor_id is None
    assert event.actor_name is None
    repo.get_user_display_name.assert_not_awaited()
