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

    async def _generate_dim_code(self, data: DimensionCreate) -> str:
        """自动生成唯一维度编码：``{domain_slug}_{name_slug}``，冲突自增后缀。"""
        from app.core.codegen import generate_unique_code, slugify_code

        domain_slug = slugify_code(data.domain)
        name_slug = slugify_code(data.name)
        if domain_slug and name_slug:
            base = f"{domain_slug}_{name_slug}"
        elif name_slug:
            base = f"dim_{name_slug}"
        elif domain_slug:
            base = f"dim_{domain_slug}"
        else:
            base = "dim"

        async def _exists(code: str) -> bool:
            return await self._repo.get_dimension(code) is not None

        return await generate_unique_code(base, _exists)

    async def create_dimension(
        self, data: DimensionCreate, actor_id: int | None = None
    ) -> Dimension:
        # 编码自动生成（FR-010：缺省时由系统生成，非人为创造）
        if not data.dim_code:
            data.dim_code = await self._generate_dim_code(data)
        if await self._repo.get_dimension(data.dim_code) is not None:
            raise ConflictError(f"维度编码已存在: {data.dim_code}", error_code="DIM_EXISTS")
        dim = Dimension(
            dim_code=data.dim_code,
            name=data.name,
            domain=data.domain,
            type=data.type,
            description=data.description,
            # PLAT-2: 认证身份优先，client 传入的 owner_id 仅作降级
            owner_id=actor_id if actor_id is not None else data.owner_id,
            status=DimensionStatus.DRAFT.value,
        )
        return await self._repo.save_dimension(dim)

    async def get_dimension(self, dim_code: str) -> Dimension:
        dim = await self._repo.get_dimension(dim_code)
        if dim is None:
            raise NotFoundError(f"维度不存在: {dim_code}")
        return dim

    async def list_dimensions(
        self, domain: str | None, status: str | None, keyword: str | None = None
    ) -> list[Dimension]:
        return await self._repo.list_dimensions(domain, status, keyword)

    async def update_dimension(self, dim_code: str, data: DimensionUpdate) -> Dimension:
        dim = await self._require(dim_code)
        if dim.status == DimensionStatus.DEPRECATED.value:
            raise UnisenseError(f"已废弃维度不可更新: {dim_code}", error_code="INVALID_STATE")
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

    async def publish_dimension(self, dim_code: str) -> Dimension:
        dim = await self._require(dim_code)
        if dim.status != DimensionStatus.DRAFT.value:
            raise UnisenseError(
                f"仅 DRAFT 状态可发布，当前 {dim.status}", error_code="INVALID_STATE"
            )
        dim.status = DimensionStatus.PUBLISHED.value
        await self._repo.commit()
        return dim

    async def deprecate_dimension(self, dim_code: str) -> Dimension:
        dim = await self._require(dim_code)
        dim.status = DimensionStatus.DEPRECATED.value
        await self._repo.commit()
        return dim

    async def _generate_member_code(self, data: DimensionMemberCreate) -> str:
        """自动生成维度成员编码：``{dim_code}_{name_slug}``，维度内唯一，冲突自增。"""
        from app.core.codegen import generate_unique_code, slugify_code

        name_slug = slugify_code(data.member_name)
        base = f"{data.dim_code}_{name_slug}" if name_slug else f"{data.dim_code}_member"

        async def _exists(code: str) -> bool:
            members = await self._repo.list_members(data.dim_code)
            return any(m.member_code == code for m in members)

        return await generate_unique_code(base, _exists)

    async def create_member(self, data: DimensionMemberCreate) -> DimensionMember:
        await self._require(data.dim_code)
        # 编码自动生成（FR-010：缺省时由系统生成）
        if not data.member_code:
            data.member_code = await self._generate_member_code(data)
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

    async def create_mapping(
        self, data: DimensionMappingCreate, actor_id: int | None = None
    ) -> DimensionMapping:
        mapping = DimensionMapping(
            source_dim_code=data.source_dim_code,
            target_dim_code=data.target_dim_code,
            mapping_type=data.mapping_type,
            expression=data.expression,
            # PLAT-2: 认证身份优先，client 传入的 created_by 仅作降级
            created_by=actor_id if actor_id is not None else data.created_by,
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
