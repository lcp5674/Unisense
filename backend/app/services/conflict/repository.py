"""冲突仓储（TD §12.4 / FR-09）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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
        severity: str | None = None,
    ) -> tuple[list[Conflict], int]:
        conditions: list[Any] = [Conflict.deleted_at.is_(None)]
        if status is not None:
            conditions.append(Conflict.status == status)
        if ctype is not None:
            conditions.append(Conflict.type == ctype)
        if domain is not None:
            conditions.append(Conflict.domain == domain)
        if severity is not None:
            conditions.append(Conflict.severity == severity)
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

    async def get_first_open_for_metric(self, metric_code: str) -> Conflict | None:
        """取某指标（作为候选或现有）最新的一条未决冲突（OPEN/NEGOTIATING/ESCALATED）。

        创建路径（semantic.create_metric）据此以冲突表**实际记录**为准挂
        ``pending_conflict`` 标记——保证「指标目录标记 ⇔ 仲裁台可处置记录」
        严格一致，杜绝「有标记无记录」的孤儿态（曾致目录显示冲突、仲裁台为空）。
        返回 None 表示该指标当前无未决冲突。
        """
        cand = func.json_unquote(func.json_extract(Conflict.metric_codes, "$.candidate"))
        ext = func.json_unquote(func.json_extract(Conflict.metric_codes, "$.existing"))
        stmt = (
            select(Conflict)
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
            .order_by(Conflict.created_at.desc())
            .limit(1)
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        return row

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

    @staticmethod
    def _consistency_filters(
        alias: Any, domain: str | None, type: str | None, status: str | None  # noqa: A002 - 筛选参数名对齐前端 query 参数 type
    ) -> list[Any]:
        """按业务域/指标类型/指标状态构建指标过滤条件（空列表 = 不过滤）。

        用于一致性统计中按指标属性收敛范围（如「仅统计 sales 域的冲突」）。
        """
        conds: list[Any] = []
        if domain:
            conds.append(alias.domain == domain)
        if type:
            conds.append(alias.type == type)
        if status:
            conds.append(alias.status == status)
        return conds

    async def consistency_stats(
        self,
        domain: str | None = None,
        type: str | None = None,  # noqa: A002 - 筛选参数名对齐前端 query 参数 type
        status: str | None = None,
    ) -> dict[str, Any]:
        """口径一致率统计（P1）：总口径数 / 一致率 / 部门间冲突数 / 平均争议解决时长。

        Args:
            domain/type/status: 可选过滤条件（业务域 code / 指标类型 / 指标状态）。
                总口径数按指标属性过滤；冲突相关计数仅统计「至少一方属于筛选范围」
                的冲突记录（无过滤时保持全平台统计，向后兼容）。

        聚合风格对齐 observability.repository（多 count + 派生比率）。一致率 =
        未卷入冲突的口径数占比；部门间冲突 = 双方指标分属不同域；平均解决时长 =
        已解决冲突 (resolved_at - created_at) 的小时均值。
        """
        ma, mb = aliased(Metric), aliased(Metric)
        fa = self._consistency_filters(ma, domain, type, status)
        fb = self._consistency_filters(mb, domain, type, status)
        scoped = bool(fa or fb)
        # 冲突「至少一方在筛选范围」的 OR 条件（无过滤时不启用，保持原全平台语义）
        in_scope = or_(*fa, *fb) if scoped else None
        # 指标属性过滤（总口径数 / 卷入冲突指标数）
        metric_scope = self._consistency_filters(Metric, domain, type, status)

        total_stmt = select(func.count()).select_from(Metric).where(Metric.deleted_at.is_(None))
        total_stmt = total_stmt.where(*metric_scope)
        total_defs = (
            await self._db.execute(total_stmt)
        ).scalar() or 0

        if scoped:
            conflict_stmt = (
                select(func.count())
                .select_from(Conflict)
                .join(ma, ma.id == Conflict.metric_a)
                .join(mb, mb.id == Conflict.metric_b)
                .where(
                    Conflict.deleted_at.is_(None),
                    ma.deleted_at.is_(None),
                    mb.deleted_at.is_(None),
                    in_scope,
                )
            )
        else:
            conflict_stmt = (
                select(func.count()).select_from(Conflict).where(Conflict.deleted_at.is_(None))
            )
        total_conflicts = (
            await self._db.execute(conflict_stmt)
        ).scalar() or 0

        cand = func.json_unquote(func.json_extract(Conflict.metric_codes, "$.candidate"))
        ext = func.json_unquote(func.json_extract(Conflict.metric_codes, "$.existing"))
        codes = select(cand.label("code")).where(
            Conflict.deleted_at.is_(None), cand.is_not(None)
        ).union(
            select(ext.label("code")).where(Conflict.deleted_at.is_(None), ext.is_not(None))
        ).subquery()
        conflicted_stmt = select(func.count()).select_from(codes)
        if scoped:
            conflicted_stmt = conflicted_stmt.join(
                Metric, Metric.metric_code == codes.c.code
            ).where(Metric.deleted_at.is_(None), *metric_scope)
        conflicted = (
            await self._db.execute(conflicted_stmt)
        ).scalar() or 0

        cross_stmt = (
            select(func.count())
            .select_from(Conflict)
            .join(ma, ma.id == Conflict.metric_a)
            .join(mb, mb.id == Conflict.metric_b)
            .where(
                Conflict.deleted_at.is_(None),
                ma.deleted_at.is_(None),
                mb.deleted_at.is_(None),
                ma.domain.is_not(None),
                mb.domain.is_not(None),
                ma.domain != mb.domain,
            )
        )
        if scoped:
            cross_stmt = cross_stmt.where(in_scope)
        cross_dept = (
            await self._db.execute(cross_stmt)
        ).scalar() or 0

        if scoped:
            avg_stmt = (
                select(
                    func.avg(
                        func.timestampdiff(
                            text("SECOND"), Conflict.created_at, Conflict.resolved_at
                        )
                    )
                )
                .select_from(Conflict)
                .join(ma, ma.id == Conflict.metric_a)
                .join(mb, mb.id == Conflict.metric_b)
                .where(
                    Conflict.deleted_at.is_(None),
                    Conflict.resolved_at.is_not(None),
                    ma.deleted_at.is_(None),
                    mb.deleted_at.is_(None),
                    in_scope,
                )
            )
        else:
            avg_stmt = select(
                func.avg(
                    func.timestampdiff(
                        text("SECOND"), Conflict.created_at, Conflict.resolved_at
                    )
                )
            ).where(Conflict.deleted_at.is_(None), Conflict.resolved_at.is_not(None))
        avg_sec = (
            await self._db.execute(avg_stmt)
        ).scalar()
        avg_hours = round(float(avg_sec) / 3600, 1) if avg_sec is not None else 0.0
        rate = round((1 - conflicted / total_defs) * 100, 1) if total_defs else 100.0
        return {
            "total_definitions": total_defs,
            "total_conflicts": total_conflicts,
            "conflicted_metrics": conflicted,
            "consistency_rate_pct": rate,
            "cross_department_conflicts": cross_dept,
            "avg_resolve_hours": avg_hours,
        }
