"""维度管理 Repository（TD §12.15 / FR-05 / FR-09）。"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dimension import (
    Dimension,
    DimensionMapping,
    DimensionMember,
    MetricDimension,
    Reconciliation,
)
from app.models.metric import Metric


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
        self,
        domain: str | None,
        status: str | None,
        keyword: str | None = None,
        owner_id: int | None = None,
    ) -> list[tuple[Dimension, int]]:
        """列出维度并附带绑定指标数（LEFT JOIN 聚合，未绑定的维度计数为 0）。"""
        stmt = (
            select(Dimension, func.count(MetricDimension.id))
            .outerjoin(MetricDimension, MetricDimension.dim_code == Dimension.dim_code)
            .group_by(Dimension.id)
        )
        if domain:
            stmt = stmt.where(Dimension.domain == domain)
        if status:
            stmt = stmt.where(Dimension.status == status)
        if owner_id is not None:
            stmt = stmt.where(Dimension.owner_id == owner_id)
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
        rows = (await self._session.execute(stmt)).all()
        return [(dim, count) for dim, count in rows]

    async def save_member(self, obj: DimensionMember) -> DimensionMember:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def list_members(self, dim_code: str) -> list[DimensionMember]:
        stmt = select(DimensionMember).where(DimensionMember.dim_code == dim_code)
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_member(self, dim_code: str, member_code: str) -> DimensionMember | None:
        stmt = (
            select(DimensionMember)
            .where(DimensionMember.dim_code == dim_code)
            .where(DimensionMember.member_code == member_code)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def delete_members(self, members: list[DimensionMember]) -> None:
        """物理删除一组维度成员（级联子树时一次性删除，避免逐条 N+1）。"""
        for member in members:
            await self._session.delete(member)
        await self._session.flush()

    async def save_mapping(self, obj: DimensionMapping) -> DimensionMapping:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def list_mappings(self, source_dim_code: str | None) -> list[DimensionMapping]:
        stmt = select(DimensionMapping)
        if source_dim_code:
            stmt = stmt.where(DimensionMapping.source_dim_code == source_dim_code)
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_mapping(self, mapping_id: int) -> DimensionMapping | None:
        stmt = select(DimensionMapping).where(DimensionMapping.id == mapping_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def delete_mapping(self, obj: DimensionMapping) -> None:
        await self._session.delete(obj)
        await self._session.flush()

    async def save_metric_dimension(self, obj: MetricDimension) -> MetricDimension:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def list_metric_dimensions(self, metric_id: int) -> list[MetricDimension]:
        stmt = select(MetricDimension).where(MetricDimension.metric_id == metric_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def delete_metric_dimension(
        self, metric_id: int, dim_code: str
    ) -> MetricDimension | None:
        """删除指标-维度绑定关系（解绑）；不存在返回 None。"""
        stmt = select(MetricDimension).where(
            MetricDimension.metric_id == metric_id,
            MetricDimension.dim_code == dim_code,
        )
        obj = (await self._session.execute(stmt)).scalar_one_or_none()
        if obj is None:
            return None
        await self._session.delete(obj)
        await self._session.flush()
        return obj


    async def list_dimension_metrics(self, dim_code: str) -> list[tuple[MetricDimension, Metric]]:
        """按维度查绑定指标：join Metric 拿 metric_code/name/status（治理追溯）。"""
        stmt = (
            select(MetricDimension, Metric)
            .join(Metric, Metric.id == MetricDimension.metric_id)
            .where(MetricDimension.dim_code == dim_code)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(binding, metric) for binding, metric in rows]

    async def save_reconciliation(self, obj: Reconciliation) -> Reconciliation:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def list_reconciliations(
        self, status: str | None
    ) -> list[tuple[Reconciliation, Metric | None]]:
        """列出对账记录并 LEFT JOIN Metric 取指标编码/名称；metric 缺失时返回 None。"""
        stmt = select(Reconciliation, Metric).outerjoin(
            Metric, Metric.id == Reconciliation.metric_id
        )
        if status:
            stmt = stmt.where(Reconciliation.status == status)
        rows = (await self._session.execute(stmt)).all()
        return [(rec, metric) for rec, metric in rows]

    async def get_reconciliation(self, rec_id: int) -> Reconciliation | None:
        stmt = select(Reconciliation).where(Reconciliation.id == rec_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def rename_dimension_references(self, old_code: str, new_code: str) -> None:
        """级联重命名维度编码在引用表中的全部引用（事务内）。

        维度编码被 3 张表引用（字符串外键，非 DB FK 约束）：
        - ``dimension_member.dim_code``：维度成员归属
        - ``dimension_mapping.source_dim_code`` / ``target_dim_code``：映射两端
        - ``metric_dimension.dim_code``：指标-维度绑定

        编辑维度编码时须同步更新这些引用，否则会留下悬挂引用。
        """
        from sqlalchemy import update

        await self._session.execute(
            update(DimensionMember)
            .where(DimensionMember.dim_code == old_code)
            .values(dim_code=new_code)
        )
        await self._session.execute(
            update(DimensionMapping)
            .where(DimensionMapping.source_dim_code == old_code)
            .values(source_dim_code=new_code)
        )
        await self._session.execute(
            update(DimensionMapping)
            .where(DimensionMapping.target_dim_code == old_code)
            .values(target_dim_code=new_code)
        )
        await self._session.execute(
            update(MetricDimension)
            .where(MetricDimension.dim_code == old_code)
            .values(dim_code=new_code)
        )

    async def commit(self) -> None:
        await self._session.commit()
