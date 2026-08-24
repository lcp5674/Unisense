"""逻辑度量目录服务（OneData 原子层，TD §4.2 / FR-02-08）。

核心能力：
1. 逻辑度量 CRUD + 状态机（DRAFT → PUBLISHED → DEPRECATED）。
2. 度量格式联动默认（金额:元/2 位，比率:小数/4 位，数值:自定义）——PRD FR-02-08。
3. 废弃保护：被指标引用的度量禁止废弃（防原子指标悬空）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.codegen import generate_unique_code, slugify_code
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    UnisenseError,
    ValidationError,
)
from app.models.measure_catalog import MeasureCatalog, MeasureCategory, MeasureFormat, MeasureStatus
from app.services.measure_catalog.repository import MeasureCatalogRepository
from app.services.measure_catalog.schemas import (
    MeasureCreate,
    MeasureUpdate,
    _FORMAT_DEFAULTS,
)

_VALID_FORMATS = {e.value for e in MeasureFormat}
_VALID_CATEGORIES = {e.value for e in MeasureCategory}
_VALID_STATUSES = {e.value for e in MeasureStatus}


class MeasureCatalogService(BaseService):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._session = session
        self._repo = MeasureCatalogRepository(session)

    async def _generate_measure_code(self, data: MeasureCreate) -> str:
        """自动生成逻辑度量编码：``{domain_slug}_{name_slug}``，冲突自增后缀。"""
        domain_slug = slugify_code(data.domain)
        name_slug = slugify_code(data.name)
        if domain_slug and name_slug:
            base = f"{domain_slug}_{name_slug}"
        elif name_slug:
            base = f"measure_{name_slug}"
        elif domain_slug:
            base = f"measure_{domain_slug}"
        else:
            base = "measure"

        async def _exists(code: str) -> bool:
            return await self._repo.get(code) is not None

        return await generate_unique_code(base, _exists)

    async def create_measure(
        self, data: MeasureCreate, actor_id: int | None = None
    ) -> MeasureCatalog:
        if not data.measure_code:
            data.measure_code = await self._generate_measure_code(data)
        if await self._repo.get(data.measure_code) is not None:
            raise ConflictError(
                f"逻辑度量编码已存在: {data.measure_code}", error_code="MEASURE_EXISTS"
            )
        if data.measure_format not in _VALID_FORMATS:
            raise ValidationError(
                f"未知度量格式: {data.measure_format}",
                error_code="INVALID_MEASURE_FORMAT",
                ctx={"format": data.measure_format},
            )
        # 度量格式联动默认（缺省已由 schema 填充，此处兜底显式赋值）
        default_unit, default_decimal = _FORMAT_DEFAULTS[data.measure_format]
        measure = MeasureCatalog(
            measure_code=data.measure_code,
            name=data.name,
            description=data.description,
            measure_format=data.measure_format,
            default_unit=data.default_unit or default_unit,
            default_decimal_places=(
                data.default_decimal_places
                if data.default_decimal_places is not None
                else default_decimal
            ),
            source_system=data.source_system,
            synonyms=data.synonyms,
            category=data.category or MeasureCategory.OTHER.value,
            stat_caliber=data.stat_caliber,
            domain=data.domain,
            # PLAT-2: 认证身份优先，client 传入的 owner_id 仅作降级
            owner_id=actor_id if actor_id is not None else data.owner_id,
            status=MeasureStatus.DRAFT.value,
        )
        return await self._repo.save(measure)

    async def get_measure(self, measure_code: str) -> MeasureCatalog:
        measure = await self._repo.get(measure_code)
        if measure is None:
            raise NotFoundError(f"逻辑度量不存在: {measure_code}")
        return measure

    async def list_measures(
        self,
        domain: str | None,
        status: str | None,
        keyword: str | None = None,
        owner_id: int | None = None,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[MeasureCatalog], int]:
        """分页列出逻辑度量，返回 (列表, total)（服务端分页，对齐 dimension）。"""
        limit = min(max(page_size, 1), 200)
        offset = (max(page, 1) - 1) * limit
        return await self._repo.list(domain, status, keyword, owner_id, limit=limit, offset=offset)

    async def update_measure(self, measure_code: str, data: MeasureUpdate) -> MeasureCatalog:
        measure = await self._require(measure_code)
        if measure.status == MeasureStatus.DEPRECATED.value:
            raise UnisenseError(f"已废弃逻辑度量不可更新: {measure_code}", error_code="INVALID_STATE")
        # 改编码：仅 DRAFT 状态允许（已发布度量被指标引用，改码会破坏引用）。
        if data.measure_code is not None and data.measure_code != measure_code:
            if measure.status != MeasureStatus.DRAFT.value:
                raise UnisenseError(
                    f"仅 DRAFT 状态可修改编码，当前 {measure.status}；已发布度量请先废弃重建",
                    error_code="INVALID_STATE",
                )
            if await self._repo.get(data.measure_code) is not None:
                raise ConflictError(
                    f"逻辑度量编码已存在: {data.measure_code}", error_code="MEASURE_EXISTS"
                )
            measure.measure_code = data.measure_code
        if data.name is not None:
            measure.name = data.name
        if data.description is not None:
            measure.description = data.description
        if data.domain is not None:
            measure.domain = data.domain
        if data.measure_format is not None:
            if data.measure_format not in _VALID_FORMATS:
                raise ValidationError(
                    f"未知度量格式: {data.measure_format}",
                    error_code="INVALID_MEASURE_FORMAT",
                    ctx={"format": data.measure_format},
                )
            # 改格式未显式给单位/小数位时，联动新格式默认（避免格式与新默认冲突）
            if data.default_unit is None and data.default_decimal_places is None:
                default_unit, default_decimal = _FORMAT_DEFAULTS[data.measure_format]
                measure.default_unit = default_unit
                measure.default_decimal_places = default_decimal
            measure.measure_format = data.measure_format
        if data.default_unit is not None:
            measure.default_unit = data.default_unit
        if data.default_decimal_places is not None:
            measure.default_decimal_places = data.default_decimal_places
        if data.source_system is not None:
            measure.source_system = data.source_system
        if data.synonyms is not None:
            measure.synonyms = data.synonyms
        if data.category is not None:
            if data.category not in _VALID_CATEGORIES:
                raise ValidationError(
                    f"未知度量分类: {data.category}",
                    error_code="INVALID_MEASURE_CATEGORY",
                    ctx={"category": data.category},
                )
            measure.category = data.category
        if data.stat_caliber is not None:
            measure.stat_caliber = data.stat_caliber
        await self._repo.commit()
        return measure

    async def publish_measure(self, measure_code: str) -> MeasureCatalog:
        measure = await self._require(measure_code)
        if measure.status != MeasureStatus.DRAFT.value:
            raise UnisenseError(
                f"仅 DRAFT 状态可发布，当前 {measure.status}", error_code="INVALID_STATE"
            )
        measure.status = MeasureStatus.PUBLISHED.value
        await self._repo.commit()
        return measure

    async def deprecate_measure(self, measure_code: str) -> MeasureCatalog:
        measure = await self._require(measure_code)
        # 废弃保护（跨服务一致性）：被指标引用的逻辑度量禁止废弃——否则原子指标
        # measure_id 悬空，继承的度量格式/单位/小数位失去权威来源。
        bound = await self._repo.count_metrics_by_measure(measure.id)
        if bound > 0:
            raise ConflictError(
                f"逻辑度量 {measure_code} 正被 {bound} 个指标引用，无法废弃；请先改绑相关指标",
                error_code="MEASURE_BOUND_BY_METRICS",
            )
        measure.status = MeasureStatus.DEPRECATED.value
        await self._repo.commit()
        return measure

    async def _require(self, measure_code: str) -> MeasureCatalog:
        measure = await self._repo.get(measure_code)
        if measure is None:
            raise NotFoundError(f"逻辑度量不存在: {measure_code}")
        return measure
