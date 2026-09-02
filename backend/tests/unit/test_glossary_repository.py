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

    async def test_get_term_by_id_found(self, repo: GlossaryRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = Term(term_code="T1", id=7)
        repo._session.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_term_by_id(7)
        assert result is not None
        assert result.id == 7

    async def test_get_term_by_id_not_found(self, repo: GlossaryRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        repo._session.execute = AsyncMock(return_value=mock_result)
        assert await repo.get_term_by_id(999) is None

    async def test_list_terms_no_filters(self, repo: GlossaryRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [Term(term_code="T1")]
        mock_result.scalar.return_value = 1
        repo._session.execute = AsyncMock(return_value=mock_result)
        rows, total = await repo.list_terms(
            domain=None, status=None, search=None, limit=10, offset=0
        )
        assert len(rows) == 1
        assert total == 1

    async def test_list_terms_with_filters(self, repo: GlossaryRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [Term(term_code="T1")]
        mock_result.scalar.return_value = 1
        repo._session.execute = AsyncMock(return_value=mock_result)
        rows, total = await repo.list_terms(
            domain="sales", status="PUBLISHED", search="用户", limit=10, offset=0
        )
        assert len(rows) == 1

    async def test_list_terms_reviewed_by_filters_approver_or_rejector(
        self, repo: GlossaryRepository
    ) -> None:
        """reviewed_by 过滤"我审过的"：approver_id 或 reject_reviewer_id 匹配（审批工作台）。"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_result.scalar.return_value = 0
        repo._session.execute = AsyncMock(return_value=mock_result)
        await repo.list_terms(
            domain=None, status=None, search=None, limit=10, offset=0, reviewed_by=9
        )
        stmt = repo._session.execute.call_args_list[1].args[0]
        literal_sql = str(
            stmt.compile(compile_kwargs={"literal_binds": True})
        )
        assert "term.approver_id = 9" in literal_sql
        assert "term.reject_reviewer_id = 9" in literal_sql

    async def test_list_terms_search_escapes_wildcards(self, repo: GlossaryRepository) -> None:
        """LIKE 通配符（% / _）须转义，防模糊放大（对齐 FR-035）。"""
        from sqlalchemy.dialects import mysql

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_result.scalar.return_value = 0
        repo._session.execute = AsyncMock(return_value=mock_result)
        await repo.list_terms(domain=None, status=None, search="100%_x", limit=10, offset=0)
        stmt = repo._session.execute.call_args_list[1].args[0]
        literal_sql = str(
            stmt.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})
        )
        # 生成 ESCAPE 子句；未转义的原始关键词（含裸 %/_）不得出现在 SQL 中
        assert "ESCAPE '/'" in literal_sql
        assert "100%_x" not in literal_sql

    async def test_list_terms_visible_regular_user_hides_others_draft(
        self, repo: GlossaryRepository
    ) -> None:
        """P0-3 读路径行级隔离：普通用户仅可见公开（PUBLISHED/DEPRECATED）+ 本人 DRAFT/REVIEW。"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_result.scalar.return_value = 0
        repo._session.execute = AsyncMock(return_value=mock_result)
        await repo.list_terms(
            domain=None,
            status=None,
            search=None,
            limit=10,
            offset=0,
            visible_actor_id=7,
            visible_role="analyst",
        )
        stmt = repo._session.execute.call_args_list[1].args[0]
        literal_sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "term.status IN ('PUBLISHED', 'DEPRECATED')" in literal_sql
        assert "term.owner_id = 7" in literal_sql
        # analyst 非 reviewer：REVIEW 仅本人可见（不额外放行全部 REVIEW）
        assert "term.status = 'REVIEW'" not in literal_sql.replace(
            "term.status IN ('PUBLISHED', 'DEPRECATED')", ""
        )

    async def test_list_terms_visible_reviewer_sees_review(
        self, repo: GlossaryRepository
    ) -> None:
        """评审人可看待审（REVIEW）术语——统一主数据审批工作台需展示全部待审项。"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_result.scalar.return_value = 0
        repo._session.execute = AsyncMock(return_value=mock_result)
        await repo.list_terms(
            domain=None,
            status=None,
            search=None,
            limit=10,
            offset=0,
            visible_actor_id=9,
            visible_role="reviewer",
        )
        stmt = repo._session.execute.call_args_list[1].args[0]
        literal_sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "term.status = 'REVIEW'" in literal_sql

    async def test_list_terms_visible_admin_no_filter(self, repo: GlossaryRepository) -> None:
        """管理角色不加可见性过滤（全量治理视角），外部 owner_id 仍透传。"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_result.scalar.return_value = 0
        repo._session.execute = AsyncMock(return_value=mock_result)
        await repo.list_terms(
            domain=None,
            status=None,
            search=None,
            limit=10,
            offset=0,
            owner_id=3,
            visible_actor_id=1,
            visible_role="platform_admin",
        )
        stmt = repo._session.execute.call_args_list[1].args[0]
        literal_sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "term.owner_id = 3" in literal_sql
        assert "PUBLISHED" not in literal_sql
        assert "DRAFT" not in literal_sql

    async def test_list_terms_visible_domain_admin_scoped_to_own_domain(
        self, repo: GlossaryRepository
    ) -> None:
        """域管理员读路径域收敛：绑定域 → 本域（全状态）+ 本人负责，不再全量可见。"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_result.scalar.return_value = 0
        repo._session.execute = AsyncMock(return_value=mock_result)
        await repo.list_terms(
            domain=None,
            status=None,
            search=None,
            limit=10,
            offset=0,
            visible_actor_id=2,
            visible_role="domain_admin",
            visible_user_domains=["outpatient"],
        )
        stmt = repo._session.execute.call_args_list[1].args[0]
        literal_sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "term.domain IN ('outpatient')" in literal_sql
        assert "term.owner_id = 2" in literal_sql
        assert "PUBLISHED" not in literal_sql

    async def test_list_terms_visible_domain_admin_no_domain_personal_view(
        self, repo: GlossaryRepository
    ) -> None:
        """未绑定域的 domain_admin → 退化个人视角（公开 + 本人负责），不泄露他人 DRAFT。"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_result.scalar.return_value = 0
        repo._session.execute = AsyncMock(return_value=mock_result)
        await repo.list_terms(
            domain=None,
            status=None,
            search=None,
            limit=10,
            offset=0,
            visible_actor_id=2,
            visible_role="domain_admin",
            visible_user_domains=None,
        )
        stmt = repo._session.execute.call_args_list[1].args[0]
        literal_sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "term.status IN ('PUBLISHED', 'DEPRECATED')" in literal_sql
        assert "term.owner_id = 2" in literal_sql
        assert "term.domain IN ('outpatient')" not in literal_sql

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
        mock_result.scalar.return_value = 1
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
