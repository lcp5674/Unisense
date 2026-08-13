"""系统字典仓储层。"""

from __future__ import annotations

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_dict import SystemDict

logger = structlog.get_logger("unisense.system_dict.repository")


class SystemDictRepository:
    """系统字典仓储。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_by_type(self, dict_type: str, status: str | None = "active") -> list[SystemDict]:
        stmt = select(SystemDict).where(
            SystemDict.dict_type == dict_type,
            SystemDict.deleted_at.is_(None),
        )
        if status:
            stmt = stmt.where(SystemDict.status == status)
        stmt = stmt.order_by(SystemDict.sort_order, SystemDict.code)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_item(self, dict_type: str, code: str) -> SystemDict | None:
        stmt = select(SystemDict).where(
            SystemDict.dict_type == dict_type,
            SystemDict.code == code,
            SystemDict.deleted_at.is_(None),
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def item_exists(self, dict_type: str, code: str) -> bool:
        stmt = select(func.count()).select_from(SystemDict).where(
            SystemDict.dict_type == dict_type,
            SystemDict.code == code,
            SystemDict.deleted_at.is_(None),
        )
        result = await self._db.execute(stmt)
        return (result.scalar() or 0) > 0

    async def dict_type_exists(self, dict_type: str) -> bool:
        """检查字典类型是否存在（有任意 active 项）。"""
        stmt = select(func.count()).select_from(SystemDict).where(
            SystemDict.dict_type == dict_type,
            SystemDict.deleted_at.is_(None),
            SystemDict.status == "active",
        )
        result = await self._db.execute(stmt)
        return (result.scalar() or 0) > 0

    async def code_exists_in_type(self, dict_type: str, code: str) -> bool:
        """检查某类型下某编码是否存在。"""
        stmt = select(func.count()).select_from(SystemDict).where(
            SystemDict.dict_type == dict_type,
            SystemDict.code == code,
            SystemDict.deleted_at.is_(None),
        )
        result = await self._db.execute(stmt)
        return (result.scalar() or 0) > 0

    async def create(self, item: SystemDict) -> SystemDict:
        self._db.add(item)
        await self._db.flush()
        return item

    async def update(self, item: SystemDict) -> SystemDict:
        await self._db.flush()
        return item

    async def soft_delete(self, item: SystemDict) -> None:
        from datetime import UTC, datetime
        item.deleted_at = datetime.now(UTC)
        await self._db.flush()

    async def list_dict_types(self) -> list[str]:
        """列出所有有数据的字典类型。"""
        stmt = select(SystemDict.dict_type).where(
            SystemDict.deleted_at.is_(None),
        ).distinct().order_by(SystemDict.dict_type)
        result = await self._db.execute(stmt)
        return [row[0] for row in result.all()]
