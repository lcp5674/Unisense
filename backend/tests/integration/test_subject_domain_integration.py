"""主题域+字典校验集成测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import BusinessError, NotFoundError
from app.services.semantic.schemas import MetricCreateRequest
from app.services.semantic.service import MetricService


@pytest.fixture
def mock_db():
    db = AsyncMock()
    return db


@pytest.fixture
def mock_repo():
    repo = AsyncMock()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=MagicMock(
        id=1, metric_code="sales_sales_amount_day", domain="sales", type="atomic",
        granularity="day", unit="CNY", aggregation="SUM", time_semantics="PERIOD",
        freshness="T1", dw_layer="DWD", metric_tier="T3", serving_mode="BATCH_ONLY",
        additivity="ADDITIVE", version=1, row_version=1, status="DRAFT", owner_id=1,
        pii_flag=False, compliance_reviewed=False,
    ))
    repo.create_version = AsyncMock()
    repo.list_metrics = AsyncMock(return_value=([], 0))
    return repo


class TestDomainValidation:
    async def test_create_metric_with_nonexistent_domain(self, mock_db, mock_repo) -> None:
        """domain 不存在时应抛 NotFoundError。"""
        with patch("app.services.semantic.service.MetricRepository", return_value=mock_repo), \
             patch("app.services.subject_domain.service.SubjectDomainRepository"), \
             patch(
                 "app.services.subject_domain.service.SubjectDomainService.validate_domain_active",
                 new_callable=AsyncMock,
                 side_effect=NotFoundError(
                     "主题域不存在: nonexistent", error_code="DOMAIN_NOT_FOUND"
                 ),
             ):

            svc = MetricService(mock_db)
            req = MetricCreateRequest(
                metric_code="test_gmv_amount_day", name="测试", domain="nonexistent",
                type="atomic", granularity="day", unit="cnt",
                aggregation="SUM", time_semantics="PERIOD", freshness="T1", dw_layer="DWD",
                definition_json={"expression": "SUM(amount)"},
            )
            with pytest.raises(NotFoundError, match="主题域不存在"):
                await svc.create_metric(req, owner_id=1)


class TestDictValidation:
    async def test_validate_dict_value_inactive(self) -> None:
        """停用的字典值应拒绝。"""
        from app.services.system_dict.service import SystemDictService

        mock_db = AsyncMock()
        svc = SystemDictService(mock_db)
        item = MagicMock()
        item.status = "inactive"
        svc._repo.get_item = AsyncMock(return_value=item)

        with pytest.raises(BusinessError, match="停用"):
            await svc.validate_dict_value("granularity", "minute")

    async def test_validate_dict_value_nonexistent(self) -> None:
        """不存在的字典值应抛 NotFoundError。"""
        from app.services.system_dict.service import SystemDictService

        mock_db = AsyncMock()
        svc = SystemDictService(mock_db)
        svc._repo.get_item = AsyncMock(return_value=None)

        with pytest.raises(NotFoundError):
            await svc.validate_dict_value("granularity", "nonexistent_val")
