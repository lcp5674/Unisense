"""指标挂载实体 Repository（OneData 挂载层，TD §4.2 dataset_metric）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric import Metric
from app.models.metric_mount import MetricMount


class MetricMountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, obj: MetricMount) -> MetricMount:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def get(self, mount_id: int) -> MetricMount | None:
        stmt = select(MetricMount).where(
            MetricMount.id == mount_id, MetricMount.deleted_at.is_(None)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_metric(self, metric_id: int) -> MetricMount | None:
        stmt = select(MetricMount).where(
            MetricMount.metric_id == metric_id, MetricMount.deleted_at.is_(None)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list(
        self,
        metric_id: int | None,
        domain: str | None,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[tuple[MetricMount, Metric | None]], int]:
        """分页列出挂载并 LEFT JOIN Metric 取指标信息，返回 (列表, total)。

        total 用独立 count（不含 JOIN），与列表共用同一过滤条件，保证分页一致性。
        """
        conditions = [MetricMount.deleted_at.is_(None)]
        if metric_id is not None:
            conditions.append(MetricMount.metric_id == metric_id)
        if domain:
            conditions.append(MetricMount.domain == domain)
        count_stmt = (
            select(func.count()).select_from(MetricMount).where(*conditions)
        )
        total = (await self._session.execute(count_stmt)).scalar() or 0
        stmt = (
            select(MetricMount, Metric)
            .outerjoin(Metric, Metric.id == MetricMount.metric_id)
            .where(*conditions)
            .order_by(MetricMount.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(mount, metric) for mount, metric in rows], int(total)

    async def soft_delete(self, mount_id: int) -> None:
        """软删除挂载（deleted_at 置位，列表过滤已排除）。"""
        await self._session.execute(
            update(MetricMount)
            .where(MetricMount.id == mount_id)
            .values(deleted_at=datetime.now(UTC))
        )

    async def commit(self) -> None:
        await self._session.commit()
