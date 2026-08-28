"""服务基类 BaseService 单测（对齐 TD §5.5 统一服务注入模式）。

覆盖：
- _write_audit：审计写入透传到 write_audit（仅 add，不 commit）
- _publish_event：事件发布透传到 eventbus
- _get_default_eventbus / _get_default_settings：缺省注入
- BaseServiceProtocol runtime_checkable 协议
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.base_service import BaseService, BaseServiceProtocol


@pytest.fixture
def session() -> MagicMock:
    return MagicMock()


@pytest.fixture
def eventbus() -> MagicMock:
    eb = MagicMock()
    eb.publish = AsyncMock()
    return eb


class TestWriteAudit:
    async def test_write_audit_passthrough(self, session: MagicMock) -> None:
        svc = BaseService(session, eventbus=MagicMock())
        await svc._write_audit(
            actor_id=7,
            action="UPDATE",
            entity_type="metric_definition",
            entity_id="m1",
            detail={"k": "v"},
            trace_id="t-1",
            pii_access=True,
        )
        session.add.assert_called_once()
        # 只 add 不 commit（调用方负责 commit）
        session.commit.assert_not_called()

    async def test_write_audit_defaults(self, session: MagicMock) -> None:
        svc = BaseService(session, eventbus=MagicMock())
        await svc._write_audit(
            actor_id=1, action="CREATE", entity_type="term", entity_id="t1", detail={}
        )
        added = session.add.call_args[0][0]
        assert added.actor_id == 1
        assert added.pii_access is False


class TestPublishEvent:
    async def test_publish_event_collects_until_commit(self, eventbus: MagicMock) -> None:
        """T1（审查修复）：_publish_event 仅收集到待投递队列，不立即 publish。"""
        svc = BaseService(MagicMock(), eventbus=eventbus)
        await svc._publish_event("metric.created", {"code": "m1"}, actor_id="9")
        assert len(svc._pending_events) == 1
        assert svc._pending_events[0] == ("metric.created", {"code": "m1"}, "9")
        eventbus.publish.assert_not_awaited()

    async def test_after_commit_flushes_pending_events(self, eventbus: MagicMock) -> None:
        """T1：after_commit 回调触发异步投递；commit 失败/未 commit 不投递。"""
        svc = BaseService(MagicMock(), eventbus=eventbus)
        await svc._publish_event("metric.created", {"code": "m1"}, actor_id="9")
        await svc._publish_event("metric.updated", {"code": "m1"}, actor_id="9")
        # 模拟 SQLAlchemy after_commit 回调（同步）
        svc._on_after_commit(None)
        # 等待 create_task 派发的异步投递完成
        import asyncio

        await asyncio.sleep(0.02)
        assert eventbus.publish.await_count == 2
        assert svc._pending_events == []

    async def test_no_commit_no_flush(self, eventbus: MagicMock) -> None:
        """T1：未触发 after_commit（如事务回滚）时事件不投递。"""
        svc = BaseService(MagicMock(), eventbus=eventbus)
        await svc._publish_event("metric.created", {"code": "m1"}, actor_id="9")
        # 不回滚也不提交——仅收集，不投递
        eventbus.publish.assert_not_awaited()
        assert len(svc._pending_events) == 1


class TestDefaultInjection:
    def test_default_eventbus_and_settings(self) -> None:
        svc = BaseService(MagicMock())
        # 未显式注入时使用模块级默认（get_eventbus / settings 单例）
        assert svc._eventbus is not None
        assert svc._settings is not None

    def test_explicit_injection(self, eventbus: MagicMock) -> None:
        settings = MagicMock()
        svc = BaseService(MagicMock(), eventbus=eventbus, settings=settings)
        assert svc._eventbus is eventbus
        assert svc._settings is settings


def test_protocol_runtime_checkable() -> None:
    """BaseService 实例满足 BaseServiceProtocol（运行时检查）。"""
    svc = BaseService(MagicMock(), eventbus=MagicMock(), settings=MagicMock())
    assert isinstance(svc, BaseServiceProtocol)
