"""资产地图 Repository（TD §12.11 / FR-18）。

只读聚合：元数据目录（db_catalog）、分类（classification）、指标（metric）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_source import DBCatalog
from app.models.governance import Classification
from app.models.metric import Metric


class AssetMapRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_tables(
        self, source_id: str | None, sensitivity: str | None, limit: int
    ) -> list[DBCatalog]:
        stmt = select(DBCatalog).where(DBCatalog.entity_type == "table")
        if source_id:
            stmt = stmt.where(DBCatalog.source_id == source_id)
        if sensitivity:
            stmt = stmt.where(DBCatalog.sensitivity_level == sensitivity)
        return list((await self._session.execute(stmt.limit(limit))).scalars().all())

    async def orphan_assets(self) -> list[DBCatalog]:
        stmt = select(DBCatalog).where(DBCatalog.owner_id.is_(None))
        return list((await self._session.execute(stmt)).scalars().all())

    async def catalog_summary(self) -> dict[str, Any]:
        total = (
            await self._session.execute(select(func.count()).select_from(DBCatalog))
        ).scalar() or 0
        by_type = (
            await self._session.execute(
                select(DBCatalog.entity_type, func.count()).group_by(DBCatalog.entity_type)
            )
        ).all()
        by_sens = (
            await self._session.execute(
                select(DBCatalog.sensitivity_level, func.count()).group_by(
                    DBCatalog.sensitivity_level
                )
            )
        ).all()
        orphans = (
            await self._session.execute(
                select(func.count()).select_from(DBCatalog).where(DBCatalog.owner_id.is_(None))
            )
        ).scalar() or 0
        return {
            "total": total,
            "by_entity_type": dict(cast("Sequence[tuple[Any, Any]]", by_type)),
            "by_sensitivity": dict(cast("Sequence[tuple[Any, Any]]", by_sens)),
            "orphan_assets": orphans,
        }

    async def classification_summary(self) -> dict[str, Any]:
        rows = (
            await self._session.execute(
                select(Classification.sensitivity_level, func.count()).group_by(
                    Classification.sensitivity_level
                )
            )
        ).all()
        return {"by_sensitivity": dict(cast("Sequence[tuple[Any, Any]]", rows))}

    async def metric_summary(self) -> dict[str, Any]:
        by_domain = (
            await self._session.execute(select(Metric.domain, func.count()).group_by(Metric.domain))
        ).all()
        by_status = (
            await self._session.execute(select(Metric.status, func.count()).group_by(Metric.status))
        ).all()
        return {
            "by_domain": dict(cast("Sequence[tuple[Any, Any]]", by_domain)),
            "by_status": dict(cast("Sequence[tuple[Any, Any]]", by_status)),
        }
