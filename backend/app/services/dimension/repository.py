"""维度管理 Repository（TD §12.15 / FR-05 / FR-09）。"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dimension import (
    Dimension,
    DimensionMapping,
    DimensionMember,
    MetricDimension,
    Reconciliation,
)


class DimensionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_dimension(self, obj: Dimension) -> Dimension:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def get_dimension(self, dim_code: str) -> Dimension | None:
        stmt = select(Dimension).where(Dimension.dim_code == dim_code)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_dimensions(
        self, domain: str | None, status: str | None, keyword: str | None = None
    ) -> list[Dimension]:
        stmt = select(Dimension)
        if domain:
            stmt = stmt.where(Dimension.domain == domain)
        if status:
            stmt = stmt.where(Dimension.status == status)
        if keyword:
            # 参数化 LIKE + 通配符转义（对齐 FR-035：% / _ 须转义，防模糊放大）
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(
                or_(
                    Dimension.dim_code.like(f"%{escaped}%"),
                    Dimension.name.like(f"%{escaped}%"),
                    Dimension.description.like(f"%{escaped}%"),
                )
            )
        return list((await self._session.execute(stmt)).scalars().all())

    async def save_member(self, obj: DimensionMember) -> DimensionMember:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def list_members(self, dim_code: str) -> list[DimensionMember]:
        stmt = select(DimensionMember).where(DimensionMember.dim_code == dim_code)
        return list((await self._session.execute(stmt)).scalars().all())

    async def save_mapping(self, obj: DimensionMapping) -> DimensionMapping:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def list_mappings(self, source_dim_code: str | None) -> list[DimensionMapping]:
        stmt = select(DimensionMapping)
        if source_dim_code:
            stmt = stmt.where(DimensionMapping.source_dim_code == source_dim_code)
        return list((await self._session.execute(stmt)).scalars().all())

    async def save_metric_dimension(self, obj: MetricDimension) -> MetricDimension:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def list_metric_dimensions(self, metric_id: int) -> list[MetricDimension]:
        stmt = select(MetricDimension).where(MetricDimension.metric_id == metric_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def save_reconciliation(self, obj: Reconciliation) -> Reconciliation:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def list_reconciliations(self, status: str | None) -> list[Reconciliation]:
        stmt = select(Reconciliation)
        if status:
            stmt = stmt.where(Reconciliation.status == status)
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_reconciliation(self, rec_id: int) -> Reconciliation | None:
        stmt = select(Reconciliation).where(Reconciliation.id == rec_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def commit(self) -> None:
        await self._session.commit()
