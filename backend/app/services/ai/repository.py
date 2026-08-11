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
        metrics = (await self._session.execute(select(Metric.metric_code))).scalars().all()
        names = (await self._session.execute(select(Metric.name))).scalars().all()
        all_metrics = list(metrics) + list(names)
        return {str(t) for t in terms} | {str(m) for m in all_metrics}
