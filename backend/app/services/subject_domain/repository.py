"""主题域仓储层。"""

from __future__ import annotations

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
        stmt = select(SubjectDomain).where(
            SubjectDomain.deleted_at.is_(None),
            SubjectDomain.parent_id == parent_id
            if parent_id is not None
            else SubjectDomain.parent_id.is_(None),
        ).order_by(SubjectDomain.sort_order, SubjectDomain.code)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_metric_count(self, domain_code: str) -> int:
        stmt = select(func.count()).select_from(Metric).where(
            Metric.domain == domain_code,
            Metric.deleted_at.is_(None),
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
        stmt = select(func.count()).select_from(SubjectDomain).where(
            SubjectDomain.code == code,
            SubjectDomain.deleted_at.is_(None),
        )
        result = await self._db.execute(stmt)
        return (result.scalar() or 0) > 0

    async def count_children(self, parent_id: int) -> int:
        stmt = select(func.count()).select_from(SubjectDomain).where(
            SubjectDomain.parent_id == parent_id,
            SubjectDomain.deleted_at.is_(None),
        )
        result = await self._db.execute(stmt)
        return result.scalar() or 0
