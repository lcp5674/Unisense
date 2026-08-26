"""主数据审核流共享实现（统一「主数据审核」复用模式）。

背景：逻辑度量/维度/术语三类主数据此前 DRAFT → PUBLISHED 直接发布、无审核，
形成"基础层比上层更松"的治理倒挂（基础定义静默传播到下游指标）。本 Mixin
集中审核流完整逻辑（提交/指派/通过/驳回 + 评审权校验 + 自审禁止 + 通知），
三个服务继承后仅需配置少量类属性，避免三套重复代码。

宿主 service 用法::

    class DimensionService(BaseService, MasterDataReviewMixin):
        _review_entity_name = "维度"
        _review_event_prefix = "dimension"
        _review_code_attr = "dim_code"
        _review_status_enum = DimensionStatus

        async def submit_dimension(self, dim_code, request, actor_id, role, user_domain):
            dim = await self._require(dim_code)
            await self._submit_review(dim, request, actor_id, role, user_domain, code=dim_code)
            return dim

可选钩子：
- ``_review_completeness(entity) -> str | None``：提交前完整性校验，返回缺失说明
  （非 None 时拒绝提交）或 None（通过）。默认无校验。

依赖约定：
- 实体模型须复用 ``app.models.review_fields.ReviewFieldsMixin``（9 审核字段名一致）；
- 宿主服务须有 ``self._repo``（commit 用）与 ``self._session``。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.core.exceptions import AuthError, UnisenseError, ValidationError

logger = logging.getLogger(__name__)


class MasterDataReviewMixin:
    """主数据审核流共享实现（submit/approve/reject + 评审权 + 通知）。"""

    #: 实体中文名（"逻辑度量"/"维度"/"术语"，用于报错与通知标题）
    _review_entity_name: str = "主数据"
    #: 通知事件前缀（"measure"/"dimension"/"term"，事件类型形如 ``{prefix}.submitted``）
    _review_event_prefix: str = "master_data"
    #: 编码字段名（"measure_code"/"dim_code"/"term_code"）
    _review_code_attr: str = "code"
    #: 状态枚举类（须含 REVIEW；如 MeasureStatus / DimensionStatus / TermStatus）
    _review_status_enum: Any = None

    def _review_code(self, entity: Any) -> str:
        return str(getattr(entity, self._review_code_attr))

    def _review_completeness(self, entity: Any) -> str | None:
        """提交前完整性校验钩子：返回缺失说明（非 None 拒绝提交）或 None（通过）。"""
        return None

    def _review_domain(self, entity: Any) -> str:
        return str(entity.domain)

    # ---- 状态机 ----

    async def _submit_review(
        self,
        entity: Any,
        request: Any,
        actor_id: int,
        role: str | None,
        user_domain: str | None,
        *,
        code: str | None = None,
    ) -> None:
        """提交审核（DRAFT → REVIEW，对齐指标审核流 TD §13）。

        评审指派解析：user 类型须带 reviewer_id；domain 类型缺省用实体自身域；
        均不传则未指派（域管理员兜底评审）。重新提审清空历史驳回原因。
        """
        code = code or self._review_code(entity)
        self._assert_owner_or_admin(entity, actor_id, role or "")
        status = self._review_status_enum
        if entity.status != status.DRAFT.value:
            raise UnisenseError(
                f"仅 {status.DRAFT.value} 状态可提交审核，当前 {entity.status}",
                error_code="INVALID_STATE",
            )
        # 完整性校验：提交审核的主数据须满足业务必填，否则评审人无法判断合理性
        missing = self._review_completeness(entity)
        if missing:
            raise ValidationError(
                f"{self._review_entity_name} {code} {missing}，请先编辑完善后再提交审核",
                error_code="DEFINITION_INCOMPLETE",
            )
        # 评审指派解析（TD §13）
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
            reviewer_updates["reviewer_domain"] = (
                request.reviewer_domain or self._review_domain(entity)
            )
        elif request.reviewer_id:
            # 兼容旧调用：仅传 reviewer_id（未显式声明类型）按 user 处理
            reviewer_updates["reviewer_id"] = request.reviewer_id
            reviewer_updates["reviewer_type"] = "user"

        entity.status = status.REVIEW.value
        entity.submitted_by = actor_id
        entity.reviewer_id = reviewer_updates["reviewer_id"]
        entity.reviewer_type = reviewer_updates["reviewer_type"]
        entity.reviewer_domain = reviewer_updates["reviewer_domain"]
        # 重新提审即清空历史驳回原因（生命周期闭环）
        entity.reject_reason = None
        entity.reject_reviewer_id = None
        entity.rejected_at = None
        await self._repo.commit()
        await self._notify_reviewers(
            entity,
            f"{self._review_event_prefix}.submitted",
            f"{self._review_entity_name}待审核",
            reviewer_id=reviewer_updates["reviewer_id"],
            reason=request.change_reason,
        )

    async def _approve_review(
        self,
        entity: Any,
        request: Any,
        actor_id: int,
        role: str | None,
        user_domain: str | None,
        *,
        code: str | None = None,
    ) -> None:
        """审核通过（REVIEW → PUBLISHED，对齐指标审核流 FR-004）。

        评审人身份校验 + 自审禁止（管理员豁免）+ 状态机校验。
        """
        code = code or self._review_code(entity)
        self._assert_reviewer_authorized(entity, actor_id, role or "", user_domain)
        # 自审禁止：提交人与审核人不得为同一人；管理员豁免（小团队单管理员兜底）
        if (
            role not in ("platform_admin", "domain_admin")
            and entity.submitted_by is not None
            and entity.submitted_by == actor_id
        ):
            raise UnisenseError(
                "提交人与审核人不得为同一人（禁止自审）",
                error_code="SELF_REVIEW_BLOCKED",
                ctx={self._review_code_attr: code, "submitted_by": entity.submitted_by},
            )
        status = self._review_status_enum
        if entity.status != status.REVIEW.value:
            raise UnisenseError(
                f"仅 {status.REVIEW.value} 状态可审核通过，当前 {entity.status}",
                error_code="INVALID_STATE",
            )
        entity.status = status.PUBLISHED.value
        entity.approver_id = actor_id
        entity.reviewed_at = datetime.now(UTC).replace(tzinfo=None)
        # 审核通过即清空历史驳回原因（生命周期闭环）
        entity.reject_reason = None
        entity.reject_reviewer_id = None
        entity.rejected_at = None
        await self._repo.commit()
        await self._notify_submitter(
            entity,
            f"{self._review_event_prefix}.approved",
            f"{self._review_entity_name}已通过",
            payload={
                self._review_code_attr: code,
                "domain": self._review_domain(entity),
            },
        )

    async def _reject_review(
        self,
        entity: Any,
        request: Any,
        actor_id: int,
        role: str | None,
        user_domain: str | None,
        *,
        code: str | None = None,
    ) -> None:
        """审核驳回（REVIEW → DRAFT，对齐指标审核流 FR-005）。

        驳回原因落库（可追溯），通知提交人引导修改后重提。
        """
        code = code or self._review_code(entity)
        self._assert_reviewer_authorized(entity, actor_id, role or "", user_domain)
        if (
            role not in ("platform_admin", "domain_admin")
            and entity.submitted_by is not None
            and entity.submitted_by == actor_id
        ):
            raise UnisenseError(
                "提交人与审核人不得为同一人（禁止自审）",
                error_code="SELF_REVIEW_BLOCKED",
                ctx={self._review_code_attr: code, "submitted_by": entity.submitted_by},
            )
        status = self._review_status_enum
        if entity.status != status.REVIEW.value:
            raise UnisenseError(
                f"仅 {status.REVIEW.value} 状态可驳回，当前 {entity.status}",
                error_code="INVALID_STATE",
            )
        now = datetime.now(UTC).replace(tzinfo=None)
        entity.status = status.DRAFT.value
        entity.reject_reason = (request.reason or "").strip()[:500]
        entity.reject_reviewer_id = actor_id
        entity.rejected_at = now
        entity.reviewed_at = now
        await self._repo.commit()
        await self._notify_submitter(
            entity,
            f"{self._review_event_prefix}.rejected",
            f"{self._review_entity_name}已驳回",
            payload={
                self._review_code_attr: code,
                "domain": self._review_domain(entity),
                "reason": request.reason,
            },
        )

    # ---- 权限 ----

    def _assert_owner_or_admin(self, entity: Any, actor_id: int, role: str) -> None:
        """越权守卫：metric_owner 仅可操作本人创建的主数据。

        platform_admin / domain_admin 放行；metric_owner 校验 owner_id == actor_id；
        其余角色一律拒绝（对齐指标 _assert_owner_or_admin）。
        """
        if role in ("platform_admin", "domain_admin"):
            return
        if role == "metric_owner":
            if entity.owner_id == actor_id:
                return
            raise AuthError(
                f"无权操作他人{self._review_entity_name}",
                error_code="FORBIDDEN",
                ctx={
                    self._review_code_attr: self._review_code(entity),
                    "actor_id": actor_id,
                    "owner_id": entity.owner_id,
                },
            )
        raise AuthError(
            f"无权操作该{self._review_entity_name}",
            error_code="FORBIDDEN",
            ctx={self._review_code_attr: self._review_code(entity), "role": role},
        )

    def _assert_reviewer_authorized(
        self,
        entity: Any,
        actor_id: int,
        role: str,
        user_domain: str | None,
    ) -> None:
        """评审人身份校验：仅被指派评审人可通过/打回主数据（对齐指标 TD §13）。

        - ``platform_admin``：始终可审（最终兜底）。
        - ``reviewer_type=user``：仅 ``reviewer_id`` 指定的用户可审。
        - ``reviewer_type=domain``：仅该域 ``domain_admin``/``reviewer`` 角色用户可审。
        - 未指派：``domain_admin`` 兜底可审。
        """
        code = self._review_code(entity)
        if role == "platform_admin":
            return
        if entity.reviewer_type == "user" and entity.reviewer_id is not None:
            if actor_id != entity.reviewer_id:
                raise AuthError(
                    f"该{self._review_entity_name}已指派给指定评审人，仅被指派者可通过/打回",
                    error_code="FORBIDDEN_REVIEWER",
                    ctx={self._review_code_attr: code, "reviewer_id": entity.reviewer_id},
                )
            return
        if entity.reviewer_type == "domain" and entity.reviewer_domain:
            if role not in ("domain_admin", "reviewer"):
                raise AuthError(
                    f"该{self._review_entity_name}已指派给域评审组，仅域管理员/评审员可通过/打回",
                    error_code="FORBIDDEN_REVIEWER",
                    ctx={
                        self._review_code_attr: code,
                        "reviewer_domain": entity.reviewer_domain,
                    },
                )
            if user_domain != entity.reviewer_domain:
                raise AuthError(
                    f"仅 {entity.reviewer_domain} 域评审组成员可评审该{self._review_entity_name}",
                    error_code="FORBIDDEN_REVIEWER",
                    ctx={self._review_code_attr: code, "user_domain": user_domain},
                )
            return
        # 未指派：域管理员兜底（保持"仅管理角色可审"语义）。
        # X-3 越权加固：未指派分支此前只查 role==domain_admin 不校验 user_domain——
        # 任意域 domain_admin 可跨域审批/打回他域未指派主数据。现补本域归属校验：
        # 实体可解析出域且不在评审人本域时拒绝（对齐指标批量审批的 PDP 域闸门）。
        if role != "domain_admin":
            raise AuthError(
                f"未指派评审人，仅域管理员可评审该{self._review_entity_name}",
                error_code="FORBIDDEN_REVIEWER",
                ctx={self._review_code_attr: code, "role": role},
            )
        entity_domain = self._review_domain(entity)
        if entity_domain and user_domain != entity_domain:
            raise AuthError(
                f"无权评审他域{self._review_entity_name}（当前域: {user_domain}，"
                f"实体域: {entity_domain}）",
                error_code="FORBIDDEN_REVIEWER",
                ctx={
                    self._review_code_attr: code,
                    "user_domain": user_domain,
                    "entity_domain": entity_domain,
                },
            )

    # ---- 通知 ----

    async def _notify_reviewers(
        self,
        entity: Any,
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
                    User.domain == self._review_domain(entity),
                )
                result = await session.execute(stmt)
                targets = [r[0] for r in result.all()]
            # 排除提交人本人（自审已被禁止，通知列表亦不应包含自己）
            if entity.submitted_by is not None:
                targets = [uid for uid in targets if uid != entity.submitted_by]
        for uid in targets:
            async with async_session_factory() as session:
                try:
                    await NotifyService(session).notify_user(
                        user_id=uid,
                        event_type=event_type,
                        title=title,
                        payload={
                            self._review_code_attr: self._review_code(entity),
                            "domain": self._review_domain(entity),
                            "reason": reason,
                            "submitter_id": entity.submitted_by,
                        },
                    )
                except Exception:  # noqa: BLE001 - 通知失败不阻断审核主流程
                    logger.warning(
                        "master_data_reviewer_notify_failed event=%s code=%s user=%s",
                        event_type, self._review_code(entity), uid,
                    )
        return None

    async def _notify_submitter(
        self,
        entity: Any,
        event_type: str,
        title: str,
        *,
        payload: dict[str, Any],
    ) -> None:
        """审核结果通知提交人（独立 session，best-effort）。"""
        from app.db.mysql import async_session_factory
        from app.services.notify.service import NotifyService

        if entity.submitted_by is None:
            return
        async with async_session_factory() as session:
            try:
                await NotifyService(session).notify_user(
                    user_id=entity.submitted_by,
                    event_type=event_type,
                    title=title,
                    payload=payload,
                )
            except Exception:  # noqa: BLE001 - 通知失败不阻断审核主流程
                logger.warning(
                    "master_data_submitter_notify_failed event=%s code=%s user=%s",
                    event_type, self._review_code(entity), entity.submitted_by,
                )
        return None
