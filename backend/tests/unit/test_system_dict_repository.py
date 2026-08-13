"""system_dict Repository 单元测试（补齐覆盖率至 CRUD≥70%）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.system_dict import SystemDict
from app.services.system_dict.repository import SystemDictRepository


@pytest.fixture
def db() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.flush = AsyncMock()
    s.execute = AsyncMock()
    return s


@pytest.fixture
def repo(db: MagicMock) -> SystemDictRepository:
    return SystemDictRepository(db)


class TestSystemDictRepo:
    async def test_list_by_type(self, repo, db) -> None:
        result = MagicMock()
        result.scalars.return_value.all.return_value = [SystemDict(id=1)]
        db.execute.return_value = result
        rows = await repo.list_by_type("granularity")
        assert len(rows) == 1

    async def test_list_by_type_no_status(self, repo, db) -> None:
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute.return_value = result
        assert await repo.list_by_type("granularity", None) == []

    async def test_get_item_found(self, repo, db) -> None:
        result = MagicMock()
        result.scalar_one_or_none.return_value = SystemDict(id=1, code="day")
        db.execute.return_value = result
        assert (await repo.get_item("granularity", "day")).code == "day"

    async def test_get_item_not_found(self, repo, db) -> None:
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute.return_value = result
        assert await repo.get_item("granularity", "nope") is None

    async def test_item_exists_true(self, repo, db) -> None:
        result = MagicMock()
        result.scalar.return_value = 1
        db.execute.return_value = result
        assert await repo.item_exists("granularity", "day") is True

    async def test_dict_type_exists(self, repo, db) -> None:
        result = MagicMock()
        result.scalar.return_value = 2
        db.execute.return_value = result
        assert await repo.dict_type_exists("granularity") is True

    async def test_code_exists_in_type(self, repo, db) -> None:
        result = MagicMock()
        result.scalar.return_value = 0
        db.execute.return_value = result
        assert await repo.code_exists_in_type("granularity", "minute") is False

    async def test_create(self, repo, db) -> None:
        item = SystemDict(dict_type="granularity", code="minute")
        out = await repo.create(item)
        assert out is item
        db.add.assert_called_once_with(item)
        db.flush.assert_awaited()

    async def test_update(self, repo, db) -> None:
        item = SystemDict(id=1)
        assert await repo.update(item) is item
        db.flush.assert_awaited()

    async def test_soft_delete(self, repo, db) -> None:
        item = SystemDict(id=1)
        await repo.soft_delete(item)
        assert item.deleted_at is not None

    async def test_list_dict_types(self, repo, db) -> None:
        result = MagicMock()
        result.all.return_value = [("granularity",), ("unit",)]
        db.execute.return_value = result
        assert await repo.list_dict_types() == ["granularity", "unit"]
