"""主题域仓储层。"""

from __future__ import annotations

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dimension import Dimension
from app.models.metric import Metric
from app.models.subject_domain import SubjectDomain

logger = structlog.get_logger("unisense.subject_domain.repository")


class SubjectDomainRepository:
    """主题域仓储。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_by_code(self, code: str) -> SubjectDomain | None:
        stmt = select(SubjectDomain).where(
            SubjectDomain.code == code,
            SubjectDomain.deleted_at.is_(None),
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, domain_id: int) -> SubjectDomain | None:
        stmt = select(SubjectDomain).where(
            SubjectDomain.id == domain_id,
            SubjectDomain.deleted_at.is_(None),
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self, status: str | None = None) -> list[SubjectDomain]:
        stmt = select(SubjectDomain).where(SubjectDomain.deleted_at.is_(None))
        if status:
            stmt = stmt.where(SubjectDomain.status == status)
        stmt = stmt.order_by(SubjectDomain.sort_order, SubjectDomain.code)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def list_children(self, parent_id: int | None) -> list[SubjectDomain]:
        stmt = (
            select(SubjectDomain)
            .where(
                SubjectDomain.deleted_at.is_(None),
                SubjectDomain.parent_id == parent_id
                if parent_id is not None
                else SubjectDomain.parent_id.is_(None),
            )
            .order_by(SubjectDomain.sort_order, SubjectDomain.code)
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_metric_count(self, domain_code: str) -> int:
        stmt = (
            select(func.count())
            .select_from(Metric)
            .where(
                Metric.domain == domain_code,
                Metric.deleted_at.is_(None),
            )
        )
        result = await self._db.execute(stmt)
        return result.scalar() or 0

    async def get_dimension_count(self, domain_code: str) -> int:
        """按业务域统计维度数（排除已废弃维度，对齐指标数排除软删的口径）。"""
        stmt = (
            select(func.count())
            .select_from(Dimension)
            .where(
                Dimension.domain == domain_code,
                Dimension.status != "DEPRECATED",
            )
        )
        result = await self._db.execute(stmt)
        return result.scalar() or 0

    async def create(self, domain: SubjectDomain) -> SubjectDomain:
        self._db.add(domain)
        await self._db.flush()
        return domain

    async def update(self, domain: SubjectDomain) -> SubjectDomain:
        await self._db.flush()
        return domain

    async def soft_delete(self, domain: SubjectDomain) -> None:
        from datetime import UTC, datetime

        domain.deleted_at = datetime.now(UTC)
        await self._db.flush()

    async def code_exists(self, code: str) -> bool:
        # 注意：不按 deleted_at 过滤——MySQL 唯一索引 uq_subject_domain_code 对软删记录仍生效，
        # 若只查存活记录，软删 code 会被误判为空闲，插入时触发 Duplicate entry（端到端断层 P3-1）。
        stmt = (
            select(func.count())
            .select_from(SubjectDomain)
            .where(
                SubjectDomain.code == code,
            )
        )
        result = await self._db.execute(stmt)
        return (result.scalar() or 0) > 0

    async def name_exists(
        self,
        name: str,
        parent_id: int | None,
        exclude_id: int | None = None,
    ) -> bool:
        """检测同一父域下是否存在同名（未删除）主题域。

        作用域限定为同父域：不同父域下允许同名（如 销售/订单 与 财务/订单）。
        MySQL utf8mb4_0900_ai_ci collation 保证比较大小写不敏感；
        用 ``func.trim`` 忽略首尾空格（与前端实时检测口径一致）。
        软删记录不计入（名称无唯一约束，软删后同名可安全重建）。
        """
        stmt = (
            select(func.count())
            .select_from(SubjectDomain)
            .where(
                func.trim(SubjectDomain.name) == name.strip(),
                SubjectDomain.deleted_at.is_(None),
                SubjectDomain.parent_id == parent_id
                if parent_id is not None
                else SubjectDomain.parent_id.is_(None),
            )
        )
        if exclude_id is not None:
            stmt = stmt.where(SubjectDomain.id != exclude_id)
        result = await self._db.execute(stmt)
        return (result.scalar() or 0) > 0

    async def count_children(self, parent_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(SubjectDomain)
            .where(
                SubjectDomain.parent_id == parent_id,
                SubjectDomain.deleted_at.is_(None),
            )
        )
        result = await self._db.execute(stmt)
        return result.scalar() or 0
