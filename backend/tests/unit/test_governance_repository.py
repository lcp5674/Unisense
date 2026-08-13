"""governance 服务 Repository 单测（补齐覆盖率）。

针对 governance/repository.py 的 26% 覆盖率，补充以下场景：
- get_role_by_name, create_role
- create_grant, get_grant
- list_grants (with/without filters)
- set_grant_status (revoke)
- classification_rescan helpers
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.data_source import DBCatalog
from app.models.governance import (
    Classification,
    Grant,
    GrantStatus,
    GrantType,
    Role,
    RoleName,
    SensitivityLevel,
)
from app.models.metric import Metric
from app.services.governance.repository import GovernanceRepository


@pytest.fixture
def db() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture
def repo(db: MagicMock) -> GovernanceRepository:
    return GovernanceRepository(db)


class TestGovernanceRepository:
    async def test_get_role_by_name_found(self, repo: GovernanceRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = Role(name=RoleName.PLATFORM_ADMIN)
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_role_by_name(RoleName.PLATFORM_ADMIN)
        assert result is not None
        assert result.name == RoleName.PLATFORM_ADMIN

    async def test_get_role_by_name_not_found(self, repo: GovernanceRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_role_by_name(RoleName.VIEWER)
        assert result is None

    async def test_create_role(self, repo: GovernanceRepository) -> None:
        role = Role(name=RoleName.VIEWER)
        result = await repo.create_role(role)
        assert result is role
        repo._db.add.assert_called_once_with(role)
        repo._db.flush.assert_called_once()
        repo._db.refresh.assert_called_once_with(role)

    async def test_create_grant(self, repo: GovernanceRepository) -> None:
        grant = Grant(user_id=1, role_id=2)
        result = await repo.create_grant(grant)
        assert result is grant
        repo._db.add.assert_called_once_with(grant)

    async def test_get_grant_found(self, repo: GovernanceRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = Grant(id=1, user_id=1)
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_grant(grant_id=1)
        assert result is not None
        assert result.id == 1

    async def test_get_grant_not_found(self, repo: GovernanceRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_grant(grant_id=999)
        assert result is None

    async def test_list_grants_no_filters(self, repo: GovernanceRepository) -> None:
        mock_count = MagicMock()
        mock_count.scalar.return_value = 5
        mock_rows = MagicMock()
        mock_rows.scalars.return_value.all.return_value = [Grant(id=1), Grant(id=2)]
        repo._db.execute = AsyncMock(side_effect=[mock_count, mock_rows])
        results, total = await repo.list_grants(
            user_id=None, domain=None, status=None, page=1, page_size=10
        )
        assert len(results) == 2
        assert total == 5

    async def test_list_grants_with_user_id(self, repo: GovernanceRepository) -> None:
        mock_count = MagicMock()
        mock_count.scalar.return_value = 1
        mock_rows = MagicMock()
        mock_rows.scalars.return_value.all.return_value = [Grant(id=1, user_id=5)]
        repo._db.execute = AsyncMock(side_effect=[mock_count, mock_rows])
        results, total = await repo.list_grants(
            user_id=5, domain=None, status=None, page=1, page_size=10
        )
        assert len(results) == 1
        assert total == 1

    async def test_revoke_grant(self, repo: GovernanceRepository) -> None:
        grant = Grant(id=1, user_id=1, status=GrantStatus.ACTIVE)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = grant
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.set_grant_status(grant, GrantStatus.REVOKED, reason="revoked")
        assert result.status == GrantStatus.REVOKED


class TestGovernanceRepositoryExtended:
    """governance 仓储剩余分支（find/list/expire/classification 等）。"""

    @staticmethod
    def _result(row: object | None) -> MagicMock:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = row
        return mock_result

    async def test_find_active_grant_all_none(self, repo: GovernanceRepository) -> None:
        grant = Grant(id=1, user_id=1)
        repo._db.execute = AsyncMock(return_value=self._result(grant))
        result = await repo.find_active_grant(1, None, None, GrantType.READ)
        assert result is grant
        stmt = repo._db.execute.call_args.args[0]
        where_str = " ".join(str(c) for c in stmt.whereclause.clauses)
        assert "role_id IS NULL" in where_str
        assert "domain IS NULL" in where_str

    async def test_find_active_grant_with_role_and_domain(self, repo: GovernanceRepository) -> None:
        repo._db.execute = AsyncMock(return_value=self._result(None))
        result = await repo.find_active_grant(2, 3, "sales", GrantType.WRITE)
        assert result is None
        stmt = repo._db.execute.call_args.args[0]
        where_str = " ".join(str(c) for c in stmt.whereclause.clauses)
        assert "role_id = :role_id_1" in where_str
        assert "domain = :domain_1" in where_str

    async def test_list_grants_with_domain_and_status(self, repo: GovernanceRepository) -> None:
        mock_count = MagicMock()
        mock_count.scalar.return_value = 2
        mock_rows = MagicMock()
        mock_rows.scalars.return_value.all.return_value = [Grant(id=1), Grant(id=2)]
        repo._db.execute = AsyncMock(side_effect=[mock_count, mock_rows])
        results, total = await repo.list_grants(
            user_id=None, domain="sales", status="ACTIVE", page=2, page_size=5
        )
        assert len(results) == 2
        assert total == 2
        stmt = repo._db.execute.call_args_list[0].args[0]
        where_str = " ".join(str(c) for c in stmt.whereclause.clauses)
        assert "domain = :domain_1" in where_str
        assert "status = :status_1" in where_str

    async def test_active_grants_for_user(self, repo: GovernanceRepository) -> None:
        grants = [Grant(id=1), Grant(id=2)]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = grants
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.active_grants_for_user(7)
        assert result == grants

    async def test_set_grant_status_without_reason(self, repo: GovernanceRepository) -> None:
        grant = Grant(id=1, user_id=1, status=GrantStatus.ACTIVE)
        result = await repo.set_grant_status(grant, GrantStatus.REVOKED)
        assert result.status == GrantStatus.REVOKED
        assert result.reason is None

    async def test_expire_due_grants_with_rows(self, repo: GovernanceRepository) -> None:
        grants = [Grant(id=1, status=GrantStatus.ACTIVE), Grant(id=2, status=GrantStatus.ACTIVE)]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = grants
        repo._db.execute = AsyncMock(return_value=mock_result)
        from datetime import UTC, datetime

        result = await repo.expire_due_grants(datetime(2026, 8, 1, tzinfo=UTC))
        assert all(g.status == GrantStatus.EXPIRED for g in result)
        repo._db.flush.assert_awaited_once()

    async def test_expire_due_grants_without_rows(self, repo: GovernanceRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.expire_due_grants()
        assert result == []

    async def test_get_metric_by_code(self, repo: GovernanceRepository) -> None:
        metric = Metric(metric_code="gmv_daily")
        repo._db.execute = AsyncMock(return_value=self._result(metric))
        result = await repo.get_metric_by_code("gmv_daily")
        assert result is metric

    async def test_get_metric_by_code_not_found(self, repo: GovernanceRepository) -> None:
        repo._db.execute = AsyncMock(return_value=self._result(None))
        assert await repo.get_metric_by_code("nope") is None

    async def test_set_compliance_reviewed(self, repo: GovernanceRepository) -> None:
        metric = Metric(metric_code="m", compliance_reviewed=False)
        result = await repo.set_compliance_reviewed(metric, True)
        assert result.compliance_reviewed is True
        repo._db.flush.assert_awaited_once()

    async def test_list_catalog_all_filters(self, repo: GovernanceRepository) -> None:
        rows = [DBCatalog(id=1, entity_name="t"), DBCatalog(id=2, entity_name="t2")]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = rows
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.list_catalog("src1", [1, 2], 10)
        assert result == rows
        stmt = repo._db.execute.call_args.args[0]
        where_str = " ".join(str(c) for c in stmt.whereclause.clauses)
        assert "source_id = :source_id_1" in where_str
        assert "db_catalog.id IN" in where_str

    async def test_list_catalog_no_filters(self, repo: GovernanceRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.list_catalog(None, None, 5)
        assert result == []

    async def test_update_catalog_sensitivity(self, repo: GovernanceRepository) -> None:
        await repo.update_catalog_sensitivity(1, SensitivityLevel.CONFIDENTIAL)
        repo._db.execute.assert_awaited_once()
        stmt = repo._db.execute.call_args.args[0]
        assert stmt.table.name == "db_catalog"
        assert "sensitivity_level" in str(stmt)

    async def test_get_classification(self, repo: GovernanceRepository) -> None:
        cls = Classification(catalog_id=1, sensitivity_level=SensitivityLevel.PII)
        repo._db.execute = AsyncMock(return_value=self._result(cls))
        result = await repo.get_classification(1)
        assert result is cls

    async def test_upsert_classification_updates_existing(self, repo: GovernanceRepository) -> None:
        existing = Classification(
            catalog_id=1,
            sensitivity_level=SensitivityLevel.INTERNAL,
            classified_by="rule_engine",
            model_version="v1",
        )
        repo._db.execute = AsyncMock(return_value=self._result(existing))
        result = await repo.upsert_classification(
            1, SensitivityLevel.PII, [{"col": "phone"}], "admin", "v2"
        )
        assert result is existing
        assert result.sensitivity_level == SensitivityLevel.PII
        assert result.pii_columns == [{"col": "phone"}]
        assert result.model_version == "v2"
        repo._db.flush.assert_awaited_once()

    async def test_upsert_classification_creates_new(self, repo: GovernanceRepository) -> None:
        repo._db.execute = AsyncMock(return_value=self._result(None))
        result = await repo.upsert_classification(5, SensitivityLevel.PUBLIC, [], "admin", "v1")
        assert result.catalog_id == 5
        assert result.sensitivity_level == SensitivityLevel.PUBLIC
        repo._db.add.assert_called_once_with(result)
        repo._db.refresh.assert_awaited_once_with(result)
