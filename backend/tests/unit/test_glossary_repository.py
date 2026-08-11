"""术语库 Repository 单测（补齐覆盖率）。

针对 glossary/repository.py 的 32% 覆盖率补充。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.glossary import GlossaryConflict, TermRelation, TermVersion
from app.models.term import Term
from app.services.glossary.repository import GlossaryRepository


@pytest.fixture
def db() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture
def repo(db: MagicMock) -> GlossaryRepository:
    return GlossaryRepository(db)


class TestTermRepo:
    async def test_save_term(self, repo: GlossaryRepository) -> None:
        term = Term(term_code="T1", name="活跃用户")
        result = await repo.save_term(term)
        assert result is term
        db = repo._session
        db.add.assert_called_once_with(term)

    async def test_get_term_found(self, repo: GlossaryRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = Term(term_code="T1")
        repo._session.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_term("T1")
        assert result is not None
        assert result.term_code == "T1"

    async def test_get_term_not_found(self, repo: GlossaryRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        repo._session.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_term("MISSING")
        assert result is None

    async def test_list_terms_no_filters(self, repo: GlossaryRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [Term(term_code="T1")]
        repo._session.execute = AsyncMock(return_value=mock_result)
        rows, total = await repo.list_terms(
            domain=None, status=None, search=None, limit=10, offset=0
        )
        assert len(rows) == 1
        assert total == 1

    async def test_list_terms_with_filters(self, repo: GlossaryRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [Term(term_code="T1")]
        repo._session.execute = AsyncMock(return_value=mock_result)
        rows, total = await repo.list_terms(
            domain="sales", status="PUBLISHED", search="用户", limit=10, offset=0
        )
        assert len(rows) == 1

    async def test_delete_term(self, repo: GlossaryRepository) -> None:
        term = Term(term_code="T1")
        await repo.delete_term(term)
        repo._session.delete.assert_called_once_with(term)

    async def test_all_terms(self, repo: GlossaryRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [Term(term_code="T1")]
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.all_terms()
        assert len(results) == 1


class TestConflictRepo:
    async def test_save_conflict(self, repo: GlossaryRepository) -> None:
        conflict = GlossaryConflict(id=1)
        result = await repo.save_conflict(conflict)
        assert result is conflict

    async def test_list_conflicts_no_status(self, repo: GlossaryRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [GlossaryConflict(id=1)]
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.list_conflicts(status=None)
        assert len(results) == 1

    async def test_list_conflicts_with_status(self, repo: GlossaryRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [GlossaryConflict(id=1)]
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.list_conflicts(status="OPEN")
        assert len(results) == 1

    async def test_get_conflict_found(self, repo: GlossaryRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = GlossaryConflict(id=1)
        repo._session.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_conflict(1)
        assert result is not None

    async def test_get_conflict_not_found(self, repo: GlossaryRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        repo._session.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_conflict(999)
        assert result is None


class TestVersionRelationRepo:
    async def test_save_term_version(self, repo: GlossaryRepository) -> None:
        version = TermVersion(term_id=1, version=2)
        result = await repo.save_term_version(version)
        assert result is version

    async def test_count_term_versions(self, repo: GlossaryRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [TermVersion(term_id=1)]
        repo._session.execute = AsyncMock(return_value=mock_result)
        count = await repo.count_term_versions(term_id=1)
        assert count == 1

    async def test_save_term_relation(self, repo: GlossaryRepository) -> None:
        relation = TermRelation(
            source_term_id=1,
            target_term_id=2,
            relation_type="RELATED",
        )
        result = await repo.save_term_relation(relation)
        assert result is relation

    async def test_commit(self, repo: GlossaryRepository) -> None:
        await repo.commit()
        repo._session.commit.assert_called_once()
