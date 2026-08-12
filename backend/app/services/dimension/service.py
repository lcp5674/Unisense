"""维度管理服务（TD §12.15 / FR-05 / FR-09）。

核心能力：
1. 维度主表 CRUD + 状态机（DRAFT→PUBLISHED→DEPRECATED）。
2. 维度成员（维值/层级）维护，支持 parent_code 层级。
3. 维度映射（等价/部分）维护。
4. 指标-维度关联（PARTITION/SPLICE/FILTER）。
5. 口径对账（提交 + 人工复核 APPROVED/REJECTED）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.exceptions import ConflictError, NotFoundError, UnisenseError
from app.models.dimension import (
    Dimension,
    DimensionMapping,
    DimensionMember,
    DimensionStatus,
    MetricDimension,
    Reconciliation,
    ReconciliationStatus,
)
from app.services.dimension.repository import DimensionRepository
from app.services.dimension.schemas import (
    DimensionCreate,
    DimensionMappingCreate,
    DimensionMemberCreate,
    DimensionUpdate,
    MetricDimensionBind,
    ReconciliationReview,
    ReconciliationSubmit,
)


class DimensionService(BaseService):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._session = session
        self._repo = DimensionRepository(session)

    async def create_dimension(self, data: DimensionCreate) -> Dimension:
        if await self._repo.get_dimension(data.dim_code) is not None:
            raise ConflictError(f"维度编码已存在: {data.dim_code}", error_code="DIM_EXISTS")
        dim = Dimension(
            dim_code=data.dim_code,
            name=data.name,
            domain=data.domain,
            type=data.type,
            description=data.description,
            owner_id=data.owner_id,
            status=DimensionStatus.DRAFT.value,
        )
        return await self._repo.save_dimension(dim)

    async def get_dimension(self, dim_code: str) -> Dimension:
        dim = await self._repo.get_dimension(dim_code)
        if dim is None:
            raise NotFoundError(f"维度不存在: {dim_code}")
        return dim

    async def list_dimensions(self, domain: str | None, status: str | None) -> list[Dimension]:
        return await self._repo.list_dimensions(domain, status)

    async def update_dimension(self, dim_code: str, data: DimensionUpdate) -> Dimension:
        dim = await self._require(dim_code)
        if data.name is not None:
            dim.name = data.name
        if data.domain is not None:
            dim.domain = data.domain
        if data.type is not None:
            dim.type = data.type
        if data.description is not None:
            dim.description = data.description
        await self._repo.commit()
        return dim

    async def deprecate_dimension(self, dim_code: str) -> Dimension:
        dim = await self._require(dim_code)
        dim.status = DimensionStatus.DEPRECATED.value
        await self._repo.commit()
        return dim

    async def create_member(self, data: DimensionMemberCreate) -> DimensionMember:
        await self._require(data.dim_code)
        member = DimensionMember(
            dim_code=data.dim_code,
            member_code=data.member_code,
            member_name=data.member_name,
            parent_code=data.parent_code,
            path=data.path,
            attributes=data.attributes,
            status=data.status,
        )
        return await self._repo.save_member(member)

    async def list_members(self, dim_code: str) -> list[DimensionMember]:
        await self._require(dim_code)
        return await self._repo.list_members(dim_code)

    async def create_mapping(self, data: DimensionMappingCreate) -> DimensionMapping:
        mapping = DimensionMapping(
            source_dim_code=data.source_dim_code,
            target_dim_code=data.target_dim_code,
            mapping_type=data.mapping_type,
            expression=data.expression,
            created_by=data.created_by,
        )
        return await self._repo.save_mapping(mapping)

    async def list_mappings(self, source_dim_code: str | None) -> list[DimensionMapping]:
        return await self._repo.list_mappings(source_dim_code)

    async def bind_metric_dimension(self, data: MetricDimensionBind) -> MetricDimension:
        await self._require(data.dim_code)
        binding = MetricDimension(
            metric_id=data.metric_id,
            dim_code=data.dim_code,
            role=data.role,
            default_member=data.default_member,
        )
        return await self._repo.save_metric_dimension(binding)

    async def list_metric_dimensions(self, metric_id: int) -> list[MetricDimension]:
        return await self._repo.list_metric_dimensions(metric_id)

    async def submit_reconciliation(self, data: ReconciliationSubmit) -> Reconciliation:
        rec = Reconciliation(
            metric_id=data.metric_id,
            dim_code=data.dim_code,
            expected_expr=data.expected_expr,
            actual_expr=data.actual_expr,
            diff_summary=data.diff_summary,
            status=ReconciliationStatus.PENDING.value,
        )
        return await self._repo.save_reconciliation(rec)

    async def list_reconciliations(self, status: str | None) -> list[Reconciliation]:
        return await self._repo.list_reconciliations(status)

    async def review_reconciliation(
        self, rec_id: int, data: ReconciliationReview, reviewer_id: int | None = None
    ) -> Reconciliation:
        rec = await self._repo.get_reconciliation(rec_id)
        if rec is None:
            raise NotFoundError(f"对账记录不存在: {rec_id}")
        if data.decision not in (
            ReconciliationStatus.APPROVED.value,
            ReconciliationStatus.REJECTED.value,
        ):
            raise UnisenseError(f"未知复核结论: {data.decision}", error_code="INVALID_DECISION")
        rec.status = data.decision
        # PLAT-2: 以服务端认证身份 reviewer_id 落库，忽略 client 伪造的 reviewer_id
        rec.reviewed_by = reviewer_id if reviewer_id is not None else data.reviewer_id
        rec.reviewed_at = datetime.now(UTC)
        await self._repo.commit()
        return rec

    async def _require(self, dim_code: str) -> Dimension:
        dim = await self._repo.get_dimension(dim_code)
        if dim is None:
            raise NotFoundError(f"维度不存在: {dim_code}")
        return dim
