"""subject_domain Repository 单元测试（补齐覆盖率至 CRUD≥70%）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.subject_domain import SubjectDomain
from app.services.subject_domain.repository import SubjectDomainRepository


@pytest.fixture
def db() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.flush = AsyncMock()
    s.execute = AsyncMock()
    return s


@pytest.fixture
def repo(db: MagicMock) -> SubjectDomainRepository:
    return SubjectDomainRepository(db)


class TestSubjectDomainRepo:
    async def test_get_by_code_found(self, repo, db) -> None:
        result = MagicMock()
        result.scalar_one_or_none.return_value = SubjectDomain(id=1, code="sales")
        db.execute.return_value = result
        d = await repo.get_by_code("sales")
        assert d is not None
        assert d.code == "sales"

    async def test_get_by_code_not_found(self, repo, db) -> None:
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute.return_value = result
        assert await repo.get_by_code("nope") is None

    async def test_get_by_id(self, repo, db) -> None:
        result = MagicMock()
        result.scalar_one_or_none.return_value = SubjectDomain(id=7)
        db.execute.return_value = result
        assert (await repo.get_by_id(7)).id == 7

    async def test_list_all(self, repo, db) -> None:
        result = MagicMock()
        result.scalars.return_value.all.return_value = [SubjectDomain(id=1), SubjectDomain(id=2)]
        db.execute.return_value = result
        rows = await repo.list_all()
        assert len(rows) == 2

    async def test_list_all_with_status(self, repo, db) -> None:
        result = MagicMock()
        result.scalars.return_value.all.return_value = [SubjectDomain(id=1, status="active")]
        db.execute.return_value = result
        rows = await repo.list_all(status="active")
        assert len(rows) == 1

    async def test_list_children(self, repo, db) -> None:
        result = MagicMock()
        result.scalars.return_value.all.return_value = [SubjectDomain(id=2, parent_id=1)]
        db.execute.return_value = result
        rows = await repo.list_children(1)
        assert len(rows) == 1

    async def test_list_root_children(self, repo, db) -> None:
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute.return_value = result
        assert await repo.list_children(None) == []

    async def test_get_metric_count(self, repo, db) -> None:
        result = MagicMock()
        result.scalar.return_value = 5
        db.execute.return_value = result
        assert await repo.get_metric_count("sales") == 5

    async def test_create(self, repo, db) -> None:
        d = SubjectDomain(code="sales")
        out = await repo.create(d)
        assert out is d
        db.add.assert_called_once_with(d)
        db.flush.assert_awaited()

    async def test_update(self, repo, db) -> None:
        d = SubjectDomain(id=1)
        assert await repo.update(d) is d
        db.flush.assert_awaited()

    async def test_soft_delete(self, repo, db) -> None:
        d = SubjectDomain(id=1)
        await repo.soft_delete(d)
        assert d.deleted_at is not None

    async def test_code_exists_true(self, repo, db) -> None:
        result = MagicMock()
        result.scalar.return_value = 3
        db.execute.return_value = result
        assert await repo.code_exists("sales") is True

    async def test_code_exists_false(self, repo, db) -> None:
        result = MagicMock()
        result.scalar.return_value = 0
        db.execute.return_value = result
        assert await repo.code_exists("nope") is False

    async def test_count_children(self, repo, db) -> None:
        result = MagicMock()
        result.scalar.return_value = 2
        db.execute.return_value = result
        assert await repo.count_children(1) == 2
