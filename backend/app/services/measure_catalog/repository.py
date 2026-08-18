"""逻辑度量目录 Repository（OneData 原子层，TD §4.2 / FR-02-08）。"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.measure_catalog import MeasureCatalog
from app.models.metric import Metric


class MeasureCatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, obj: MeasureCatalog) -> MeasureCatalog:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def get(self, measure_code: str) -> MeasureCatalog | None:
        stmt = select(MeasureCatalog).where(MeasureCatalog.measure_code == measure_code)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, measure_id: int) -> MeasureCatalog | None:
        stmt = select(MeasureCatalog).where(MeasureCatalog.id == measure_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list(
        self,
        domain: str | None,
        status: str | None,
        keyword: str | None = None,
        owner_id: int | None = None,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[MeasureCatalog], int]:
        """分页列出逻辑度量，返回 (列表, total)。

        - total 用独立 count（不含 JOIN），与列表共用同一过滤条件，保证分页一致性
        - keyword 参数化 LIKE + 通配符转义（对齐 FR-035：% / _ 须转义，防模糊放大）
        """
        conditions = [MeasureCatalog.deleted_at.is_(None)]
        if domain:
            conditions.append(MeasureCatalog.domain == domain)
        if status:
            conditions.append(MeasureCatalog.status == status)
        if owner_id is not None:
            conditions.append(MeasureCatalog.owner_id == owner_id)
        if keyword:
            escaped = keyword.replace("/", "//").replace("%", "/%").replace("_", "/_")
            conditions.append(
                or_(
                    MeasureCatalog.measure_code.like(f"%{escaped}%", escape="/"),
                    MeasureCatalog.name.like(f"%{escaped}%", escape="/"),
                    MeasureCatalog.description.like(f"%{escaped}%", escape="/"),
                )
            )
        count_stmt = (
            select(func.count()).select_from(MeasureCatalog).where(*conditions)
        )
        total = (await self._session.execute(count_stmt)).scalar() or 0
        stmt = (
            select(MeasureCatalog)
            .where(*conditions)
            .order_by(MeasureCatalog.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all()), int(total)

    async def count_metrics_by_measure(self, measure_id: int) -> int:
        """统计逻辑度量被多少指标引用（废弃保护：被引用度量禁止废弃）。"""
        stmt = select(func.count(Metric.id)).where(Metric.measure_id == measure_id)
        return int((await self._session.execute(stmt)).scalar_one() or 0)

    async def commit(self) -> None:
        await self._session.commit()
