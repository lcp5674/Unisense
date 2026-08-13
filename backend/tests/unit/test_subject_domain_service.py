"""主题域 Service 单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import BusinessError, NotFoundError
from app.services.subject_domain.service import SubjectDomainService


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def svc(mock_db):
    return SubjectDomainService(mock_db)


class TestCreateDomain:
    async def test_create_root_domain(self, svc) -> None:
        """创建根域：level=1, path=自身id。"""
        svc._repo.code_exists = AsyncMock(return_value=False)
        domain = MagicMock()
        domain.id = 1
        domain.code = "sales"
        domain.level = 1
        domain.path = "1"
        svc._repo.create = AsyncMock(return_value=domain)
        svc._repo.update = AsyncMock(return_value=domain)

        from app.services.subject_domain.schemas import SubjectDomainCreate
        data = SubjectDomainCreate(code="sales", name="销售", owner_id=1)
        result = await svc.create_domain(data)
        assert result.code == "sales"
        assert result.level == 1

    async def test_create_child_domain_exceeds_3_levels(self, svc) -> None:
        """创建第4层域应被拒绝。"""
        parent = MagicMock()
        parent.id = 5
        parent.level = 3
        parent.status = "active"
        parent.path = "1.3.5"
        svc._repo.code_exists = AsyncMock(return_value=False)
        svc._repo.get_by_id = AsyncMock(return_value=parent)

        from app.services.subject_domain.schemas import SubjectDomainCreate
        data = SubjectDomainCreate(code="too_deep", name="太深了", parent_id=5, owner_id=1)
        with pytest.raises(BusinessError, match="最多"):
            await svc.create_domain(data)

    async def test_create_domain_duplicate_code(self, svc) -> None:
        """编码重复应抛 ConflictError。"""
        svc._repo.code_exists = AsyncMock(return_value=True)

        from app.services.subject_domain.schemas import SubjectDomainCreate
        data = SubjectDomainCreate(code="sales", name="销售", owner_id=1)
        from app.core.exceptions import ConflictError
        with pytest.raises(ConflictError):
            await svc.create_domain(data)


class TestDeleteDomain:
    async def test_delete_with_metrics_rejected(self, svc) -> None:
        """有关联指标的域不可删除。"""
        domain = MagicMock()
        domain.code = "sales"
        svc._repo.get_by_code = AsyncMock(return_value=domain)
        svc._repo.get_metric_count = AsyncMock(return_value=20)

        with pytest.raises(BusinessError, match="关联指标"):
            await svc.delete_domain("sales")

    async def test_delete_with_children_rejected(self, svc) -> None:
        """有子域的域不可删除。"""
        domain = MagicMock()
        domain.code = "sales"
        domain.id = 1
        svc._repo.get_by_code = AsyncMock(return_value=domain)
        svc._repo.get_metric_count = AsyncMock(return_value=0)
        svc._repo.count_children = AsyncMock(return_value=3)

        with pytest.raises(BusinessError, match="子域"):
            await svc.delete_domain("sales")


class TestToggleDomain:
    async def test_activate_child_with_inactive_parent(self, svc) -> None:
        """父域停用时不可启用子域。"""
        domain = MagicMock()
        domain.code = "sales_order"
        domain.parent_id = 5
        svc._repo.get_by_code = AsyncMock(return_value=domain)
        parent = MagicMock()
        parent.status = "inactive"
        svc._repo.get_by_id = AsyncMock(return_value=parent)

        with pytest.raises(BusinessError, match="父域"):
            await svc.activate_domain("sales_order")


class TestDefaults:
    async def test_get_defaults(self, svc) -> None:
        domain = MagicMock()
        domain.defaults_json = {"granularity": "day", "unit": "CNY"}
        svc._repo.get_by_code = AsyncMock(return_value=domain)

        result = await svc.get_defaults("sales")
        assert result["granularity"] == "day"
        assert result["unit"] == "CNY"

    async def test_domain_not_found(self, svc) -> None:
        svc._repo.get_by_code = AsyncMock(return_value=None)
        with pytest.raises(NotFoundError):
            await svc.get_domain("nonexistent")
