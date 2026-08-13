"""系统字典 Service 单元测试。"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

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
