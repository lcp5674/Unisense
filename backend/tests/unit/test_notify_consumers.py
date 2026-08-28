"""业务事件 → 通知闭环消费者注册测试（C1：worker 与 API 两侧对称注入）。

覆盖：
- register_notify_event_consumers：为全部 BUSINESS_EVENT_TYPES 注册本地订阅者
- worker startup：注入 Redis 版 EventBus + 注册通知消费者（此前 worker 侧事件全丢）
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.core.eventbus import EventBus
from app.services.notify.consumers import BUSINESS_EVENT_TYPES, register_notify_event_consumers


def test_register_notify_event_consumers_covers_all_event_types():
    """注册函数为 BUSINESS_EVENT_TYPES 全量建立本地订阅者（单一事实来源）。"""
    bus = EventBus()
    register_notify_event_consumers(bus)

    assert set(bus._subscribers.keys()) == set(BUSINESS_EVENT_TYPES)
    # 采集链路 worker 侧发布的事件必须在通知闭环内（C1 核心场景）
    assert "catalog_registered" in bus._subscribers
    assert "collect.degraded" in bus._subscribers
    assert "collect.failed" in bus._subscribers
    assert "quality.anomaly" in bus._subscribers


def test_business_event_types_has_no_duplicates():
    """事件类型集合无重复（重复会导致同一事件被消费多次）。"""
    assert len(BUSINESS_EVENT_TYPES) == len(set(BUSINESS_EVENT_TYPES))


async def test_list_event_types_endpoint_returns_authoritative_set():
    """GET /notify/subscriptions/event-types 返回 BUSINESS_EVENT_TYPES 权威清单。

    订阅弹窗下拉数据源（前端动态拉取，后端新增业务事件无需发版）。
    """
    from app.api.notify import list_event_types

    out = await list_event_types(trace_id="t")
    items = out.data["items"]
    assert items == list(BUSINESS_EVENT_TYPES)
    # 权威清单可被订阅弹窗直接消费：无重复、含新增事件（metric.reactivated 等）
    assert len(items) == len(set(items))
    assert "metric.reactivated" in items
    assert "conflict_forced_closed" in items
    assert "storage.table_oversized" in items


async def test_worker_startup_injects_eventbus_and_consumers():
    """C1: worker startup 注入 Redis 版 EventBus + 注册通知消费者。"""
    from app.services.collector.worker import startup

    mock_redis = MagicMock()
    with (
        patch("app.services.collector.worker.ArqRedis") as m_arq,
        patch("app.services.collector.worker.init_eventbus") as m_init,
        patch("app.services.collector.worker.register_notify_event_consumers") as m_reg,
    ):
        m_arq.from_url.return_value = mock_redis
        ctx: dict = {}
        await startup(ctx)

    # EventBus 注入 Redis 实例 + 通知消费者注册均被调用
    m_init.assert_called_once_with(mock_redis)
    m_reg.assert_called_once()
    # arq redis 与 job_store 仍正常注入（不影响原有职责）
    assert ctx["redis"] is mock_redis
    assert ctx["job_store"] is not None


async def test_worker_startup_survives_eventbus_failure():
    """C1: EventBus 初始化失败不阻断 worker 启动（best-effort 舱壁）。"""
    from app.services.collector.worker import startup

    mock_redis = MagicMock()
    with (
        patch("app.services.collector.worker.ArqRedis") as m_arq,
        patch(
            "app.services.collector.worker.init_eventbus",
            side_effect=RuntimeError("redis down"),
        ),
        patch("app.services.collector.worker.register_notify_event_consumers"),
    ):
        m_arq.from_url.return_value = mock_redis
        ctx: dict = {}
        await startup(ctx)  # 不应抛异常

    assert ctx["redis"] is mock_redis
