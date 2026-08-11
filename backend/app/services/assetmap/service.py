"""资产地图服务（TD §12.11 / FR-18）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.assetmap.repository import AssetMapRepository


class AssetMapService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = AssetMapRepository(session)

    async def catalog_summary(self) -> dict[str, Any]:
        return await self._repo.catalog_summary()

    async def classification_summary(self) -> dict[str, Any]:
        return await self._repo.classification_summary()

    async def metric_summary(self) -> dict[str, Any]:
        return await self._repo.metric_summary()

    async def list_tables(
        self, source_id: str | None, sensitivity: str | None, limit: int
    ) -> list[dict[str, Any]]:
        rows = await self._repo.list_tables(source_id, sensitivity, limit)
        # assetmap T-2: 经 to_dict 剔除敏感字段（connection_config 等）
        return [r.to_dict() for r in rows]

    async def orphan_assets(self) -> list[dict[str, Any]]:
        rows = await self._repo.orphan_assets()
        return [r.to_dict() for r in rows]
