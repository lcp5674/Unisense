"""冲突仓储（TD §12.4 / FR-09）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conflict import Conflict, ConflictStatus, ConflictType, RulingRecord


class ConflictRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(self, conflict: Conflict) -> Conflict:
        self._db.add(conflict)
        await self._db.flush()
        await self._db.refresh(conflict)
        return conflict

    async def get_by_conflict_id(self, conflict_id: str) -> Conflict | None:
        stmt = select(Conflict).where(
            Conflict.conflict_id == conflict_id, Conflict.deleted_at.is_(None)
        )
        res = await self._db.execute(stmt)
        return res.scalar_one_or_none()

    async def list_conflicts(
        self,
        status: ConflictStatus | None,
        ctype: ConflictType | None,
        domain: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[Conflict], int]:
        conditions: list[Any] = [Conflict.deleted_at.is_(None)]
        if status is not None:
            conditions.append(Conflict.status == status)
        if ctype is not None:
            conditions.append(Conflict.type == ctype)
        if domain is not None:
            conditions.append(Conflict.domain == domain)
        count_stmt = select(func.count()).select_from(Conflict).where(*conditions)
        total = int((await self._db.execute(count_stmt)).scalar() or 0)
        stmt = (
            select(Conflict)
            .where(*conditions)
            .order_by(Conflict.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return list(rows), total

    async def update_status(
        self,
        conflict: Conflict,
        status: ConflictStatus,
        arbitrator_id: int | None = None,
        decision_json: dict[str, Any] | None = None,
        resolved: bool = False,
    ) -> Conflict:
        conflict.status = status
        if arbitrator_id is not None:
            conflict.arbitrator_id = arbitrator_id
        if decision_json is not None:
            conflict.decision_json = decision_json
        if resolved:
            conflict.resolved_at = datetime.utcnow()
        await self._db.flush()
        return conflict

    async def create_ruling(self, ruling: RulingRecord) -> RulingRecord:
        self._db.add(ruling)
        await self._db.flush()
        await self._db.refresh(ruling)
        return ruling

    async def get_rulings(self, conflict_id: str) -> list[RulingRecord]:
        stmt = (
            select(RulingRecord)
            .where(RulingRecord.conflict_id == conflict_id, RulingRecord.deleted_at.is_(None))
            .order_by(RulingRecord.decided_at.desc())
        )
        return list((await self._db.execute(stmt)).scalars().all())
