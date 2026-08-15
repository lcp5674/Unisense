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
        self,
        dict_type: str,
        status: str | None = "active",
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
        """新增字典项。

        编码唯一性以「含软删除」全量判定：软删除行仍占用唯一索引
        （uk_dict_type_code），若命中软删除行则恢复并更新字段，避免
        IntegrityError → 500；命中 active 行则抛 ConflictError。
        ``data.code`` 未传时按显示名自动生成英文编码（冲突自动追加序号）。
        """
        code = data.code
        if not code:
            code = await self._generate_unique_code(dict_type, data.label)
        existing = await self._repo.get_item_including_deleted(dict_type, code)
        if existing is not None and existing.deleted_at is None:
            raise ConflictError(
                f"字典项已存在: {dict_type}/{code}",
                error_code="DUPLICATE_DICT_CODE",
            )
        if existing is not None:
            # 软删除行 → 恢复重建（去删除标记 + 回 active + 覆盖字段）
            existing.deleted_at = None
            existing.status = "active"
            existing.label = data.label
            existing.sort_order = data.sort_order
            existing.description = data.description
            item = await self._repo.update(existing)
            logger.info(
                "dict_item_restored",
                dict_type=dict_type,
                code=code,
                auto_generated=not data.code,
            )
            return item

        item = SystemDict(
            dict_type=dict_type,
            code=code,
            label=data.label,
            sort_order=data.sort_order,
            status="active",
            description=data.description,
        )
        item = await self._repo.create(item)
        logger.info(
            "dict_item_created",
            dict_type=dict_type,
            code=code,
            auto_generated=not data.code,
        )
        return item

    async def _generate_unique_code(self, dict_type: str, label: str) -> str:
        """按显示名自动生成唯一字典项编码。

        规则：中文经术语字典翻译（贪心最长匹配）、未覆盖字拼音兜底、
        ASCII 保留生成 slug（对齐 ``app.core.codegen.slugify_code``）；
        纯标点/空白等无可提取字符时回退 ``item``；冲突时追加 ``_2/_3/...``
        后缀（上限 100 次）。与主题域/数据源编码自动生成约定保持一致。
        """
        from app.core.codegen import (
            MAX_CODE_ATTEMPTS,
            MAX_CODE_LEN,
            generate_unique_code,
            slugify_code,
        )

        base = slugify_code(label) or "item"
        try:
            return await generate_unique_code(
                base,
                lambda cand: self._repo.code_exists_in_type(dict_type, cand),
                max_len=MAX_CODE_LEN,
                max_attempts=MAX_CODE_ATTEMPTS,
            )
        except RuntimeError as exc:
            raise BusinessError(
                f"无法为 {label} 生成唯一字典项编码，请手动指定",
                error_code="DICT_CODE_EXHAUSTED",
            ) from exc

    async def update_item(
        self,
        dict_type: str,
        code: str,
        data: DictItemUpdate,
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

        stmt = (
            select(func.count())
            .select_from(Metric)
            .where(
                column == code,
                Metric.deleted_at.is_(None),
            )
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
