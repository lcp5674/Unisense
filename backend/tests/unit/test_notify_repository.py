"""notify 服务 Repository 单测（补齐覆盖率）。

针对 notify/repository.py 的 39% 覆盖率，补充以下场景：
- save_event / save_notification / get_notification
- list_notifications (with/without status filter)
- find_subscription / list_subscriptions
- list_enabled_subscriptions
- list_event_logs (with/without event_type filter)
- commit
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.notify import EventLog, Notification, SubscriptionPref
from app.services.notify.repository import NotifyRepository


@pytest.fixture
def repo() -> NotifyRepository:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    return NotifyRepository(session)


class TestNotifyRepository:
    async def test_save_event(self, repo: NotifyRepository) -> None:
        event = EventLog(event_type="test", source="api", payload={})
        result = await repo.save_event(event)
        assert result is event
        repo._session.add.assert_called_once_with(event)
        repo._session.flush.assert_called_once()

    async def test_save_notification(self, repo: NotifyRepository) -> None:
        notif = Notification(subscriber_id=1, channel="email", payload={})
        result = await repo.save_notification(notif)
        assert result is notif
        repo._session.add.assert_called_once_with(notif)

    async def test_get_notification_found(self, repo: NotifyRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = Notification(id=1)
        repo._session.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_notification(1)
        assert result is not None
        assert result.id == 1

    async def test_get_notification_not_found(self, repo: NotifyRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        repo._session.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_notification(999)
        assert result is None

    async def test_list_notifications_no_status(self, repo: NotifyRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            Notification(id=1, subscriber_id=1),
            Notification(id=2, subscriber_id=1),
        ]
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.list_notifications(subscriber_id=1, status=None)
        assert len(results) == 2

    async def test_list_notifications_with_status(self, repo: NotifyRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [Notification(id=1, status="SENT")]
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.list_notifications(subscriber_id=1, status="SENT")
        assert len(results) == 1

    async def test_find_subscription_found(self, repo: NotifyRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = SubscriptionPref(user_id=1, channel="email")
        repo._session.execute = AsyncMock(return_value=mock_result)
        result = await repo.find_subscription(user_id=1, channel="email", event_type="alert")
        assert result is not None

    async def test_find_subscription_not_found(self, repo: NotifyRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        repo._session.execute = AsyncMock(return_value=mock_result)
        result = await repo.find_subscription(user_id=999, channel="email", event_type="alert")
        assert result is None

    async def test_list_subscriptions(self, repo: NotifyRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            SubscriptionPref(user_id=1, channel="email"),
        ]
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.list_subscriptions(user_id=1)
        assert len(results) == 1

    async def test_list_enabled_subscriptions(self, repo: NotifyRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            SubscriptionPref(user_id=1, enabled=True),
        ]
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.list_enabled_subscriptions(event_type="alert")
        assert len(results) == 1

    async def test_save_subscription(self, repo: NotifyRepository) -> None:
        sub = SubscriptionPref(user_id=1, channel="email", event_type="alert")
        result = await repo.save_subscription(sub)
        assert result is sub
        repo._session.add.assert_called_once_with(sub)

    async def test_list_event_logs_no_filter(self, repo: NotifyRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [EventLog(event_type="alert")]
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.list_event_logs(event_type=None, limit=10)
        assert len(results) == 1

    async def test_list_event_logs_with_filter(self, repo: NotifyRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [EventLog(event_type="alert")]
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.list_event_logs(event_type="alert", limit=10)
        assert len(results) == 1

    async def test_commit(self, repo: NotifyRepository) -> None:
        await repo.commit()
        repo._session.commit.assert_called_once()

    async def test_get_user_display_name_prefers_display(self, repo: NotifyRepository) -> None:
        """操作人姓名快照：display_name 优先。"""
        mock_result = MagicMock()
        mock_result.first.return_value = ("爱丽丝", "alice")
        repo._session.execute = AsyncMock(return_value=mock_result)
        name = await repo.get_user_display_name(7)
        assert name == "爱丽丝"

    async def test_get_user_display_name_falls_back_to_username(
        self, repo: NotifyRepository
    ) -> None:
        """display_name 为空时回落 username。"""
        mock_result = MagicMock()
        mock_result.first.return_value = ("", "bob")
        repo._session.execute = AsyncMock(return_value=mock_result)
        name = await repo.get_user_display_name(9)
        assert name == "bob"

    async def test_get_user_display_name_unknown_user(self, repo: NotifyRepository) -> None:
        """用户不存在/已删除：返回 None（通知仍是历史记录，前端回落展示）。"""
        mock_result = MagicMock()
        mock_result.first.return_value = None
        repo._session.execute = AsyncMock(return_value=mock_result)
        name = await repo.get_user_display_name(999)
        assert name is None

    # ---- list_notifications_page 产品化筛选（read_state / template_code / todo_only / days）----

    def _page_mocks(self) -> AsyncMock:
        """count + rows 两次 execute。"""
        mock_count = MagicMock()
        mock_count.scalar_one.return_value = 0
        mock_rows = MagicMock()
        mock_rows.scalars.return_value.all.return_value = []
        return AsyncMock(side_effect=[mock_count, mock_rows])

    async def test_list_page_unread_filter(self, repo: NotifyRepository) -> None:
        """read_state=unread → SQL 含 read_at IS NULL。"""
        repo._session.execute = self._page_mocks()
        await repo.list_notifications_page(1, None, read_state="unread")
        select_stmt = repo._session.execute.call_args_list[0].args[0]
        sql = str(select_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "read_at IS NULL" in sql

    async def test_list_page_read_filter(self, repo: NotifyRepository) -> None:
        """read_state=read → SQL 含 read_at IS NOT NULL。"""
        repo._session.execute = self._page_mocks()
        await repo.list_notifications_page(1, None, read_state="read")
        select_stmt = repo._session.execute.call_args_list[0].args[0]
        sql = str(select_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "read_at IS NOT NULL" in sql

    async def test_list_page_template_code_filter(self, repo: NotifyRepository) -> None:
        """template_code 精确过滤。"""
        repo._session.execute = self._page_mocks()
        await repo.list_notifications_page(1, None, template_code="metric.approved")
        select_stmt = repo._session.execute.call_args_list[0].args[0]
        sql = str(select_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "template_code" in sql
        assert "metric.approved" in sql

    async def test_list_page_todo_only_filter(self, repo: NotifyRepository) -> None:
        """todo_only → template_code IN (待处理事件集)。"""
        repo._session.execute = self._page_mocks()
        await repo.list_notifications_page(1, None, todo_only=True)
        select_stmt = repo._session.execute.call_args_list[0].args[0]
        sql = str(select_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "template_code IN" in sql
        assert "conflict_open" in sql  # 待处理集中事件出现
        assert "metric.approved" not in sql  # 非待处理事件不出现

    async def test_list_page_days_filter(self, repo: NotifyRepository) -> None:
        """days=N → created_at 近 N 天过滤。"""
        repo._session.execute = self._page_mocks()
        await repo.list_notifications_page(1, None, days=7)
        select_stmt = repo._session.execute.call_args_list[0].args[0]
        sql = str(select_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "created_at" in sql
        assert ">=" in sql

    async def test_list_page_todo_only_excludes_handled(self, repo: NotifyRepository) -> None:
        """todo_only → 排除已标记处理的（handled_at IS NULL 条件）。"""
        repo._session.execute = self._page_mocks()
        await repo.list_notifications_page(1, None, todo_only=True)
        select_stmt = repo._session.execute.call_args_list[0].args[0]
        sql = str(select_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "handled_at IS NULL" in sql

    async def test_list_page_object_key_filter(self, repo: NotifyRepository) -> None:
        """object_key → payload 业务对象键 JSON 精确匹配（json_extract）。"""
        repo._session.execute = self._page_mocks()
        await repo.list_notifications_page(1, None, object_key="sales_gmv")
        select_stmt = repo._session.execute.call_args_list[0].args[0]
        sql = str(select_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "json_extract" in sql
        assert "metric_code" in sql
        assert "sales_gmv" in sql

    async def test_list_page_object_key_numeric_matches_ref_id(
        self, repo: NotifyRepository
    ) -> None:
        """数字 object_key → 额外匹配 ref_id 精确相等。"""
        repo._session.execute = self._page_mocks()
        await repo.list_notifications_page(1, None, object_key="123")
        select_stmt = repo._session.execute.call_args_list[0].args[0]
        sql = str(select_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "ref_id" in sql
        assert "123" in sql

    async def test_find_recent_notification(self, repo: NotifyRepository) -> None:
        """find_recent_notification → 近窗口同类型通知查询（时间下界 + 未处理）。"""
        mock = MagicMock()
        mock.scalar_one_or_none.return_value = None
        repo._session.execute = AsyncMock(return_value=mock)
        out = await repo.find_recent_notification(7, "collect.degraded", 60)
        assert out is None
        select_stmt = repo._session.execute.call_args.args[0]
        sql = str(select_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "collect.degraded" in sql
        assert "handled_at IS NULL" in sql
