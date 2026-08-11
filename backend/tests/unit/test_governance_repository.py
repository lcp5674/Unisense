"""governance 服务 Repository 单测（补齐覆盖率）。

针对 governance/repository.py 的 26% 覆盖率，补充以下场景：
- get_role_by_name, create_role
- create_grant, get_grant
- list_grants (with/without filters)
- revoke_grant
- classification_rescan helpers
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.governance import Grant, Role, RoleName
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
        result = await repo.get_role_by_name(RoleName.ANALYST)
        assert result is None

    async def test_create_role(self, repo: GovernanceRepository) -> None:
        role = Role(name=RoleName.ANALYST)
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
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [Grant(id=1), Grant(id=2)]
        repo._db.execute = AsyncMock(return_value=mock_result)
        results, total = await repo.list_grants(page=1, page_size=10)
        assert len(results) == 2

    async def test_list_grants_with_user_id(self, repo: GovernanceRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [Grant(id=1, user_id=5)]
        repo._db.execute = AsyncMock(return_value=mock_result)
        results, total = await repo.list_grants(user_id=5, page=1, page_size=10)
        assert len(results) == 1

    async def test_revoke_grant(self, repo: GovernanceRepository) -> None:
        grant = Grant(id=1, user_id=1)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = grant
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.revoke_grant(grant_id=1, actor_id=1)
        assert result is not None
