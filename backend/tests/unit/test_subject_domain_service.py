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


class TestGetDomainWithCount:
    async def test_get_domain_with_count(self, svc) -> None:
        from datetime import UTC, datetime

        domain = MagicMock()
        domain.id = 1
        domain.code = "sales"
        domain.name = "销售"
        domain.parent_id = None
        domain.level = 1
        domain.path = "1"
        domain.sort_order = 0
        domain.status = "active"
        domain.defaults_json = {}
        domain.description = None
        domain.owner_id = 1
        domain.created_at = datetime.now(UTC)
        domain.updated_at = datetime.now(UTC)
        svc._repo.get_by_code = AsyncMock(return_value=domain)
        svc._repo.get_metric_count = AsyncMock(return_value=7)

        resp = await svc.get_domain_with_count("sales")
        assert resp.code == "sales"
        assert resp.metric_count == 7


class TestListTree:
    async def test_list_tree_builds_hierarchy(self, svc) -> None:
        root = MagicMock()
        root.id = 1
        root.code = "sales"
        root.name = "销售"
        root.parent_id = None
        root.level = 1
        root.sort_order = 0
        root.status = "active"

        child = MagicMock()
        child.id = 2
        child.code = "sales_order"
        child.name = "订单"
        child.parent_id = 1
        child.level = 2
        child.sort_order = 1
        child.status = "active"

        svc._repo.list_all = AsyncMock(return_value=[root, child])
        svc._repo.get_metric_count = AsyncMock(side_effect=[3, 5])

        tree = await svc.list_tree()
        assert len(tree) == 1
        assert tree[0].code == "sales"
        assert len(tree[0].children) == 1
        assert tree[0].children[0].code == "sales_order"


class TestUpdateDomain:
    async def test_update_domain_fields(self, svc) -> None:
        domain = MagicMock()
        domain.code = "sales"
        svc._repo.get_by_code = AsyncMock(return_value=domain)
        svc._repo.update = AsyncMock(return_value=domain)

        from app.services.subject_domain.schemas import SubjectDomainUpdate
        data = SubjectDomainUpdate(name="销售域", sort_order=9, description="desc", owner_id=3)
        result = await svc.update_domain("sales", data)
        assert result.code == "sales"
        assert domain.name == "销售域"
        assert domain.sort_order == 9


class TestToggleMore:
    async def test_deactivate_domain(self, svc) -> None:
        domain = MagicMock()
        domain.code = "sales"
        domain.status = "active"
        svc._repo.get_by_code = AsyncMock(return_value=domain)
        svc._repo.update = AsyncMock(return_value=domain)
        result = await svc.deactivate_domain("sales")
        assert result.status == "inactive"

    async def test_activate_domain_without_parent(self, svc) -> None:
        domain = MagicMock()
        domain.code = "sales"
        domain.parent_id = None
        domain.status = "inactive"
        svc._repo.get_by_code = AsyncMock(return_value=domain)
        svc._repo.update = AsyncMock(return_value=domain)
        result = await svc.activate_domain("sales")
        assert result.status == "active"


class TestUpdateDefaults:
    async def test_update_defaults(self, svc) -> None:
        domain = MagicMock()
        domain.code = "sales"
        svc._repo.get_by_code = AsyncMock(return_value=domain)
        svc._repo.update = AsyncMock(return_value=domain)

        from app.services.subject_domain.schemas import SubjectDomainDefaultsUpdate
        data = SubjectDomainDefaultsUpdate(defaults_json={"granularity": "day"})
        await svc.update_defaults("sales", data)
        assert domain.defaults_json == {"granularity": "day"}


class TestGetDomainMetrics:
    async def test_get_domain_metrics(self, svc) -> None:
        m = MagicMock()
        m.id = 1
        m.metric_code = "sales_gmv_amount_day"
        m.name = "GMV"
        m.status = "PUBLISHED"
        m.type = "atomic"
        result = MagicMock()
        result.scalars.return_value.all.return_value = [m]
        svc._db.execute = AsyncMock(return_value=result)

        rows = await svc.get_domain_metrics("sales")
        assert len(rows) == 1
        assert rows[0]["metric_code"] == "sales_gmv_amount_day"


class TestValidateDomainActive:
    async def test_validate_active(self, svc) -> None:
        domain = MagicMock()
        domain.code = "sales"
        domain.status = "active"
        svc._repo.get_by_code = AsyncMock(return_value=domain)
        assert (await svc.validate_domain_active("sales")).code == "sales"

    async def test_validate_inactive(self, svc) -> None:
        domain = MagicMock()
        domain.code = "sales"
        domain.status = "inactive"
        svc._repo.get_by_code = AsyncMock(return_value=domain)
        with pytest.raises(BusinessError):
            await svc.validate_domain_active("sales")
