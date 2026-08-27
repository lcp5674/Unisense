"""逻辑度量目录服务（OneData 原子层，TD §4.2 / FR-02-08）。

核心能力：
1. 逻辑度量 CRUD + 状态机（DRAFT → PUBLISHED → DEPRECATED）。
2. 度量格式联动默认（金额:元/2 位，比率:小数/4 位，数值:自定义）——PRD FR-02-08。
3. 废弃保护：被指标引用的度量禁止废弃（防原子指标悬空）。
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from sqlalchemy import select
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
from app.models.subject_domain import SubjectDomain
from app.services.master_data_review.service import MasterDataReviewMixin
from app.services.measure_catalog.repository import MeasureCatalogRepository
from app.services.measure_catalog.schemas import (
    _FORMAT_DEFAULTS,
    MeasureAutoSuggestRequest,
    MeasureCreate,
    MeasureSuggestResponse,
    MeasureUpdate,
    SuggestField,
)

logger = structlog.get_logger("unisense.measure_catalog.service")

_VALID_FORMATS = {e.value for e in MeasureFormat}
_VALID_CATEGORIES = {e.value for e in MeasureCategory}
_VALID_STATUSES = {e.value for e in MeasureStatus}

# ---- 度量目录 AI 推断规则表（LLM 不可用时的确定性兜底）----
# 度量格式关键词：按业务语义命中 AMOUNT / RATIO / NUMERIC
_FORMAT_KEYWORDS: dict[str, list[str]] = {
    "AMOUNT": ["金额", "费用", "收入", "收费", "支出", "结算", "支付", "成本", "毛利", "余额"],
    "RATIO": ["占比", "比例", "比率", "报销比例", "药占比"],
    "NUMERIC": ["人次", "人数", "次数", "数量", "笔", "张", "件", "例", "数"],
}
# 度量分类关键词：流量 / 费用 / 药品 / 医保 / 效率 / 质量
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "FLOW": ["人次", "就诊", "挂号", "接诊", "门诊量", "患者数", "流量"],
    "FEE": ["金额", "费用", "收入", "收费", "支出", "成本", "余额"],
    "DRUG": ["药品", "处方", "抗菌", "用药", "药费"],
    "MEDICAL_INSURANCE": ["医保", "报销", "统筹", "自付", "结算"],
    "EFFICIENCY": ["次均", "人均", "平均", "单价", "效率", "周转", "候诊"],
    "QUALITY": ["占比", "比例", "达标", "合格", "质控", "率"],
}
# NUMERIC 度量默认单位：按名称细化（人次/人/次/笔/张）
_NUMERIC_UNIT_KEYWORDS: list[tuple[str, str]] = [
    ("人次", "人次"),
    ("人数", "人"),
    ("患者", "人"),
    ("次数", "次"),
    ("笔", "笔"),
    ("张", "张"),
]
# 医疗场景源头系统推断
_MEDICAL_DOMAINS = {
    "outpatient", "medication", "medical_fee", "medical_insurance",
    "diagnosis", "quality", "patient",
}


class MeasureCatalogService(BaseService, MasterDataReviewMixin):
    """逻辑度量服务：复用 ``MasterDataReviewMixin`` 审核流（DRAFT→REVIEW→PUBLISHED→DEPRECATED）。"""

    _review_entity_name = "逻辑度量"
    _review_event_prefix = "measure"
    _review_code_attr = "measure_code"
    _review_status_enum = MeasureStatus

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._session = session
        self._repo = MeasureCatalogRepository(session)

    def _review_completeness(self, measure: MeasureCatalog) -> str | None:
        """提交审核的度量必须有统计口径，否则评审人无法判断定义合理性。"""
        if not (measure.stat_caliber or "").strip():
            return "尚未填写统计口径"
        return None

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

    async def _validate_category(self, category: str) -> None:
        """校验度量分类存在且 active（字典化后，dict_type=measure_category）。

        语义：字典类型未配置（未种子/空表）→ 回退枚举种子值校验（兼容历史环境）；
        类型已配置但值不在 → 拦截（DICT_VALUE_NOT_FOUND）；值已停用 → 拦截
        （DICT_VALUE_INACTIVE）；DB 查询异常 → best-effort 放行（不阻断业务）。
        """
        from app.core.exceptions import BusinessError, NotFoundError
        from app.services.system_dict.service import SystemDictService

        if not category:
            return
        try:
            svc = SystemDictService(self._db)
            if not await svc.list_by_type("measure_category", status="active"):
                if category not in _VALID_CATEGORIES:
                    raise ValidationError(
                        f"未知度量分类: {category}",
                        error_code="INVALID_MEASURE_CATEGORY",
                        ctx={"category": category},
                    )
                return
            await svc.validate_dict_value("measure_category", category)
        except (NotFoundError, BusinessError, ValidationError):
            raise
        except Exception:
            return

    async def _validate_format(self, fmt: str) -> None:
        """校验度量格式存在且 active（字典化后，dict_type=measure_format）。

        语义与 ``_validate_category`` 一致：字典未配置 → 回退枚举种子值校验；
        已配置但值不在/停用 → 拦截；DB 异常 → best-effort 放行。
        """
        from app.core.exceptions import BusinessError, NotFoundError
        from app.services.system_dict.service import SystemDictService

        if not fmt:
            return
        try:
            svc = SystemDictService(self._db)
            if not await svc.list_by_type("measure_format", status="active"):
                if fmt not in _VALID_FORMATS:
                    raise ValidationError(
                        f"未知度量格式: {fmt}",
                        error_code="INVALID_MEASURE_FORMAT",
                        ctx={"format": fmt},
                    )
                return
            await svc.validate_dict_value("measure_format", fmt)
        except (NotFoundError, BusinessError, ValidationError):
            raise
        except Exception:
            return

    async def _resolve_format_defaults(self, fmt: str) -> tuple[str, int | None]:
        """按度量格式解析默认单位/小数位（PRD FR-02-08 联动）。

        字典化后优先取字典项 extra（``{"unit": ..., "decimal": ...}``）；字典未配置
        或取值缺失时回退枚举常量（``_FORMAT_DEFAULTS``），自定义格式回退空单位/按需。
        """
        from app.services.system_dict.service import SystemDictService

        try:
            svc = SystemDictService(self._db)
            if await svc.list_by_type("measure_format", status="active"):
                item = await svc.get_item("measure_format", fmt)
                extra = item.extra or {}
                unit = str(extra.get("unit") or "")
                decimal = extra.get("decimal")
                decimal = int(decimal) if decimal is not None else None
                return unit, decimal
        except Exception:
            pass
        return _FORMAT_DEFAULTS.get(fmt, ("", None))

    async def create_measure(
        self, data: MeasureCreate, actor_id: int | None = None
    ) -> MeasureCatalog:
        if not data.measure_code:
            data.measure_code = await self._generate_measure_code(data)
        if await self._repo.get(data.measure_code) is not None:
            raise ConflictError(
                f"逻辑度量编码已存在: {data.measure_code}", error_code="MEASURE_EXISTS"
            )
        # 度量格式字典化校验（存在且 active；字典未配置时回退枚举种子值）
        await self._validate_format(data.measure_format)
        # 度量分类字典化校验（存在且 active；字典未配置时回退枚举种子值）
        await self._validate_category(data.category or MeasureCategory.OTHER.value)
        # 度量格式联动默认（缺省已由 schema 填充已知枚举，此处按字典 extra 兜底）
        default_unit, default_decimal = await self._resolve_format_defaults(data.measure_format)
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
        reviewed_by: int | None = None,
        deleted: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[MeasureCatalog], int]:
        """分页列出逻辑度量，返回 (列表, total)（服务端分页，对齐 dimension）。

        reviewed_by 非空时过滤"我审过的"（通过/驳回人 ID 匹配，供统一主数据审批工作台）。
        """
        limit = min(max(page_size, 1), 200)
        offset = (max(page, 1) - 1) * limit
        return await self._repo.list(
            domain,
            status,
            keyword,
            owner_id,
            reviewed_by=reviewed_by,
            deleted=deleted,
            limit=limit,
            offset=offset,
        )

    async def update_measure(self, measure_code: str, data: MeasureUpdate) -> MeasureCatalog:
        measure = await self._require(measure_code)
        if measure.status == MeasureStatus.DEPRECATED.value:
            raise UnisenseError(
                f"已废弃逻辑度量不可更新: {measure_code}", error_code="INVALID_STATE"
            )
        # 审核中锁定（REVIEW）：评审人基于当前定义审核，审核中改定义会造成评审失真；
        # 驳回回 DRAFT 后即可修改重提（对齐指标 REVIEW 编辑即撤回的语义，这里更严格）。
        if measure.status == MeasureStatus.REVIEW.value:
            raise UnisenseError(
                f"审核中的逻辑度量不可编辑（{measure_code}），请等待审核结果或驳回后修改",
                error_code="INVALID_STATE",
            )
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
            await self._validate_format(data.measure_format)
            # 改格式未显式给单位/小数位时，联动新格式默认（字典 extra 优先，回退枚举常量）
            if data.default_unit is None and data.default_decimal_places is None:
                default_unit, default_decimal = await self._resolve_format_defaults(
                    data.measure_format
                )
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
            await self._validate_category(data.category)
            measure.category = data.category
        if data.stat_caliber is not None:
            measure.stat_caliber = data.stat_caliber
        await self._repo.commit()
        return measure

    async def publish_measure(self, measure_code: str) -> MeasureCatalog:
        """直接发布（DRAFT → PUBLISHED，平台管理员直发通道）。

        业务用户发布度量须走审核流（submit_measure → approve_measure）；
        本方法保留为系统/种子/平台管理员兜底直发（API 层已收紧为 platform_admin），
        避免造数与迁移场景被迫走审核流程。
        """
        measure = await self._require(measure_code)
        if measure.status != MeasureStatus.DRAFT.value:
            raise UnisenseError(
                f"仅 DRAFT 状态可发布，当前 {measure.status}", error_code="INVALID_STATE"
            )
        measure.status = MeasureStatus.PUBLISHED.value
        await self._repo.commit()
        return measure

    async def submit_measure(
        self,
        measure_code: str,
        request: Any,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> MeasureCatalog:
        """提交度量审核（DRAFT → REVIEW，复用主数据审核流 TD §13）。"""
        measure = await self._require(measure_code)
        await self._submit_review(
            measure, request, actor_id, role, user_domain, code=measure_code
        )
        return measure

    async def approve_measure(
        self,
        measure_code: str,
        request: Any,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> MeasureCatalog:
        """审核通过度量（REVIEW → PUBLISHED，复用主数据审核流 FR-004）。"""
        measure = await self._require(measure_code)
        await self._approve_review(
            measure, request, actor_id, role, user_domain, code=measure_code
        )
        return measure

    async def reject_measure(
        self,
        measure_code: str,
        request: Any,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> MeasureCatalog:
        """审核驳回度量（REVIEW → DRAFT，复用主数据审核流 FR-005）。"""
        measure = await self._require(measure_code)
        await self._reject_review(
            measure, request, actor_id, role, user_domain, code=measure_code
        )
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

    async def reactivate_measure(self, measure_code: str) -> MeasureCatalog:
        """重新启用已废弃逻辑度量（DEPRECATED → DRAFT）。

        已废弃为终态，重新启用后回到草稿，可编辑后**重新走审核**（与
        DRAFT→REVIEW→PUBLISHED 审核流一致，避免绕过审核直接复活）。
        仅平台管理员或原 Owner 可执行（API 层写角色 + service 层 owner 校验）。
        """
        measure = await self._require(measure_code)
        if measure.status != MeasureStatus.DEPRECATED.value:
            raise UnisenseError(
                f"仅 DEPRECATED 状态可重新启用，当前 {measure.status}",
                error_code="INVALID_STATE",
            )
        measure.status = MeasureStatus.DRAFT.value
        await self._repo.commit()
        return measure

    async def delete_measure(
        self, measure_code: str, actor_id: int, role: str | None = None
    ) -> MeasureCatalog:
        """软删除逻辑度量（仅 DRAFT/DEPRECATED 未对外投入状态；REVIEW/PUBLISHED 禁止）。

        删除语义（用户决策）：草稿/废弃这种未对外投入的可交由管理员或生产者
        （原 Owner）软删；审核中/启用中的资源不可删。被指标引用的度量禁止删除
        （对齐 deprecate_measure 的 MEASURE_BOUND_BY_METRICS 保护）。
        """
        measure = await self._require(measure_code)
        if measure.status not in (
            MeasureStatus.DRAFT.value,
            MeasureStatus.DEPRECATED.value,
        ):
            raise UnisenseError(
                f"仅 DRAFT/DEPRECATED 状态的逻辑度量可删除（当前 {measure.status}）；"
                "审核中/启用中的资源不可删除",
                error_code="INVALID_STATE",
            )
        # 权限：平台/域管理员或原 Owner（生产者）
        if role not in ("platform_admin", "domain_admin") and measure.owner_id != actor_id:
            raise UnisenseError(
                "仅平台/域管理员或逻辑度量原 Owner 可删除",
                error_code="FORBIDDEN",
            )
        # 引用保护（跨服务一致性）：被指标引用的度量禁止删除（同废弃保护）
        bound = await self._repo.count_metrics_by_measure(measure.id)
        if bound > 0:
            raise ConflictError(
                f"逻辑度量 {measure_code} 正被 {bound} 个指标引用，无法删除；请先改绑相关指标",
                error_code="MEASURE_BOUND_BY_METRICS",
            )
        await self._repo.soft_delete_measure(measure.id)
        await self._repo.commit()
        return measure

    async def restore_measure(
        self, measure_code: str, actor_id: int, role: str | None = None
    ) -> MeasureCatalog:
        """恢复已软删逻辑度量（回收站恢复；仅 DRAFT/DEPRECATED 且 deleted_at 置位）。

        仅平台/域管理员或原 Owner 可恢复（对齐删除语义）。清除 deleted_at 使
        度量重新进入正常列表，重新走审核流。
        """
        measure = await self._repo.get(measure_code)
        if measure is None:
            raise NotFoundError(f"逻辑度量不存在: {measure_code}")
        if measure.deleted_at is None:
            raise UnisenseError(
                f"逻辑度量 {measure_code} 未处于已删除状态，无需恢复",
                error_code="INVALID_STATE",
            )
        if measure.status not in (
            MeasureStatus.DRAFT.value,
            MeasureStatus.DEPRECATED.value,
        ):
            raise UnisenseError(
                f"仅 DRAFT/DEPRECATED 状态的已删逻辑度量可恢复，当前 {measure.status}",
                error_code="INVALID_STATE",
            )
        if role not in ("platform_admin", "domain_admin") and measure.owner_id != actor_id:
            raise UnisenseError(
                "仅平台/域管理员或逻辑度量原 Owner 可恢复",
                error_code="FORBIDDEN",
            )
        await self._repo.restore_measure(measure.id)
        await self._repo.commit()
        return measure

    async def purge_measure(
        self, measure_code: str, actor_id: int, role: str | None = None
    ) -> None:
        """彻底删除已软删逻辑度量（回收站硬删，物理删除不可恢复；仅平台管理员）。

        回收站完整闭环：恢复（DRAFT/DEPRECATED）或彻底删除（仅平台管理员）。
        已软删记录不参与正常列表与审核流（``_require`` 已拒绝），故本方法直取
        记录、仅允许 deleted_at 置位者硬删；被指标引用的度量禁止彻底删除
        （防御性保留——历史数据可能存在被删度量仍被引用的情况）。
        """
        measure = await self._repo.get(measure_code)
        if measure is None:
            raise NotFoundError(f"逻辑度量不存在: {measure_code}")
        if measure.deleted_at is None:
            raise UnisenseError(
                f"逻辑度量 {measure_code} 未处于已删除状态，无需彻底删除",
                error_code="INVALID_STATE",
            )
        if role != "platform_admin":
            raise UnisenseError(
                "仅平台管理员可彻底删除逻辑度量",
                error_code="FORBIDDEN",
            )
        bound = await self._repo.count_metrics_by_measure(measure.id)
        if bound > 0:
            raise ConflictError(
                f"逻辑度量 {measure_code} 正被 {bound} 个指标引用，无法彻底删除；"
                "请先改绑相关指标",
                error_code="MEASURE_BOUND_BY_METRICS",
            )
        await self._repo.purge_measure(measure.id)
        await self._repo.commit()

    async def auto_suggest(self, data: MeasureAutoSuggestRequest) -> MeasureSuggestResponse:
        """度量目录 AI 推断：规则兜底 + LLM 业务增强，任一失败不阻断。

        规则产出确定性字段（编码/格式/单位/小数位/分类）；LLM 补充同义词/统计口径/
        业务域/源头系统（不可用自动降级规则）。
        """
        rule = self._suggest_by_rule(data)
        llm = await self._suggest_by_llm(data)
        # LLM 覆盖规则字段：来源标记 llm，置信度 0.7，理由统一
        for key, val in llm.items():
            if val is None or (isinstance(val, list) and not val):
                continue
            rule[key] = SuggestField(
                value=val, source="llm", confidence=0.7, reason="AI 依据业务语义推断"
            )
        # 编码/格式/单位/小数位/分类始终以规则为准（确定性、避免 LLM 幻觉破坏枚举合法性）
        return MeasureSuggestResponse(fields=rule)

    def _suggest_by_rule(self, data: MeasureAutoSuggestRequest) -> dict[str, SuggestField]:
        """规则推断：编码/格式/单位/小数位/分类/源头系统（确定性，LLM 不可用兜底）。"""
        text = f"{data.name} {data.description or ''}"
        fmt = self._match_keyword(text, _FORMAT_KEYWORDS) or "NUMERIC"
        category = self._match_keyword(text, _CATEGORY_KEYWORDS) or MeasureCategory.OTHER.value
        default_unit, decimal = _FORMAT_DEFAULTS[fmt]
        if fmt == "AMOUNT":
            default_unit = "CNY"  # 货币单位编码（对齐 unit 字典与 seed 语义）
        if fmt == "NUMERIC":
            for kw, unit in _NUMERIC_UNIT_KEYWORDS:
                if kw in data.name:
                    default_unit, decimal = unit, 0
                    break
        # 编码：{domain_slug}_{name_slug}，缺省仅 name_slug（与 create_measure 生成规则对齐）
        domain_slug = slugify_code(data.domain or "")
        name_slug = slugify_code(data.name)
        code_base = (
            f"{domain_slug}_{name_slug}" if domain_slug and name_slug else (name_slug or "measure")
        )
        source_system = (
            ["HIS"]
            if (data.domain in _MEDICAL_DOMAINS or "wedw" in (data.source_table or "").lower())
            else []
        )
        fields: dict[str, SuggestField] = {
            "measure_code": SuggestField(
                value=code_base, source="rule", confidence=0.9,
                reason="由业务域与名称自动生成，可修改",
            ),
            "name": SuggestField(
                value=data.name, source="rule", confidence=1.0, reason="沿用输入名称"
            ),
            "description": SuggestField(
                value=data.description, source="rule", confidence=0.8, reason="沿用输入描述"
            ),
            "measure_format": SuggestField(
                value=fmt, source="rule", confidence=0.85,
                reason=f"名称/描述含「{self._matched_keyword(text, _FORMAT_KEYWORDS, fmt)}」语义",
            ),
            "default_unit": SuggestField(
                value=default_unit, source="rule", confidence=0.9, reason="度量格式联动默认单位"
            ),
            "default_decimal_places": SuggestField(
                value=decimal, source="rule", confidence=0.9, reason="度量格式联动默认小数位"
            ),
            "source_system": SuggestField(
                value=source_system or [], source="rule", confidence=0.6,
                reason="按业务域/源表推断源头系统",
            ),
            "synonyms": SuggestField(
                value=[], source="rule", confidence=0.4, reason="规则无法推断同义词，交 AI 补充"
            ),
            "category": SuggestField(
                value=category, source="rule", confidence=0.8,
                reason=(
                    f"名称/描述命中「{self._matched_keyword(text, _CATEGORY_KEYWORDS, category)}」"
                    "分类"
                ),
            ),
            "stat_caliber": SuggestField(
                value=data.description, source="rule", confidence=0.5,
                reason="暂用输入描述作口径，交 AI 完善",
            ),
            "domain": SuggestField(
                value=data.domain, source="rule", confidence=0.7, reason="沿用所选业务域"
            ),
        }
        return fields

    async def _suggest_by_llm(self, data: MeasureAutoSuggestRequest) -> dict[str, Any]:
        """LLM 业务增强：同义词/统计口径/业务域/源头系统。不可用/解析失败返回 {}。"""
        try:
            from app.services.llm.config_service import LlmConfigService

            llm_client = await LlmConfigService(self._session).build_client()
            if not getattr(llm_client, "enabled", False):
                return {}
            # 业务域候选（供 LLM 从现有域中选择，避免推断出不存在的域）
            domain_stmt = select(SubjectDomain.code, SubjectDomain.name).where(
                SubjectDomain.deleted_at.is_(None), SubjectDomain.status == "active"
            )
            domains = {
                f"{code}({name})": code
                for code, name in (await self._session.execute(domain_stmt)).all()
            }
            candidates = "、".join(domains.keys()) or "门诊/药品/医疗费用/医保/诊断/质控/患者"
            prompt = (
                "你是医疗指标体系专家。给定逻辑度量，仅返回合法 JSON"
                "（不要解释、不要 markdown）：\n"
                f'{{"synonyms":["同义词1","同义词2"],"stat_caliber":"统计口径","domain":"业务域code",'
                f'"source_system":["源头系统"],"description":"精炼描述"}}\n'
                f"名称：{data.name}\n描述：{data.description or '无'}\n"
                f"参考源表：{data.source_table or '无'}\n可选业务域：{candidates}\n"
                f"domain 必须取业务域 code 之一（不确定用空字符串）。"
            )
            resp = await llm_client.chat(
                messages=[{"role": "user", "content": prompt}], max_tokens=300
            )
            raw = (resp.get("content") or "").strip()
            # 提取 JSON：容忍 ```json 包裹
            if "```" in raw:
                raw = raw.split("```")[1] if "```" in raw else raw
            raw = raw.strip().strip("`").strip()
            start, end = raw.find("{"), raw.rfind("}")
            if start < 0 or end < start:
                return {}
            parsed = json.loads(raw[start : end + 1])
            out: dict[str, Any] = {}
            for key in ("synonyms", "stat_caliber", "domain", "source_system", "description"):
                val = parsed.get(key)
                if isinstance(val, str):
                    val = val.strip()
                if val in (None, "", []):
                    continue
                if key == "domain" and val not in domains.values():
                    continue  # 推断出的域不在现有域集合 → 丢弃，防脏域
                if key == "synonyms" and isinstance(val, list):
                    val = [str(s).strip() for s in val if str(s).strip()][:5]
                if key == "source_system" and isinstance(val, list):
                    val = [str(s).strip() for s in val if str(s).strip()][:3]
                out[key] = val
            return out
        except Exception:
            return {}  # LLM 不可用/解析失败 → 规则兜底，不阻断

    async def infer_synonyms(self, name: str, description: str | None = None) -> list[str]:
        """编辑弹窗「AI 生成同义词」：基于名称/描述生成同义词候选。

        纯 LLM 调用（不落库，落库仍走既有 update 流程）；LLM 不可用/空内容/
        异常内容 → ``LLM_INFER_UNAVAILABLE``（与 infer-dict-description 对齐）；
        返回有效但非数组 → 空列表（前端提示未生成）。
        """
        from app.core.exceptions import BusinessError
        from app.services.llm.config_service import LlmConfigService
        from app.services.llm.parse import is_abnormal_llm_text

        llm_client = await LlmConfigService(self._session).build_client()
        if not getattr(llm_client, "enabled", False):
            raise BusinessError(
                "LLM 不可用：请检查 LLM 配置或稍后重试",
                error_code="LLM_INFER_UNAVAILABLE",
            )
        prompt = (
            "你是医疗指标体系专家。给定逻辑度量，生成 3-5 个同义词"
            "（其他叫法/别名/简称，供统一查询与查重匹配），仅返回 JSON 数组"
            "如 [\"同义词1\",\"同义词2\"]，不要解释、不要 markdown。\n"
            f"度量名称：{name}\n描述：{description or '无'}"
        )
        try:
            resp = await llm_client.chat(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                # 要求 JSON 数组，显式 json_object 约束（与 auto_suggest 一致）
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 - LLM 网络/超时等统一转业务错误
            logger.warning(
                "measure_infer_synonyms_llm_failed",
                name=name,
                error=str(exc)[:200],
            )
            raise BusinessError(
                "LLM 调用失败，请稍后重试",
                error_code="LLM_INFER_UNAVAILABLE",
            ) from exc

        raw = (resp.get("content") or "").strip()
        if not raw or is_abnormal_llm_text(raw):
            raise BusinessError(
                "LLM 未返回有效内容，请重试",
                error_code="LLM_INFER_UNAVAILABLE",
            )
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                return []
            return [str(s).strip() for s in parsed if str(s).strip()][:8]
        except Exception:
            return []

    @staticmethod
    def _match_keyword(text: str, table: dict[str, list[str]]) -> str | None:
        """返回首个命中关键词的 key（按表内关键词顺序）。"""
        for key, kws in table.items():
            if any(kw in text for kw in kws):
                return key
        return None

    @staticmethod
    def _matched_keyword(text: str, table: dict[str, list[str]], fallback: str) -> str:
        """返回命中的具体关键词（供 reason 展示），未命中返回 fallback 值本身。"""
        for kws in table.values():
            for kw in kws:
                if kw in text:
                    return kw
        return fallback

    async def _require(self, measure_code: str) -> MeasureCatalog:
        """加载逻辑度量并校验可操作：不存在或已软删（回收站）均拒绝。

        已软删记录除「恢复 / 彻底删除」外不可变——防止回收站中的记录被
        更新/提交/通过/发布/废弃/重新启用等操作复活成矛盾态（如 PUBLISHED +
        deleted_at）。恢复/彻底删除用 ``_repo.get`` 直取，不走本守卫。
        """
        measure = await self._repo.get(measure_code)
        if measure is None:
            raise NotFoundError(f"逻辑度量不存在: {measure_code}")
        if getattr(measure, "deleted_at", None) is not None:
            raise UnisenseError(
                f"已删除的逻辑度量不可执行该操作（{measure_code}），"
                "请先在回收站恢复或彻底删除",
                error_code="INVALID_STATE",
            )
        return measure
