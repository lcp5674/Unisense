"""系统字典 Service 单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import BusinessError, NotFoundError
from app.services.system_dict.service import SystemDictService


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def svc(mock_db):
    return SystemDictService(mock_db)


class TestCreateItem:
    async def test_create_item_success(self, svc) -> None:
        svc._repo.code_exists_in_type = AsyncMock(return_value=False)
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
        svc._repo.code_exists_in_type = AsyncMock(return_value=True)

        from app.services.system_dict.schemas import DictItemCreate
        data = DictItemCreate(code="day", label="天")
        from app.core.exceptions import ConflictError
        with pytest.raises(ConflictError):
            await svc.create_item("granularity", data)


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
        svc._repo.code_exists_in_type = AsyncMock(return_value=False)
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
