"""推荐服务 Repository 单测（补齐覆盖率）。

针对 recommend/repository.py 的 67% 覆盖率补充。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.lineage import LineageEdge
from app.models.term import Term
from app.services.recommend.repository import RecommendRepository


@pytest.fixture
def db() -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture
def repo(db: MagicMock) -> RecommendRepository:
    return RecommendRepository(db)


class TestRecommendRepository:
    async def test_related_edges(self, repo: RecommendRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [LineageEdge(id=1)]
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.related_edges(node="sales", limit=10)
        assert len(results) == 1

    async def test_related_edges_empty(self, repo: RecommendRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.related_edges(node="unknown", limit=10)
        assert results == []

    async def test_published_terms(self, repo: RecommendRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [Term(id=1, status="PUBLISHED")]
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.published_terms(limit=10)
        assert len(results) == 1

    async def test_published_terms_empty(self, repo: RecommendRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.published_terms(limit=10)
        assert results == []

    async def test_published_terms_domain_filter(self, repo: RecommendRepository) -> None:
        """P1-5 术语域收敛：传 domain 时 SQL 带 Term.domain 过滤 + 软删过滤。"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            Term(id=1, status="PUBLISHED", domain="sales")
        ]
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.published_terms(limit=10, domain="sales")
        assert len(results) == 1
        sql = str(
            repo._session.execute.call_args.args[0].compile(
                compile_kwargs={"literal_binds": True}
            )
        )
        assert "term.domain = 'sales'" in sql
        assert "term.deleted_at IS NULL" in sql

    async def test_published_terms_no_domain_keeps_soft_delete(
        self, repo: RecommendRepository
    ) -> None:
        """platform_admin 不限域：不传 domain 时无 Term.domain 条件，但软删过滤保留。"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        repo._session.execute = AsyncMock(return_value=mock_result)
        await repo.published_terms(limit=10)
        sql = str(
            repo._session.execute.call_args.args[0].compile(
                compile_kwargs={"literal_binds": True}
            )
        )
        assert "term.domain =" not in sql  # SELECT 列含 domain，仅断言 WHERE 条件
        assert "term.deleted_at IS NULL" in sql
