"""系统字典业务逻辑层。"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessError, ConflictError, NotFoundError
from app.models.system_dict import SystemDict
from app.services.system_dict.repository import SystemDictRepository
from app.services.system_dict.schemas import DictItemCreate, DictItemUpdate

logger = structlog.get_logger("unisense.system_dict.service")


class SystemDictService:
    """系统字典服务。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repo = SystemDictRepository(db)

    async def list_by_type(
        self, dict_type: str, status: str | None = "active",
    ) -> list[SystemDict]:
        """获取某类型字典项列表（默认仅 active）。"""
        return await self._repo.list_by_type(dict_type, status)

    async def list_all_by_type(self, dict_type: str) -> list[SystemDict]:
        """获取某类型全部字典项（含 inactive），仅管理端用。"""
        return await self._repo.list_by_type(dict_type, status=None)

    async def list_dict_types(self) -> list[str]:
        """列出所有字典类型。"""
        return await self._repo.list_dict_types()

    async def get_item(self, dict_type: str, code: str) -> SystemDict:
        """获取单个字典项。"""
        item = await self._repo.get_item(dict_type, code)
        if item is None:
            raise NotFoundError(f"字典项不存在: {dict_type}/{code}")
        return item

    async def create_item(self, dict_type: str, data: DictItemCreate) -> SystemDict:
        """新增字典项。"""
        # 编码唯一性校验
        if await self._repo.code_exists_in_type(dict_type, data.code):
            raise ConflictError(
                f"字典项已存在: {dict_type}/{data.code}",
                error_code="DUPLICATE_DICT_CODE",
            )
        item = SystemDict(
            dict_type=dict_type,
            code=data.code,
            label=data.label,
            sort_order=data.sort_order,
            status="active",
            description=data.description,
        )
        item = await self._repo.create(item)
        logger.info("dict_item_created", dict_type=dict_type, code=data.code)
        return item

    async def update_item(
        self, dict_type: str, code: str, data: DictItemUpdate,
    ) -> SystemDict:
        """更新字典项（label/sort_order/description）。"""
        item = await self.get_item(dict_type, code)
        if data.label is not None:
            item.label = data.label
        if data.sort_order is not None:
            item.sort_order = data.sort_order
        if data.description is not None:
            item.description = data.description
        item = await self._repo.update(item)
        logger.info("dict_item_updated", dict_type=dict_type, code=code)
        return item

    async def deactivate_item(self, dict_type: str, code: str) -> SystemDict:
        """停用字典项。"""
        item = await self.get_item(dict_type, code)
        item.status = "inactive"
        item = await self._repo.update(item)
        logger.info("dict_item_deactivated", dict_type=dict_type, code=code)
        return item

    async def activate_item(self, dict_type: str, code: str) -> SystemDict:
        """启用字典项。"""
        item = await self.get_item(dict_type, code)
        item.status = "active"
        item = await self._repo.update(item)
        logger.info("dict_item_activated", dict_type=dict_type, code=code)
        return item

    async def delete_item(self, dict_type: str, code: str) -> None:
        """删除字典项（需无引用）。"""
        item = await self.get_item(dict_type, code)
        ref_count = await self.get_ref_count(dict_type, code)
        if ref_count > 0:
            raise BusinessError(
                f"该字典项被 {ref_count} 个指标引用，不可删除，请先停用",
                error_code="HAS_REFERENCES",
            )
        await self._repo.soft_delete(item)
        logger.info("dict_item_deleted", dict_type=dict_type, code=code)

    async def get_ref_count(self, dict_type: str, code: str) -> int:
        """获取字典项引用计数（被多少指标引用）。

        按 dict_type 映射到 Metric 对应字段，统计引用数。
        """
        from sqlalchemy import func, select

        from app.models.metric import Metric

        # dict_type → Metric 字段映射
        field_map: dict[str, str] = {
            "granularity": "granularity",
            "unit": "unit",
            "aggregation": "aggregation",
            "time_semantics": "time_semantics",
            "freshness": "freshness",
            "dw_layer": "dw_layer",
            "metric_type": "type",
            "additivity": "additivity",
            "serving_mode": "serving_mode",
            "metric_tier": "metric_tier",
        }

        metric_field = field_map.get(dict_type)
        if metric_field is None:
            return 0

        column = getattr(Metric, metric_field, None)
        if column is None:
            return 0

        stmt = select(func.count()).select_from(Metric).where(
            column == code,
            Metric.deleted_at.is_(None),
        )
        result = await self._db.execute(stmt)
        return result.scalar() or 0

    async def validate_dict_value(self, dict_type: str, code: str) -> SystemDict:
        """校验字典值存在且 active（供指标注册时调用）。"""
        item = await self._repo.get_item(dict_type, code)
        if item is None:
            raise NotFoundError(
                f"字典值不存在: {dict_type}/{code}",
                error_code="DICT_VALUE_NOT_FOUND",
            )
        if item.status != "active":
            raise BusinessError(
                f"字典值已停用: {dict_type}/{code}",
                error_code="DICT_VALUE_INACTIVE",
            )
        return item
