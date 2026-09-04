"""系统字典 Service 单元测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import BusinessError, NotFoundError
from app.services.system_dict.service import SystemDictService


@asynccontextmanager
async def _nested():
    """真实 savepoint 语义：异常从 yield 抛出进外层 except。

    MagicMock 的 async with（``begin_nested``）的 ``__aexit__`` 返回真值会吞掉
    块内异常，导致批量失败项无法被捕获；用真实 asynccontextmanager 模拟。
    """
    yield


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def svc(mock_db):
    return SystemDictService(mock_db)


class TestCreateItem:
    async def test_create_item_success(self, svc) -> None:
        svc._repo.get_item_including_deleted = AsyncMock(return_value=None)
        item = MagicMock()
        item.dict_type = "granularity"
        item.code = "minute"
        item.label = "分钟"
        svc._repo.create = AsyncMock(return_value=item)

        from app.services.system_dict.schemas import DictItemCreate

        data = DictItemCreate(code="minute", label="分钟", sort_order=7)
        result = await svc.create_item("granularity", data)
        assert result.code == "minute"

    async def test_create_duplicate_rejected(self, svc) -> None:
        active = MagicMock()
        active.deleted_at = None
        svc._repo.get_item_including_deleted = AsyncMock(return_value=active)

        from app.services.system_dict.schemas import DictItemCreate

        data = DictItemCreate(code="day", label="天")
        from app.core.exceptions import ConflictError

        with pytest.raises(ConflictError):
            await svc.create_item("granularity", data)

    async def test_create_restores_soft_deleted(self, svc) -> None:
        """软删后重建同码：恢复行而非触发唯一索引冲突 500。"""
        deleted = MagicMock()
        deleted.deleted_at = object()  # 非 None → 软删行
        deleted.status = "inactive"
        svc._repo.get_item_including_deleted = AsyncMock(return_value=deleted)
        svc._repo.update = AsyncMock(return_value=deleted)

        from app.services.system_dict.schemas import DictItemCreate

        data = DictItemCreate(code="day", label="天", sort_order=1)
        result = await svc.create_item("granularity", data)
        assert deleted.deleted_at is None
        assert deleted.status == "active"
        assert result is deleted

    async def test_create_item_auto_generate_code(self, svc) -> None:
        """未传 code：按显示名自动生成英文编码（分钟 → minute）。"""
        svc._repo.get_item_including_deleted = AsyncMock(return_value=None)
        svc._repo.code_exists_in_type = AsyncMock(return_value=False)
        item = MagicMock()
        item.code = "minute"
        svc._repo.create = AsyncMock(return_value=item)

        from app.services.system_dict.schemas import DictItemCreate

        data = DictItemCreate(label="分钟", sort_order=7)
        result = await svc.create_item("granularity", data)
        assert result.code == "minute"
        # 自动生成路径：存在性判定按 (dict_type, 候选编码) 查询
        svc._repo.code_exists_in_type.assert_awaited()
        created = svc._repo.create.await_args.args[0]
        assert created.code == "minute"

    async def test_create_item_auto_generate_conflict_suffix(self, svc) -> None:
        """自动生成编码冲突时追加序号（minute → minute_2）。"""
        svc._repo.get_item_including_deleted = AsyncMock(return_value=None)
        # 第一次（minute）冲突、第二次（minute_2）可用
        svc._repo.code_exists_in_type = AsyncMock(side_effect=[True, False])
        svc._repo.create = AsyncMock(side_effect=lambda item: item)

        from app.services.system_dict.schemas import DictItemCreate

        data = DictItemCreate(label="分钟")
        result = await svc.create_item("granularity", data)
        assert result.code == "minute_2"

    async def test_create_item_auto_generate_fallback(self, svc) -> None:
        """纯标点/空白显示名无可提取字符：回退 item。"""
        svc._repo.get_item_including_deleted = AsyncMock(return_value=None)
        svc._repo.code_exists_in_type = AsyncMock(return_value=False)
        svc._repo.create = AsyncMock(side_effect=lambda item: item)

        from app.services.system_dict.schemas import DictItemCreate

        data = DictItemCreate(label="￥")
        result = await svc.create_item("unit", data)
        assert result.code == "item"


class TestDeleteItem:
    async def test_delete_with_references_rejected(self, svc) -> None:
        item = MagicMock()
        item.dict_type = "unit"
        item.code = "CNY"
        svc._repo.get_item = AsyncMock(return_value=item)
        svc.get_ref_count = AsyncMock(return_value=50)

        with pytest.raises(BusinessError, match="引用"):
            await svc.delete_item("unit", "CNY")


class TestValidateDictValue:
    async def test_validate_active_value(self, svc) -> None:
        item = MagicMock()
        item.status = "active"
        svc._repo.get_item = AsyncMock(return_value=item)
        result = await svc.validate_dict_value("granularity", "day")
        assert result is item

    async def test_validate_inactive_value(self, svc) -> None:
        item = MagicMock()
        item.status = "inactive"
        svc._repo.get_item = AsyncMock(return_value=item)
        with pytest.raises(BusinessError, match="停用"):
            await svc.validate_dict_value("granularity", "minute")

    async def test_validate_nonexistent_value(self, svc) -> None:
        svc._repo.get_item = AsyncMock(return_value=None)
        with pytest.raises(NotFoundError):
            await svc.validate_dict_value("granularity", "nonexistent")


class TestList:
    async def test_list_by_type_delegates(self, svc) -> None:
        svc._repo.list_by_type = AsyncMock(return_value=[MagicMock()])
        rows = await svc.list_by_type("granularity")
        assert len(rows) == 1
        svc._repo.list_by_type.assert_awaited_once_with("granularity", "active")

    async def test_list_all_by_type_delegates(self, svc) -> None:
        svc._repo.list_by_type = AsyncMock(return_value=[MagicMock(), MagicMock()])
        rows = await svc.list_all_by_type("unit")
        assert len(rows) == 2
        # service 以关键字 status=None 调用
        svc._repo.list_by_type.assert_awaited_once_with("unit", status=None)

    async def test_list_dict_types_delegates(self, svc) -> None:
        svc._repo.list_dict_types = AsyncMock(return_value=["granularity", "unit"])
        assert await svc.list_dict_types() == ["granularity", "unit"]

    async def test_get_item_found(self, svc) -> None:
        item = MagicMock()
        svc._repo.get_item = AsyncMock(return_value=item)
        assert await svc.get_item("granularity", "day") is item


class TestUpdate:
    async def test_update_item_success(self, svc) -> None:
        item = MagicMock()
        item.dict_type = "granularity"
        item.code = "day"
        svc._repo.get_item = AsyncMock(return_value=item)
        svc._repo.update = AsyncMock(return_value=item)

        from app.services.system_dict.schemas import DictItemUpdate

        data = DictItemUpdate(label="天", sort_order=1)
        result = await svc.update_item("granularity", "day", data)
        assert item.label == "天"
        assert result is item

    async def test_update_item_not_found(self, svc) -> None:
        svc._repo.get_item = AsyncMock(return_value=None)
        from app.services.system_dict.schemas import DictItemUpdate

        with pytest.raises(NotFoundError):
            await svc.update_item("granularity", "nope", DictItemUpdate(label="x"))


def _exec_result(rowcount: int = 0, scalar: int | None = None) -> MagicMock:
    """构造 db.execute 返回值：UPDATE 用 rowcount，COUNT 用 scalar。"""
    result = MagicMock()
    result.rowcount = rowcount
    result.scalar.return_value = scalar if scalar is not None else rowcount
    result.scalars.return_value.all.return_value = []
    return result


class TestRenameCode:
    """改码（rename-with-sync）：校验 → 同步引用 → 更新编码，任一步失败整体回滚。"""

    async def test_rename_syncs_references(self, svc, mock_db) -> None:
        item = MagicMock()
        item.dict_type = "unit"
        item.code = "yuan"
        svc._repo.get_item = AsyncMock(return_value=item)
        svc._repo.get_item_including_deleted = AsyncMock(return_value=None)
        svc._repo.update = AsyncMock(return_value=item)
        # unit → 仅 Metric.unit 一列；UPDATE rowcount=3；JSON 同步 0 行
        mock_db.execute = AsyncMock(return_value=_exec_result(rowcount=3))

        from app.services.system_dict.schemas import DictItemUpdate

        with patch(
            "app.services.system_dict.service._sync_json_references",
            new=AsyncMock(return_value=0),
        ):
            result = await svc.update_item("unit", "yuan", DictItemUpdate(code="CNY"))
        assert result.code == "CNY"
        assert mock_db.execute.await_count == 1  # unit 只有 Metric.unit 一个目标列

    async def test_rename_enum_value_rejected(self, svc, mock_db) -> None:
        """业务逻辑 ENUM 列（metric.type 三分支）：新编码不在值域 → 拒绝且不触发任何写。"""
        item = MagicMock()
        item.dict_type = "metric_type"
        item.code = "atomic"
        svc._repo.get_item = AsyncMock(return_value=item)

        from app.services.system_dict.schemas import DictItemUpdate

        with pytest.raises(BusinessError) as exc_info:
            await svc.update_item("metric_type", "atomic", DictItemUpdate(code="STAGING"))
        assert exc_info.value.error_code == "DICT_CODE_ENUM_CONSTRAINT"
        mock_db.execute.assert_not_awaited()

    async def test_rename_varchar_value_allowed(self, svc, mock_db) -> None:
        """纯字典消费列（0143 起 VARCHAR）：新编码不受 ENUM 值域限制（如扩 DIM）→ 放行。"""
        item = MagicMock()
        item.dict_type = "dw_layer"
        item.code = "ODS"
        svc._repo.get_item = AsyncMock(return_value=item)
        svc._repo.get_item_including_deleted = AsyncMock(return_value=None)
        svc._repo.update = AsyncMock(return_value=item)
        mock_db.execute = AsyncMock(return_value=_exec_result(rowcount=2))

        from app.services.system_dict.schemas import DictItemUpdate

        with patch(
            "app.services.system_dict.service._sync_json_references",
            new=AsyncMock(return_value=0),
        ):
            result = await svc.update_item("dw_layer", "ODS", DictItemUpdate(code="DIM"))
        assert result.code == "DIM"
        assert mock_db.execute.await_count == 1  # dw_layer → 仅 Metric.dw_layer 一列

    async def test_rename_duplicate_rejected(self, svc) -> None:
        item = MagicMock()
        item.dict_type = "unit"
        item.code = "yuan"
        svc._repo.get_item = AsyncMock(return_value=item)
        other = MagicMock()
        other.id = 999
        svc._repo.get_item_including_deleted = AsyncMock(return_value=other)

        from app.core.exceptions import ConflictError
        from app.services.system_dict.schemas import DictItemUpdate

        with pytest.raises(ConflictError):
            await svc.update_item("unit", "yuan", DictItemUpdate(code="ge"))

    async def test_rename_same_code_noop(self, svc) -> None:
        """code 与现值相同 → 不触发改码路径（无唯一性查询/UPDATE）。"""
        item = MagicMock()
        item.dict_type = "unit"
        item.code = "yuan"
        svc._repo.get_item = AsyncMock(return_value=item)
        svc._repo.get_item_including_deleted = AsyncMock()
        svc._repo.update = AsyncMock(return_value=item)

        from app.services.system_dict.schemas import DictItemUpdate

        await svc.update_item("unit", "yuan", DictItemUpdate(code="yuan", label="元"))
        svc._repo.get_item_including_deleted.assert_not_awaited()


class TestRefCountRegistry:
    """引用计数复用注册表：覆盖度量/挂载等 Metric 之外的引用面。"""

    async def test_ref_count_measure_category(self, svc, mock_db) -> None:
        """measure_category → MeasureCatalog.category（此前不计数，删除保护缺口）。"""
        result = _exec_result(rowcount=7)
        mock_db.execute = AsyncMock(return_value=result)
        assert await svc.get_ref_count("measure_category", "FLOW") == 7

    async def test_ref_count_unknown_type_zero(self, svc) -> None:
        assert await svc.get_ref_count("pii_rule", "phone") == 0

    async def test_ref_count_sums_multiple_tables(self, svc, mock_db) -> None:
        """granularity → Metric + MetricMount + MetricTemplate 三列求和。"""
        mock_db.execute = AsyncMock(
            side_effect=[
                _exec_result(rowcount=2),
                _exec_result(rowcount=3),
                _exec_result(rowcount=1),
            ]
        )
        assert await svc.get_ref_count("granularity", "day") == 6


class TestToggle:
    async def test_deactivate_item(self, svc) -> None:
        item = MagicMock()
        item.status = "active"
        svc._repo.get_item = AsyncMock(return_value=item)
        svc._repo.update = AsyncMock(return_value=item)
        result = await svc.deactivate_item("granularity", "day")
        assert result.status == "inactive"

    async def test_activate_item(self, svc) -> None:
        item = MagicMock()
        item.status = "inactive"
        svc._repo.get_item = AsyncMock(return_value=item)
        svc._repo.update = AsyncMock(return_value=item)
        result = await svc.activate_item("granularity", "day")
        assert result.status == "active"


class TestDeleteMore:
    async def test_delete_success(self, svc) -> None:
        item = MagicMock()
        item.dict_type = "unit"
        item.code = "CNY"
        svc._repo.get_item = AsyncMock(return_value=item)
        svc.get_ref_count = AsyncMock(return_value=0)
        svc._repo.soft_delete = AsyncMock()
        await svc.delete_item("unit", "CNY")
        svc._repo.soft_delete.assert_awaited_once()

    async def test_get_ref_count_delegates(self, svc) -> None:
        # get_ref_count 直接查 Metric 表（不走 repo）
        result = MagicMock()
        result.scalar.return_value = 3
        svc._db.execute = AsyncMock(return_value=result)
        assert await svc.get_ref_count("unit", "CNY") == 3

    async def test_get_ref_count_unknown_type(self, svc) -> None:
        assert await svc.get_ref_count("unknown_type", "x") == 0

    async def test_get_ref_count_currency(self, svc) -> None:
        """currency 字典参与引用保护（field_map 已映射 Metric.currency）。"""
        result = MagicMock()
        result.scalar.return_value = 2
        svc._db.execute = AsyncMock(return_value=result)
        assert await svc.get_ref_count("currency", "CNY") == 2


class TestUnknownValueGovernance:
    """字典未收录值治理：校验 / 通知管理员收录打回 / 打回闭环。"""

    async def test_verify_values_filters_unknown(self, svc) -> None:
        """DB 实时判定：已收录值不返回，未收录值返回。"""
        svc._repo.item_exists = AsyncMock(
            side_effect=lambda dict_type, value: not (dict_type == "currency" and value == "XXX")
        )
        unknown = await svc.verify_values(
            [
                {"dict_type": "currency", "value": "CNY"},
                {"dict_type": "currency", "value": "XXX"},
            ]
        )
        assert unknown == [{"dict_type": "currency", "value": "XXX"}]

    async def test_verify_values_dedups(self, svc) -> None:
        """同 (dict_type, value) 重复提交只返回一条。"""
        svc._repo.item_exists = AsyncMock(return_value=False)
        unknown = await svc.verify_values(
            [
                {"dict_type": "unit", "value": "cnt"},
                {"dict_type": "unit", "value": "cnt"},
            ]
        )
        assert len(unknown) == 1

    async def test_notify_unknown_values_notifies_all_admins(self, svc) -> None:
        """无收录权限用户保存未收录值：通知全部 platform_admin。"""
        svc._repo.item_exists = AsyncMock(return_value=False)
        svc._repo.list_admin_ids = AsyncMock(return_value=[1, 2])
        mock_notify = AsyncMock()
        mock_notify._repo.find_recent_notification_by_value_key = AsyncMock(return_value=None)
        with patch("app.services.system_dict.service.NotifyService", return_value=mock_notify):
            result = await svc.notify_unknown_values(
                metric_code="m1",
                values=[{"dict_type": "currency", "value": "XXX"}],
                actor_id=9,
                actor_name="张三",
                note="历史存量",
            )
        assert result == {"notified": 2, "unknown": 1}
        assert mock_notify.notify_user.await_count == 2
        first_kwargs = mock_notify.notify_user.await_args_list[0].kwargs
        assert mock_notify.notify_user.await_args_list[0].args[0] == 1  # user_id（位置参数）
        assert mock_notify.notify_user.await_args_list[0].args[1] == "dict.unknown_pending"
        assert first_kwargs["payload"]["value_key"] == "currency:XXX"
        assert first_kwargs["payload"]["actor_id"] == 9

    async def test_notify_unknown_values_skips_known(self, svc) -> None:
        """值已被收录：不通知（服务端复核兜底，防过期提交误报）。"""
        svc._repo.item_exists = AsyncMock(return_value=True)
        svc._repo.list_admin_ids = AsyncMock(return_value=[1])
        with patch("app.services.system_dict.service.NotifyService") as mock_cls:
            result = await svc.notify_unknown_values(
                metric_code="m1",
                values=[{"dict_type": "currency", "value": "CNY"}],
                actor_id=9,
                actor_name="张三",
            )
        assert result == {"notified": 0, "unknown": 0}
        mock_cls.assert_not_called()

    async def test_notify_unknown_values_no_admin(self, svc) -> None:
        """无 platform_admin 用户：不通知，返回 unknown 数（不阻断）。"""
        svc._repo.item_exists = AsyncMock(return_value=False)
        svc._repo.list_admin_ids = AsyncMock(return_value=[])
        with patch("app.services.system_dict.service.NotifyService") as mock_cls:
            result = await svc.notify_unknown_values(
                metric_code="m1",
                values=[{"dict_type": "currency", "value": "XXX"}],
                actor_id=9,
                actor_name="张三",
            )
        assert result == {"notified": 0, "unknown": 1}
        mock_cls.assert_not_called()

    async def test_notify_unknown_values_dedup_skips(self, svc) -> None:
        """窗口内同一未收录值已通知过某管理员：跳过（防刷屏）。"""
        svc._repo.item_exists = AsyncMock(return_value=False)
        svc._repo.list_admin_ids = AsyncMock(return_value=[1, 2])
        mock_notify = AsyncMock()
        # admin1 已收到（返回非 None）→ 跳过；admin2 未收到 → 通知
        mock_notify._repo.find_recent_notification_by_value_key = AsyncMock(
            side_effect=[object(), None]
        )
        with patch("app.services.system_dict.service.NotifyService", return_value=mock_notify):
            result = await svc.notify_unknown_values(
                metric_code="m1",
                values=[{"dict_type": "unit", "value": "cnt"}],
                actor_id=9,
                actor_name="张三",
            )
        assert result == {"notified": 1, "unknown": 1}
        assert mock_notify.notify_user.await_count == 1
        assert mock_notify.notify_user.await_args.args[0] == 2  # 仅 admin2 收到通知

    async def test_reject_unknown_value_invalid_notify_type(self, svc) -> None:
        """打回非字典收录待办通知：拒绝。"""
        notif = MagicMock()
        notif.template_code = "metric.created"
        mock_notify = AsyncMock()
        mock_notify.get_notification = AsyncMock(return_value=notif)
        with patch(
            "app.services.system_dict.service.NotifyService", return_value=mock_notify
        ), pytest.raises(BusinessError, match="字典收录待办"):
            await svc.reject_unknown_value(
                notification_id=1, reason="r", actor_id=5, actor_name="管理员"
            )
        mock_notify.notify_user.assert_not_called()

    async def test_reject_unknown_value_success(self, svc) -> None:
        """打回成功：通知提交人改用字典内值 + 办结原待办。"""
        notif = MagicMock()
        notif.template_code = "dict.unknown_pending"
        notif.payload = {
            "metric_code": "m1",
            "dict_type": "currency",
            "value": "XXX",
            "actor_id": 9,
        }
        mock_notify = AsyncMock()
        mock_notify.get_notification = AsyncMock(return_value=notif)
        with patch("app.services.system_dict.service.NotifyService", return_value=mock_notify):
            result = await svc.reject_unknown_value(
                notification_id=1,
                reason="请用 ISO 4217 标准币种",
                actor_id=5,
                actor_name="管理员",
            )
        assert result is notif
        # 通知提交人（原通知 payload.actor_id = 9）
        assert mock_notify.notify_user.await_args.args[0] == 9  # user_id（位置参数）
        assert mock_notify.notify_user.await_args.args[1] == "dict.unknown_rejected"
        kwargs = mock_notify.notify_user.await_args.kwargs
        assert "请用 ISO 4217 标准币种" in kwargs["body"]
        assert kwargs["payload"]["value"] == "XXX"
        # 原待办办结（不再出现在「仅待处理」）
        mock_notify.mark_handled.assert_awaited_once_with(1, 5, "platform_admin")


class TestBatch:
    """字典批量操作（207 语义：单条失败逐项标注，不影响其余）。"""

    @staticmethod
    def _install_nested(svc) -> None:
        """用真实 asynccontextmanager 模拟 begin_nested（MagicMock __aexit__ 吞异常）。"""
        svc._db.begin_nested = _nested  # type: ignore[method-assign]

    async def test_batch_create_all_success(self, svc) -> None:
        self._install_nested(svc)
        created = MagicMock()
        created.code = "minute"
        created.label = "分钟"
        svc.create_item = AsyncMock(return_value=created)

        from app.services.system_dict.schemas import DictItemCreate

        items = [DictItemCreate(label="分钟"), DictItemCreate(label="秒")]
        result = await svc.batch_create_items("granularity", items)
        assert len(result.succeeded) == 2
        assert len(result.failed) == 0
        assert result.succeeded[0].code == "minute"
        # 逐条复用 create_item（含编码自动生成/软删恢复/冲突判定）
        assert svc.create_item.await_count == 2

    async def test_batch_create_partial_duplicate(self, svc) -> None:
        """编码重复（业务错误）：仅该条失败（DUPLICATE_DICT_CODE），其余继续。"""
        self._install_nested(svc)
        from app.core.exceptions import ConflictError
        from app.services.system_dict.schemas import DictItemCreate

        created = MagicMock()
        created.code = "second"
        created.label = "秒"
        svc.create_item = AsyncMock(
            side_effect=[
                ConflictError("字典项已存在: granularity/day", error_code="DUPLICATE_DICT_CODE"),
                created,
            ]
        )
        items = [DictItemCreate(code="day", label="天"), DictItemCreate(code="second", label="秒")]
        result = await svc.batch_create_items("granularity", items)
        assert len(result.succeeded) == 1
        assert result.succeeded[0].code == "second"
        assert len(result.failed) == 1
        assert result.failed[0].code == "day"
        assert result.failed[0].error_code == "DUPLICATE_DICT_CODE"
        assert "已存在" in result.failed[0].message

    async def test_batch_toggle_all_success(self, svc) -> None:
        self._install_nested(svc)
        item = MagicMock()
        item.code = "day"
        item.label = "天"
        svc.deactivate_item = AsyncMock(return_value=item)
        result = await svc.batch_toggle_items("granularity", ["day", "month"], "deactivate")
        assert len(result.succeeded) == 2
        assert len(result.failed) == 0
        assert svc.deactivate_item.await_count == 2

    async def test_batch_toggle_partial_not_found(self, svc) -> None:
        """编码不存在：记为 NOT_FOUND 失败项，其余继续。"""
        self._install_nested(svc)
        item = MagicMock()
        item.code = "day"
        item.label = "天"
        svc.activate_item = AsyncMock(
            side_effect=[NotFoundError("字典项不存在: granularity/nope"), item]
        )
        result = await svc.batch_toggle_items("granularity", ["nope", "day"], "activate")
        assert len(result.succeeded) == 1
        assert result.succeeded[0].code == "day"
        assert len(result.failed) == 1
        assert result.failed[0].code == "nope"
        assert result.failed[0].error_code == "NOT_FOUND"

    async def test_batch_delete_partial_reference_rejected(self, svc) -> None:
        """被引用项不可删（HAS_REFERENCES）：仅该条失败，其余继续。"""
        self._install_nested(svc)
        svc.delete_item = AsyncMock(
            side_effect=[
                BusinessError("该字典项被 3 个指标引用，不可删除", error_code="HAS_REFERENCES"),
                None,
            ]
        )
        result = await svc.batch_delete_items("unit", ["CNY", "USD"])
        assert len(result.succeeded) == 1
        assert result.succeeded[0].code == "USD"
        assert len(result.failed) == 1
        assert result.failed[0].code == "CNY"
        assert result.failed[0].error_code == "HAS_REFERENCES"

    async def test_batch_delete_db_error_savepoint_continues(self, svc) -> None:
        """DB 级错误（如并发唯一键冲突）：savepoint 隔离，仅该条失败、后续继续。"""
        self._install_nested(svc)
        from sqlalchemy.exc import IntegrityError

        svc.delete_item = AsyncMock(
            side_effect=[
                IntegrityError("stmt", {}, Exception("boom")),
                None,
            ]
        )
        result = await svc.batch_delete_items("unit", ["A", "B"])
        assert len(result.succeeded) == 1
        assert result.succeeded[0].code == "B"
        assert len(result.failed) == 1
        assert result.failed[0].code == "A"
        assert result.failed[0].error_code == "INTERNAL"
