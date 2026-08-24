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
    repo.create = AsyncMock(
        return_value=MagicMock(
            id=1,
            metric_code="sales_sales_amount_day",
            domain="sales",
            type="atomic",
            granularity="day",
            unit="CNY",
            aggregation="SUM",
            time_semantics="PERIOD",
            freshness="T1",
            dw_layer="DWD",
            metric_tier="T3",
            serving_mode="BATCH_ONLY",
            additivity="ADDITIVE",
            version=1,
            row_version=1,
            status="DRAFT",
            owner_id=1,
            pii_flag=False,
            compliance_reviewed=False,
        )
    )
    repo.create_version = AsyncMock()
    repo.list_metrics = AsyncMock(return_value=([], 0))
    return repo


class TestDomainValidation:
    async def test_create_metric_with_unconfigured_domain_is_allowed(
        self, mock_db, mock_repo
    ) -> None:
        """降级语义：domain 未在 subject_domain 配置（NotFoundError）时放行。

        对齐语义服务 _validate_domain_active 的降级约定——subject_domain 表为空/未种子时
        不阻断存量指标创建；仅"已配置但停用"的域才拦截（迁移 0026 空表兼容）。
        """
        with (
            patch("app.services.semantic.service.MetricRepository", return_value=mock_repo),
            patch("app.services.subject_domain.service.SubjectDomainRepository"),
            patch(
                "app.services.subject_domain.service.SubjectDomainService.validate_domain_active",
                new_callable=AsyncMock,
                side_effect=NotFoundError(
                    "主题域不存在: nonexistent", error_code="DOMAIN_NOT_FOUND"
                ),
            ),
        ):
            svc = MetricService(mock_db)
            req = MetricCreateRequest(
                metric_code="order_gmv_amount_day",
                name="测试订单量",
                domain="nonexistent",
                type="atomic",
                granularity="day",
                # OneData 原子层：原子指标 = 逻辑度量 + 聚合方式（mock 场景，无需真实度量行）
                measure_id=1,
                unit="cnt",
                aggregation="SUM",
                time_semantics="PERIOD",
                freshness="T1",
                dw_layer="DWD",
                definition_json={"expression": "SUM(amount)"},
            )
            # 域未配置 → 放行，创建成功（不抛 NotFoundError）
            result = await svc.create_metric(req, owner_id=1)
            assert result.metric_code == "sales_sales_amount_day"


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
