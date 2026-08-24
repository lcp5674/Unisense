"""逻辑度量目录服务（OneData 原子层，TD §4.2 / FR-02-08）。

核心能力：
1. 逻辑度量 CRUD + 状态机（DRAFT → PUBLISHED → DEPRECATED）。
2. 度量格式联动默认（金额:元/2 位，比率:小数/4 位，数值:自定义）——PRD FR-02-08。
3. 废弃保护：被指标引用的度量禁止废弃（防原子指标悬空）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.codegen import generate_unique_code, slugify_code
from app.core.exceptions import (
    AuthError,
    ConflictError,
    NotFoundError,
    UnisenseError,
    ValidationError,
)
from app.models.measure_catalog import MeasureCatalog, MeasureCategory, MeasureFormat, MeasureStatus
from app.models.subject_domain import SubjectDomain
from app.services.measure_catalog.repository import MeasureCatalogRepository
from app.services.measure_catalog.schemas import (
    _FORMAT_DEFAULTS,
    MeasureApproveRequest,
    MeasureAutoSuggestRequest,
    MeasureCreate,
    MeasureRejectRequest,
    MeasureSubmitRequest,
    MeasureSuggestResponse,
    MeasureUpdate,
    SuggestField,
)

logger = logging.getLogger(__name__)

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
        request: MeasureSubmitRequest,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> MeasureCatalog:
        """提交度量审核（DRAFT → REVIEW，对齐指标审核流 TD §13）。

        度量是原子指标的权威继承源（单位/格式/小数位/口径直接传播到下游指标），
        故发布须先提交审核；口径完整性校验保证评审人可判断。
        """
        measure = await self._require(measure_code)
        self._assert_owner_or_admin(measure, actor_id, role or "")
        if measure.status != MeasureStatus.DRAFT.value:
            raise UnisenseError(
                f"仅 DRAFT 状态可提交审核，当前 {measure.status}",
                error_code="INVALID_STATE",
            )
        # 口径完整性校验：提交审核的度量必须有统计口径，否则评审人无法判断
        # 度量定义合理性（对齐指标 submit 的空心指标拦截语义）。
        if not (measure.stat_caliber or "").strip():
            raise ValidationError(
                f"逻辑度量 {measure_code} 尚未填写统计口径，请先编辑完善后再提交审核",
                error_code="DEFINITION_INCOMPLETE",
            )
        # 评审指派解析（TD §13）：user 类型须带 reviewer_id；domain 类型缺省用度量自身域；
        # 均不传则未指派（域管理员兜底评审）。
        reviewer_updates: dict[str, Any] = {
            "reviewer_id": None,
            "reviewer_type": None,
            "reviewer_domain": None,
        }
        rtype = request.reviewer_type
        if rtype == "user":
            if not request.reviewer_id:
                raise ValidationError(
                    "指定评审用户时须填写评审人",
                    error_code="REVIEWER_ASSIGN_INVALID",
                )
            reviewer_updates["reviewer_id"] = request.reviewer_id
            reviewer_updates["reviewer_type"] = "user"
        elif rtype == "domain":
            reviewer_updates["reviewer_type"] = "domain"
            reviewer_updates["reviewer_domain"] = request.reviewer_domain or measure.domain
        elif request.reviewer_id:
            # 兼容旧调用：仅传 reviewer_id（未显式声明类型）按 user 处理
            reviewer_updates["reviewer_id"] = request.reviewer_id
            reviewer_updates["reviewer_type"] = "user"

        measure.status = MeasureStatus.REVIEW.value
        measure.submitted_by = actor_id
        measure.reviewer_id = reviewer_updates["reviewer_id"]
        measure.reviewer_type = reviewer_updates["reviewer_type"]
        measure.reviewer_domain = reviewer_updates["reviewer_domain"]
        # 重新提审即清空历史驳回原因（生命周期闭环）
        measure.reject_reason = None
        measure.reject_reviewer_id = None
        measure.rejected_at = None
        await self._repo.commit()
        await self._notify_reviewers(
            measure, "measure.submitted", "度量待审核",
            reviewer_id=reviewer_updates["reviewer_id"],
            reason=request.change_reason,
        )
        return measure

    async def approve_measure(
        self,
        measure_code: str,
        request: MeasureApproveRequest,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> MeasureCatalog:
        """审核通过度量（REVIEW → PUBLISHED，对齐指标审核流 FR-004）。

        评审人身份校验 + 自审禁止（管理员豁免）+ 状态机校验。
        """
        measure = await self._require(measure_code)
        self._assert_reviewer_authorized(measure, actor_id, role or "", user_domain)
        # 自审禁止：提交人与审核人不得为同一人；管理员豁免（小团队单管理员兜底）
        if (
            role not in ("platform_admin", "domain_admin")
            and measure.submitted_by is not None
            and measure.submitted_by == actor_id
        ):
            raise UnisenseError(
                "提交人与审核人不得为同一人（禁止自审）",
                error_code="SELF_REVIEW_BLOCKED",
                ctx={"measure_code": measure_code, "submitted_by": measure.submitted_by},
            )
        if measure.status != MeasureStatus.REVIEW.value:
            raise UnisenseError(
                f"仅 REVIEW 状态可审核通过，当前 {measure.status}",
                error_code="INVALID_STATE",
            )
        measure.status = MeasureStatus.PUBLISHED.value
        measure.approver_id = actor_id
        measure.reviewed_at = datetime.now(UTC).replace(tzinfo=None)
        # 审核通过即清空历史驳回原因（生命周期闭环）
        measure.reject_reason = None
        measure.reject_reviewer_id = None
        measure.rejected_at = None
        await self._repo.commit()
        await self._notify_submitter(
            measure, "measure.approved", "度量已通过",
            payload={"measure_code": measure_code, "domain": measure.domain},
        )
        return measure

    async def reject_measure(
        self,
        measure_code: str,
        request: MeasureRejectRequest,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> MeasureCatalog:
        """审核驳回度量（REVIEW → DRAFT，对齐指标审核流 FR-005）。

        驳回原因落库（可追溯），通知提交人引导修改后重提。
        """
        measure = await self._require(measure_code)
        self._assert_reviewer_authorized(measure, actor_id, role or "", user_domain)
        if (
            role not in ("platform_admin", "domain_admin")
            and measure.submitted_by is not None
            and measure.submitted_by == actor_id
        ):
            raise UnisenseError(
                "提交人与审核人不得为同一人（禁止自审）",
                error_code="SELF_REVIEW_BLOCKED",
                ctx={"measure_code": measure_code, "submitted_by": measure.submitted_by},
            )
        if measure.status != MeasureStatus.REVIEW.value:
            raise UnisenseError(
                f"仅 REVIEW 状态可驳回，当前 {measure.status}",
                error_code="INVALID_STATE",
            )
        now = datetime.now(UTC).replace(tzinfo=None)
        measure.status = MeasureStatus.DRAFT.value
        measure.reject_reason = (request.reason or "").strip()[:500]
        measure.reject_reviewer_id = actor_id
        measure.rejected_at = now
        measure.reviewed_at = now
        await self._repo.commit()
        await self._notify_submitter(
            measure, "measure.rejected", "度量已驳回",
            payload={
                "measure_code": measure_code,
                "domain": measure.domain,
                "reason": request.reason,
            },
        )
        return measure

    def _assert_owner_or_admin(self, measure: MeasureCatalog, actor_id: int, role: str) -> None:
        """越权守卫：metric_owner 仅可操作本人创建的度量。

        platform_admin / domain_admin 放行；metric_owner 校验 owner_id == actor_id；
        其余角色一律拒绝（对齐指标 _assert_owner_or_admin，度量无副 Owner）。
        """
        if role in ("platform_admin", "domain_admin"):
            return
        if role == "metric_owner":
            if measure.owner_id == actor_id:
                return
            raise AuthError(
                "无权操作他人逻辑度量",
                error_code="FORBIDDEN",
                ctx={
                    "measure_code": measure.measure_code,
                    "actor_id": actor_id,
                    "owner_id": measure.owner_id,
                },
            )
        raise AuthError(
            "无权操作该逻辑度量",
            error_code="FORBIDDEN",
            ctx={"measure_code": measure.measure_code, "role": role},
        )

    def _assert_reviewer_authorized(
        self,
        measure: MeasureCatalog,
        actor_id: int,
        role: str,
        user_domain: str | None,
    ) -> None:
        """评审人身份校验：仅被指派评审人可通过/打回度量（对齐指标 TD §13）。

        - ``platform_admin``：始终可审（最终兜底）。
        - ``reviewer_type=user``：仅 ``reviewer_id`` 指定的用户可审。
        - ``reviewer_type=domain``：仅该域 ``domain_admin``/``reviewer`` 角色用户可审。
        - 未指派：``domain_admin`` 兜底可审。
        """
        if role == "platform_admin":
            return
        if measure.reviewer_type == "user" and measure.reviewer_id is not None:
            if actor_id != measure.reviewer_id:
                raise AuthError(
                    "该度量已指派给指定评审人，仅被指派者可通过/打回",
                    error_code="FORBIDDEN_REVIEWER",
                    ctx={"measure_code": measure.measure_code, "reviewer_id": measure.reviewer_id},
                )
            return
        if measure.reviewer_type == "domain" and measure.reviewer_domain:
            if role not in ("domain_admin", "reviewer"):
                raise AuthError(
                    "该度量已指派给域评审组，仅域管理员/评审员可通过/打回",
                    error_code="FORBIDDEN_REVIEWER",
                    ctx={
                        "measure_code": measure.measure_code,
                        "reviewer_domain": measure.reviewer_domain,
                    },
                )
            if user_domain != measure.reviewer_domain:
                raise AuthError(
                    f"仅 {measure.reviewer_domain} 域评审组成员可评审该度量",
                    error_code="FORBIDDEN_REVIEWER",
                    ctx={"measure_code": measure.measure_code, "user_domain": user_domain},
                )
            return
        # 未指派：域管理员兜底（保持"仅管理角色可审"语义）
        if role != "domain_admin":
            raise AuthError(
                "未指派评审人，仅域管理员可评审该度量",
                error_code="FORBIDDEN_REVIEWER",
                ctx={"measure_code": measure.measure_code, "role": role},
            )

    async def _notify_reviewers(
        self,
        measure: MeasureCatalog,
        event_type: str,
        title: str,
        *,
        reviewer_id: int | None,
        reason: str | None,
    ) -> None:
        """提交审核后通知评审人（独立 session，best-effort 不阻断主流程）。

        - 已指派评审人：仅通知被指派者；
        - 未指派：通知该域可审核角色（domain_admin/reviewer，active）。
        """
        from app.db.mysql import async_session_factory
        from app.services.notify.service import NotifyService

        targets: list[int] = []
        if reviewer_id is not None:
            targets = [reviewer_id]
        else:
            from sqlalchemy import select

            from app.models.user import User

            async with async_session_factory() as session:
                stmt = select(User.id).where(
                    User.status == "active",
                    User.role.in_(("domain_admin", "reviewer")),
                    User.domain == measure.domain,
                )
                result = await session.execute(stmt)
                targets = [r[0] for r in result.all()]
            # 排除提交人本人（自审已被禁止，通知列表亦不应包含自己）
            if measure.submitted_by is not None:
                targets = [uid for uid in targets if uid != measure.submitted_by]
        for uid in targets:
            async with async_session_factory() as session:
                try:
                    await NotifyService(session).notify_user(
                        user_id=uid,
                        event_type=event_type,
                        title=title,
                        payload={
                            "measure_code": measure.measure_code,
                            "domain": measure.domain,
                            "reason": reason,
                            "submitter_id": measure.submitted_by,
                        },
                    )
                except Exception:  # noqa: BLE001 - 通知失败不阻断审核主流程
                    logger.warning(
                        "measure_reviewer_notify_failed event=%s measure=%s user=%s",
                        event_type, measure.measure_code, uid,
                    )
        return None

    async def _notify_submitter(
        self,
        measure: MeasureCatalog,
        event_type: str,
        title: str,
        *,
        payload: dict[str, Any],
    ) -> None:
        """审核结果通知提交人（独立 session，best-effort）。"""
        from app.db.mysql import async_session_factory
        from app.services.notify.service import NotifyService

        if measure.submitted_by is None:
            return
        async with async_session_factory() as session:
            try:
                await NotifyService(session).notify_user(
                    user_id=measure.submitted_by,
                    event_type=event_type,
                    title=title,
                    payload=payload,
                )
            except Exception:  # noqa: BLE001 - 通知失败不阻断审核主流程
                logger.warning(
                    "measure_submitter_notify_failed event=%s measure=%s user=%s",
                    event_type, measure.measure_code, measure.submitted_by,
                )
        return None

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
        measure = await self._repo.get(measure_code)
        if measure is None:
            raise NotFoundError(f"逻辑度量不存在: {measure_code}")
        return measure
