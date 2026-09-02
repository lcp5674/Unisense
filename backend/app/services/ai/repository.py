"""AI 问数 Repository（TD §12.7 / FR-14）。

为语义锚定提供可信词汇表：已发布术语名 + 指标编码/名称。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric import Metric
from app.models.term import Term


class AiRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def vocabulary(self) -> set[str]:
        terms = (
            (await self._session.execute(select(Term.name).where(Term.status == "PUBLISHED")))
            .scalars()
            .all()
        )
        # 用户级可见性：词汇表仅取未删除的公开状态指标——此前全量取码/名会把
        # 他人 DRAFT/REVIEW 私有指标与已删指标拼进 LLM prompt（弱确认性泄露）。
        rows = (
            await self._session.execute(
                select(Metric.metric_code, Metric.name).where(
                    Metric.deleted_at.is_(None),
                    Metric.status.in_(("PUBLISHED", "EXPERIMENTAL", "DEPRECATED")),
                )
            )
        ).all()
        return {str(t) for t in terms} | {
            str(v) for row in rows for v in (row.metric_code, row.name) if v
        }
