"""冲突仓储（TD §12.4 / FR-09）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conflict import Conflict, ConflictStatus, ConflictType, RulingRecord
from app.models.metric import Metric


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
            conflict.resolved_at = datetime.now(UTC)
        await self._db.flush()
        return conflict

    async def reopen(self, conflict: Conflict) -> Conflict:
        """将已关闭冲突重新打开为待处理：状态置 OPEN、清除 resolved_at。

        与 close 对称（CLOSED → OPEN），供重新裁决使用；历史裁决记录保留在
        ``ruling_record`` 表作为知识库，不受影响。
        """
        conflict.status = ConflictStatus.OPEN
        conflict.resolved_at = None
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

    async def count_open_for_metric(self, metric_code: str) -> int:
        """统计某指标（作为候选或现有）当前仍处未决状态的冲突数。

        跨服务一致性（TD §12.4）：仲裁/关闭后据此判断是否可清除指标表的
        ``pending_conflict`` 冗余标记——仅当该指标不再有任何 OPEN/NEGOTIATING/
        ESCALATED 冲突时才清除，避免误清仍有关联冲突的指标。
        """
        cand = func.json_unquote(func.json_extract(Conflict.metric_codes, "$.candidate"))
        ext = func.json_unquote(func.json_extract(Conflict.metric_codes, "$.existing"))
        stmt = (
            select(func.count())
            .select_from(Conflict)
            .where(
                Conflict.deleted_at.is_(None),
                Conflict.status.in_(
                    [
                        ConflictStatus.OPEN,
                        ConflictStatus.NEGOTIATING,
                        ConflictStatus.ESCALATED,
                    ]
                ),
                or_(cand == metric_code, ext == metric_code),
            )
        )
        total = int((await self._db.execute(stmt)).scalar() or 0)
        return total

    async def count_open_for_pair(self, candidate_code: str, existing_code: str) -> int:
        """统计同一（候选, 现有）有序对当前仍处未决状态的冲突数。

        冲突表完整性（TD §12.4）：同一对指标重复调用 check 不应堆积多条 OPEN
        冲突——检测结果仍上报调用方，但仅在无未决冲突时才落库新记录。
        """
        cand = func.json_unquote(func.json_extract(Conflict.metric_codes, "$.candidate"))
        ext = func.json_unquote(func.json_extract(Conflict.metric_codes, "$.existing"))
        stmt = (
            select(func.count())
            .select_from(Conflict)
            .where(
                Conflict.deleted_at.is_(None),
                Conflict.status.in_(
                    [
                        ConflictStatus.OPEN,
                        ConflictStatus.NEGOTIATING,
                        ConflictStatus.ESCALATED,
                    ]
                ),
                cand == candidate_code,
                ext == existing_code,
            )
        )
        total = int((await self._db.execute(stmt)).scalar() or 0)
        return total

    async def resolve_active_metric_id(self, metric_code: str) -> int | None:
        """解析活动（未软删）指标行 ID；不存在返回 None。

        自我冲突防御（TD §12.4）：``metric_code`` 在指标表全局唯一——候选与
        现有同码时若都解析到同一条活动行，即是「指标与自身比对」的自我引用，
        不构成合法冲突（同名不同义的合法形态是「新提交 vs 已存在行」：候选码
        尚未落库、解析为 None）。仅返回 id 避免整行读写竞态。
        """
        if not metric_code:
            return None
        row = (
            await self._db.execute(
                select(Metric.id).where(
                    Metric.metric_code == metric_code, Metric.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        return row if row is not None else None
