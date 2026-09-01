"""维度管理 Repository（TD §12.15 / FR-05 / FR-09）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import ColumnElement, func, or_, select
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

    async def soft_delete_dimension(self, dim_id: int) -> None:
        """软删维度：置 deleted_at（回收站可恢复）。"""
        from sqlalchemy import update

        await self._session.execute(
            update(Dimension)
            .where(Dimension.id == dim_id, Dimension.deleted_at.is_(None))
            .values(deleted_at=datetime.now(UTC))
        )

    async def restore_dimension(self, dim_id: int) -> None:
        """恢复软删维度：清除 deleted_at（回收站恢复）。"""
        from sqlalchemy import update

        await self._session.execute(
            update(Dimension)
            .where(Dimension.id == dim_id, Dimension.deleted_at.is_not(None))
            .values(deleted_at=None)
        )

    async def list_dimensions(
        self,
        domain: str | None,
        status: str | None,
        keyword: str | None = None,
        owner_id: int | None = None,
        *,
        reviewed_by: int | None = None,
        deleted: bool = False,
        limit: int = 20,
        offset: int = 0,
        visible_actor_id: int | None = None,
        visible_role: str | None = None,
    ) -> tuple[list[tuple[Dimension, int]], int]:
        """分页列出维度并附带绑定指标数，返回 (列表, total)。

        - LEFT JOIN 聚合取绑定指标数（未绑定计数 0）
        - total 用独立 count（不含 JOIN），与列表共用同一过滤条件，保证分页一致性
        - 对齐 glossary 的服务端分页（page/page_size），消除全量拉回的性能隐患
        - deleted=True 时列出已软删记录（回收站视图）
        - reviewed_by 非空时过滤"我审过的"（通过/驳回人 ID 匹配，供统一主数据审批工作台）
        - visible_actor_id/visible_role：读路径行级隔离（P0-3，对齐指标）——非管理角色
          仅可见公开状态（PUBLISHED/DEPRECATED）+ 本人负责的未发布（DRAFT/REVIEW）；
          评审人可看待审（REVIEW）。管理角色传 None 即不加过滤。
        """
        conditions = (
            [Dimension.deleted_at.is_not(None)]
            if deleted
            else [Dimension.deleted_at.is_(None)]
        )
        # P0-3 读路径行级隔离（对齐指标 list_metrics）：维度 DRAFT/REVIEW 是创建者私有
        # 工作区，他人不得窥探；公开状态（PUBLISHED/DEPRECATED）可被发现。
        if (
            visible_actor_id is not None
            and visible_role is not None
            and visible_role not in ("platform_admin", "domain_admin")
        ):
            visibility: list[ColumnElement[bool]] = [
                Dimension.status.in_(("PUBLISHED", "DEPRECATED")),
                Dimension.owner_id == visible_actor_id,
            ]
            if visible_role == "reviewer":
                # 评审人可看待审（REVIEW）维度——统一主数据审批工作台需展示全部待审项
                visibility.append(Dimension.status == "REVIEW")
            conditions.append(or_(*visibility))
        if domain:
            conditions.append(Dimension.domain == domain)
        if status:
            conditions.append(Dimension.status == status)
        if owner_id is not None:
            conditions.append(Dimension.owner_id == owner_id)
        if reviewed_by is not None:
            conditions.append(
                or_(
                    Dimension.approver_id == reviewed_by,
                    Dimension.reject_reviewer_id == reviewed_by,
                )
            )
        if keyword:
            # 参数化 LIKE + 通配符转义（对齐 FR-035：% / _ 须转义，防模糊放大）
            escaped = keyword.replace("/", "//").replace("%", "/%").replace("_", "/_")
            conditions.append(
                or_(
                    Dimension.dim_code.like(f"%{escaped}%", escape="/"),
                    Dimension.name.like(f"%{escaped}%", escape="/"),
                    Dimension.description.like(f"%{escaped}%", escape="/"),
                )
            )
        stmt = (
            select(Dimension, func.count(MetricDimension.id))
            .outerjoin(MetricDimension, MetricDimension.dim_code == Dimension.dim_code)
            .group_by(Dimension.id)
            .where(*conditions)
        )
        count_stmt = (
            select(func.count())
            .select_from(Dimension)
            .where(*conditions)
        )
        total = (await self._session.execute(count_stmt)).scalar() or 0
        rows = (
            await self._session.execute(
                stmt.order_by(Dimension.id.asc()).limit(limit).offset(offset)
            )
        ).all()
        return [(dim, count) for dim, count in rows], total

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

    async def list_mappings(
        self, source_dim_code: str | None, limit: int = 200, offset: int = 0
    ) -> tuple[list[DimensionMapping], int]:
        """分页列出维度映射，返回 (列表, total)（P10 服务端分页，防大映射集全量拉取）。"""
        conditions = [DimensionMapping.deleted_at.is_(None)]
        if source_dim_code:
            conditions.append(DimensionMapping.source_dim_code == source_dim_code)
        count_stmt = (
            select(func.count()).select_from(DimensionMapping).where(*conditions)
        )
        total = int((await self._session.execute(count_stmt)).scalar_one() or 0)
        stmt = (
            select(DimensionMapping)
            .where(*conditions)
            .order_by(DimensionMapping.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all()), total

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

    async def list_metric_dimensions(
        self, metric_id: int
    ) -> list[tuple[MetricDimension, Dimension]]:
        """按指标查绑定维度：join Dimension 拿 dim_code/status（治理追溯，对齐
        ``list_dimension_metrics`` 的 join 模式）。返回 ``(binding, dimension)`` 元组，
        供指标详情「关联维度」展示维度状态（未 join 前无法区分维度已废弃与否）。
        """
        stmt = (
            select(MetricDimension, Dimension)
            .join(Dimension, Dimension.dim_code == MetricDimension.dim_code)
            .where(MetricDimension.metric_id == metric_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(binding, dimension) for binding, dimension in rows]

    async def list_metrics_by_dimension(self, dim_code: str) -> list[Metric]:
        """查询绑定指定维度的全部指标（改编码联动回写口径声明用）。"""
        stmt = (
            select(Metric)
            .join(MetricDimension, MetricDimension.metric_id == Metric.id)
            .where(MetricDimension.dim_code == dim_code)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_metrics_declaring_dimension(self, dim_code: str) -> list[Metric]:
        """扫描口径声明 ``definition_json.dimensions`` 含 dim_code 的全部指标。

        维度改编码/废弃时，指标口径声明的权威来源有两处：
        - ``metric_dimension`` 绑定表（``list_metrics_by_dimension``）
        - ``definition_json.dimensions`` 用户**手工声明**（未必绑定）

        消费校验与血缘 USES_DIMENSION 边都以 ``definition_json.dimensions`` 为准，
        若只按绑定表回查，手工声明未绑定的维度会在改码后悬空 → 消费 FORBIDDEN_DIMENSION。
        用 ``JSON_CONTAINS`` 精确匹配数组元素（避免 LIKE 误命中嵌套字符串）。
        """
        stmt = select(Metric).where(
            func.json_contains(Metric.definition_json, f'"{dim_code}"', "$.dimensions")
        )
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

    async def count_metric_dimensions(self, dim_code: str) -> int:
        """统计维度被多少指标绑定（废弃保护：被绑定维度禁止废弃）。"""
        stmt = (
            select(func.count(MetricDimension.id))
            .where(MetricDimension.dim_code == dim_code)
        )
        return int((await self._session.execute(stmt)).scalar_one() or 0)

    async def count_bindings_by_default_member(
        self, dim_code: str, member_code: str
    ) -> int:
        """统计维度成员被多少指标绑定为默认成员（废弃保护：被引用成员禁止废弃）。

        对称于 ``count_metric_dimensions``：废弃维度主体受绑定保护，
        废弃成员同样受"被绑定为默认值"保护——否则指标绑定悬空。
        """
        stmt = (
            select(func.count(MetricDimension.id))
            .where(
                MetricDimension.dim_code == dim_code,
                MetricDimension.default_member == member_code,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one() or 0)

    async def save_reconciliation(self, obj: Reconciliation) -> Reconciliation:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def list_reconciliations(
        self,
        status: str | None,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[tuple[Reconciliation, Metric | None]], int]:
        """分页列出对账记录并 LEFT JOIN Metric 取指标编码/名称，返回 (列表, total)。

        P10 服务端分页：此前全量拉取（对账记录随治理动作增长，列表页可能 OOM）。
        """
        conditions = [Reconciliation.deleted_at.is_(None)]
        if status:
            conditions.append(Reconciliation.status == status)
        count_stmt = (
            select(func.count()).select_from(Reconciliation).where(*conditions)
        )
        total = int((await self._session.execute(count_stmt)).scalar_one() or 0)
        stmt = (
            select(Reconciliation, Metric)
            .outerjoin(Metric, Metric.id == Reconciliation.metric_id)
            .where(*conditions)
            .order_by(Reconciliation.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(rec, metric) for rec, metric in rows], total

    async def get_reconciliation(self, rec_id: int) -> Reconciliation | None:
        stmt = select(Reconciliation).where(Reconciliation.id == rec_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def rename_dimension_references(self, old_code: str, new_code: str) -> None:
        """级联重命名维度编码在引用表中的全部引用（事务内）。

        维度编码被 4 张表引用（字符串外键，非 DB FK 约束）：
        - ``dimension_member.dim_code``：维度成员归属
        - ``dimension_mapping.source_dim_code`` / ``target_dim_code``：映射两端
        - ``metric_dimension.dim_code``：指标-维度绑定
        - ``reconciliation.dim_code``：口径对账记录（改码后若不级联，历史对账
          仍指向旧码 → 治理追溯悬空）

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
        await self._session.execute(
            update(Reconciliation)
            .where(Reconciliation.dim_code == old_code)
            .values(dim_code=new_code)
        )

    async def commit(self) -> None:
        await self._session.commit()
