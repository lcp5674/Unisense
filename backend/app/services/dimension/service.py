"""维度管理服务（TD §12.15 / FR-05 / FR-09）。

核心能力：
1. 维度主表 CRUD + 状态机（DRAFT→PUBLISHED→DEPRECATED）。
2. 维度成员（维值/层级）维护，支持 parent_code 层级。
3. 维度映射（等价/部分）维护。
4. 指标-维度关联（PARTITION/SPLICE/FILTER）。
5. 口径对账（提交 + 人工复核 APPROVED/REJECTED）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.exceptions import (
    BusinessError,
    ConflictError,
    ExternalDependencyError,
    NotFoundError,
    UnisenseError,
    ValidationError,
)
from app.core.logging import get_logger
from app.models.dimension import (
    Dimension,
    DimensionMapping,
    DimensionMappingValue,
    DimensionMember,
    DimensionSnapshotRun,
    DimensionStatus,
    DimensionType,
    DimensionValueSnapshot,
    MappingType,
    MetricDimension,
    MetricDimensionRole,
    Reconciliation,
    ReconciliationStatus,
    SnapshotRunStatus,
    SnapshotStatus,
    SyncMode,
)
from app.models.metric import Metric
from app.models.metric_version import MetricVersion
from app.services.dimension.repository import DimensionRepository
from app.services.dimension.schemas import (
    DimensionCreate,
    DimensionMappingCreate,
    DimensionMappingUpdate,
    DimensionMemberCreate,
    DimensionMemberUpdate,
    DimensionReferenceBind,
    DimensionUpdate,
    MappingCoverageResponse,
    MappingValueCreate,
    MetricDimensionBind,
    ReconciliationReview,
    ReconciliationSubmit,
    TranslateResult,
)
from app.services.master_data_review.service import MasterDataReviewMixin

logger = get_logger("unisense.dimension")

#: 维度成员层级深度上限（含根，1 层 = 根；防止深链/环导致的递归遍历风险）。
_MAX_MEMBER_DEPTH = 10

#: 合法映射类型 / 关联角色 / 缓慢变化维类型取值（DB Enum 列，非法值须在服务层转 4xx，而非 DB 500）。
_VALID_MAPPING_TYPES = {e.value for e in MappingType}
_VALID_ROLES = {e.value for e in MetricDimensionRole}
_VALID_DIM_TYPES = {e.value for e in DimensionType}
_VALID_MEMBER_STATUSES = {e.value for e in DimensionStatus}


class DimensionService(BaseService, MasterDataReviewMixin):
    """维度管理服务：复用 ``MasterDataReviewMixin`` 审核流（DRAFT→REVIEW→PUBLISHED→DEPRECATED）。"""

    _review_entity_name = "维度"
    _review_event_prefix = "dimension"
    _review_code_attr = "dim_code"
    _review_status_enum = DimensionStatus

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._session = session
        self._repo = DimensionRepository(session)

    async def _generate_dim_code(self, data: DimensionCreate) -> str:
        """自动生成唯一维度编码：``{domain_slug}_{name_slug}``，冲突自增后缀。"""
        from app.core.codegen import generate_unique_code, slugify_code

        domain_slug = slugify_code(data.domain)
        name_slug = slugify_code(data.name)
        if domain_slug and name_slug:
            base = f"{domain_slug}_{name_slug}"
        elif name_slug:
            base = f"dim_{name_slug}"
        elif domain_slug:
            base = f"dim_{domain_slug}"
        else:
            base = "dim"

        async def _exists(code: str) -> bool:
            return await self._repo.get_dimension(code) is not None

        return await generate_unique_code(base, _exists)

    async def create_dimension(
        self, data: DimensionCreate, actor_id: int | None = None
    ) -> Dimension:
        # 编码自动生成（FR-010：缺省时由系统生成，非人为创造）
        if not data.dim_code:
            data.dim_code = await self._generate_dim_code(data)
        if await self._repo.get_dimension(data.dim_code) is not None:
            raise ConflictError(f"维度编码已存在: {data.dim_code}", error_code="DIM_EXISTS")
        # 缓慢变化维类型 enum 显式校验（非法值 → 4xx，而非 DB Enum 500）
        if data.type not in _VALID_DIM_TYPES:
            raise ValidationError(
                f"未知缓慢变化维类型: {data.type}",
                error_code="INVALID_DIM_TYPE",
                ctx={"type": data.type},
            )
        dim = Dimension(
            dim_code=data.dim_code,
            name=data.name,
            domain=data.domain,
            type=data.type,
            description=data.description,
            # PLAT-2: 认证身份优先，client 传入的 owner_id 仅作降级
            owner_id=actor_id if actor_id is not None else data.owner_id,
            status=DimensionStatus.DRAFT.value,
        )
        return await self._repo.save_dimension(dim)

    async def get_dimension(self, dim_code: str) -> Dimension:
        dim = await self._repo.get_dimension(dim_code)
        if dim is None:
            raise NotFoundError(f"维度不存在: {dim_code}")
        return dim

    async def list_dimensions(
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
    ) -> tuple[list[tuple[Dimension, int]], int]:
        """分页列出维度，返回 (列表, total)（服务端分页，对齐 glossary）。

        deleted=True 时列出已软删记录（回收站视图）。
        reviewed_by 非空时过滤"我审过的"（通过/驳回人 ID 匹配，供统一主数据审批工作台）。
        """
        limit = min(max(page_size, 1), 200)
        offset = (max(page, 1) - 1) * limit
        return await self._repo.list_dimensions(
            domain,
            status,
            keyword,
            owner_id,
            reviewed_by=reviewed_by,
            deleted=deleted,
            limit=limit,
            offset=offset,
        )

    async def update_dimension(self, dim_code: str, data: DimensionUpdate) -> Dimension:
        dim = await self._require(dim_code)
        # P11 C-2：跨请求乐观锁——前端编辑弹窗回传 row_version，不一致说明他人已改 → 409
        expected = getattr(data, "row_version", None)
        if expected is not None and expected != dim.row_version:
            raise ConflictError(
                "维度已被他人修改，请刷新后重试",
                error_code="OPTIMISTIC_LOCK_CONFLICT",
                ctx={
                    "dim_code": dim_code,
                    "current_row_version": dim.row_version,
                    "expected_row_version": expected,
                },
            )
        if dim.status == DimensionStatus.DEPRECATED.value:
            raise UnisenseError(f"已废弃维度不可更新: {dim_code}", error_code="INVALID_STATE")
        # 审核中锁定（REVIEW）：评审人基于当前定义审核，审核中改定义会造成评审失真；
        # 驳回回 DRAFT 后即可修改重提（对齐指标 REVIEW 编辑即撤回的语义）。
        if dim.status == DimensionStatus.REVIEW.value:
            raise UnisenseError(
                f"审核中的维度不可编辑（{dim_code}），请等待审核结果或驳回后修改",
                error_code="INVALID_STATE",
            )
        # 改编码：仅 DRAFT 状态允许（已发布/已废弃维度改编码会破坏线上引用）；
        # 校验新编码唯一，事务内级联更新成员/映射/绑定引用。
        if data.dim_code is not None and data.dim_code != dim_code:
            if dim.status != DimensionStatus.DRAFT.value:
                raise UnisenseError(
                    f"仅 DRAFT 状态可修改编码，当前 {dim.status}；已发布维度请先废弃重建",
                    error_code="INVALID_STATE",
                )
            if await self._repo.get_dimension(data.dim_code) is not None:
                raise ConflictError(f"维度编码已存在: {data.dim_code}", error_code="DIM_EXISTS")
            await self._repo.rename_dimension_references(dim_code, data.dim_code)
            # 跨服务一致：同步已绑定指标口径里的维度声明（definition_json.dimensions 旧→新）。
            # 否则消费校验/血缘 USES_DIMENSION 边仍指向旧码 → 悬空。
            await self._rename_dimension_in_metric_definitions(dim_code, data.dim_code)
            dim.dim_code = data.dim_code
        if data.name is not None:
            dim.name = data.name
        if data.domain is not None:
            dim.domain = data.domain
        if data.type is not None:
            if data.type not in _VALID_DIM_TYPES:
                raise ValidationError(
                    f"未知缓慢变化维类型: {data.type}",
                    error_code="INVALID_DIM_TYPE",
                    ctx={"type": data.type},
                )
            dim.type = data.type
        if data.description is not None:
            dim.description = data.description
        # 防御式递增（测试构造的简易对象可能无 row_version 属性）
        dim.row_version = (getattr(dim, "row_version", None) or 1) + 1
        await self._repo.commit()
        return dim

    async def publish_dimension(self, dim_code: str) -> Dimension:
        dim = await self._require(dim_code)
        if dim.status != DimensionStatus.DRAFT.value:
            raise UnisenseError(
                f"仅 DRAFT 状态可发布，当前 {dim.status}", error_code="INVALID_STATE"
            )
        dim.status = DimensionStatus.PUBLISHED.value
        await self._repo.commit()
        return dim

    async def submit_dimension(
        self,
        dim_code: str,
        request: Any,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> Dimension:
        """提交维度审核（DRAFT → REVIEW，复用主数据审核流 TD §13）。"""
        dim = await self._require(dim_code)
        await self._submit_review(
            dim, request, actor_id, role, user_domain, code=dim_code
        )
        return dim

    async def approve_dimension(
        self,
        dim_code: str,
        request: Any,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> Dimension:
        """审核通过维度（REVIEW → PUBLISHED，复用主数据审核流 FR-004）。"""
        dim = await self._require(dim_code)
        await self._approve_review(
            dim, request, actor_id, role, user_domain, code=dim_code
        )
        return dim

    async def reject_dimension(
        self,
        dim_code: str,
        request: Any,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> Dimension:
        """审核驳回维度（REVIEW → DRAFT，复用主数据审核流 FR-005）。"""
        dim = await self._require(dim_code)
        await self._reject_review(
            dim, request, actor_id, role, user_domain, code=dim_code
        )
        return dim

    async def deprecate_dimension(self, dim_code: str) -> Dimension:
        dim = await self._require(dim_code)
        # 废弃保护（跨服务一致性）：被指标绑定的维度禁止废弃——否则指标维度声明
        # 悬空（dimension:{code} 血缘节点无对应维度），消费校验也失去维度权威来源。
        bound = await self._repo.count_metric_dimensions(dim_code)
        if bound > 0:
            raise BusinessError(
                f"维度 {dim_code} 正被 {bound} 个指标绑定，无法废弃；请先解绑相关指标",
                error_code="DIMENSION_BOUND_BY_METRICS",
            )
        dim.status = DimensionStatus.DEPRECATED.value
        # 防御性清理：维度废弃后不再可用，其相关血缘边（历史存量/异常路径残留，
        # 正常绑定已由 unbind 即时清理）级联软删——对称于指标废弃时
        # semantic._cleanup_metric_lineage 的边清理。被绑定的维度已在上面被保护，
        # 此处清理不影响任何有效绑定。
        _lsvc = None
        try:
            from app.services.lineage.parser import node_dimension
            from app.services.lineage.service import LineageService

            _lsvc = LineageService(self._session)
            await _lsvc.delete_by_node(node_dimension(dim_code))
        except Exception:  # noqa: BLE001 - 血缘清理失败不阻断维度废弃
            logger.warning("deprecate_dimension_lineage_cleanup_failed", dim_code=dim_code)
        await self._repo.commit()
        if _lsvc is not None:
            try:
                # P0-3：提交后执行延迟的图写/缓存失效副作用（幽灵边根治）
                await _lsvc.run_post_commit()
            except Exception:  # noqa: BLE001 - 副作用 best-effort，不阻断响应
                logger.warning("deprecate_dimension_lineage_post_commit_failed", dim_code=dim_code)
        return dim

    async def reactivate_dimension(self, dim_code: str) -> Dimension:
        """重新启用已废弃维度（DEPRECATED → DRAFT）。

        已废弃维度为终态，重新启用后回到草稿态，可编辑后**重新走审核**（与
        DRAFT→REVIEW→PUBLISHED 审核流一致，避免绕过审核直接复活）。仅平台
        管理员或原 Owner 可执行（API 层写角色 + service 层 owner 校验）。
        """
        dim = await self._require(dim_code)
        if dim.status != DimensionStatus.DEPRECATED.value:
            raise UnisenseError(
                f"仅 DEPRECATED 状态可重新启用，当前 {dim.status}",
                error_code="INVALID_STATE",
            )
        dim.status = DimensionStatus.DRAFT.value
        await self._repo.commit()
        logger.info("dimension_reactivated", dim_code=dim_code)
        return dim

    async def delete_dimension(
        self, dim_code: str, actor_id: int, role: str | None = None
    ) -> Dimension:
        """软删除维度（仅 DRAFT/DEPRECATED 未对外投入状态；REVIEW/PUBLISHED 禁止）。

        删除语义（用户决策）：草稿/废弃这种未对外投入的可交由管理员或生产者
        （原 Owner）软删；审核中/启用中的资源不可删。被指标绑定的维度禁止删除
        （对齐 deprecate_dimension 的 DIMENSION_BOUND_BY_METRICS 保护）。

        Returns:
            被软删的维度（deleted_at 置位，可经 restore 恢复）。
        """
        dim = await self._require(dim_code)
        if dim.status not in (
            DimensionStatus.DRAFT.value,
            DimensionStatus.DEPRECATED.value,
        ):
            raise UnisenseError(
                f"仅 DRAFT/DEPRECATED 状态的维度可删除（当前 {dim.status}）；"
                "审核中/启用中的资源不可删除",
                error_code="INVALID_STATE",
            )
        # 权限：平台/域管理员或原 Owner（生产者）
        if role not in ("platform_admin", "domain_admin") and dim.owner_id != actor_id:
            raise UnisenseError(
                "仅平台/域管理员或维度原 Owner 可删除",
                error_code="FORBIDDEN",
            )
        # 引用保护（跨服务一致性）：被指标绑定的维度禁止删除（同废弃保护）
        bound = await self._repo.count_metric_dimensions(dim_code)
        if bound > 0:
            raise BusinessError(
                f"维度 {dim_code} 正被 {bound} 个指标绑定，无法删除；请先解绑相关指标",
                error_code="DIMENSION_BOUND_BY_METRICS",
            )
        await self._repo.soft_delete_dimension(dim.id)
        # 软删即下线：防御性清理相关血缘边（best-effort，对称于 deprecate_dimension）
        try:
            from app.services.lineage.parser import node_dimension
            from app.services.lineage.service import LineageService

            lsvc = LineageService(self._session)
            await lsvc.delete_by_node(node_dimension(dim_code))
        except Exception:  # noqa: BLE001 - 血缘清理失败不阻断删除
            logger.warning("delete_dimension_lineage_cleanup_failed", dim_code=dim_code)
        await self._repo.commit()
        logger.info("dimension_deleted", dim_code=dim_code, actor_id=actor_id, role=role)
        return dim

    async def restore_dimension(
        self, dim_code: str, actor_id: int, role: str | None = None
    ) -> Dimension:
        """恢复已软删维度（回收站恢复；仅 DRAFT/DEPRECATED 且 deleted_at 置位）。

        仅平台/域管理员或原 Owner 可恢复（对齐删除语义）。清除 deleted_at 使
        维度重新进入正常列表，重新走审核流。
        """
        dim = await self._repo.get_dimension(dim_code)
        if dim is None:
            raise NotFoundError(f"维度不存在: {dim_code}")
        if dim.deleted_at is None:
            raise UnisenseError(
                f"维度 {dim_code} 未处于已删除状态，无需恢复",
                error_code="INVALID_STATE",
            )
        if dim.status not in (
            DimensionStatus.DRAFT.value,
            DimensionStatus.DEPRECATED.value,
        ):
            raise UnisenseError(
                f"仅 DRAFT/DEPRECATED 状态的已删维度可恢复，当前 {dim.status}",
                error_code="INVALID_STATE",
            )
        if role not in ("platform_admin", "domain_admin") and dim.owner_id != actor_id:
            raise UnisenseError(
                "仅平台/域管理员或维度原 Owner 可恢复",
                error_code="FORBIDDEN",
            )
        await self._repo.restore_dimension(dim.id)
        await self._repo.commit()
        logger.info("dimension_restored", dim_code=dim_code, actor_id=actor_id, role=role)
        return dim

    async def _generate_member_code(self, data: DimensionMemberCreate) -> str:
        """自动生成维度成员编码：``{dim_code}_{name_slug}``，维度内唯一，冲突自增。"""
        from app.core.codegen import generate_unique_code, slugify_code

        name_slug = slugify_code(data.member_name)
        base = f"{data.dim_code}_{name_slug}" if name_slug else f"{data.dim_code}_member"

        async def _exists(code: str) -> bool:
            # 定向存在性查询，避免每次尝试全量拉取维度成员（防 N+1 / 全表扫描）
            return await self._repo.get_member(data.dim_code, code) is not None

        return await generate_unique_code(base, _exists)

    def _resolve_member_path(
        self, parent: DimensionMember | None, member_code: str
    ) -> str:
        """层级路径自动推测：根成员 ``/{member_code}``，子成员 ``父path/{member_code}``。"""
        if parent is None:
            return f"/{member_code}"
        if parent.path:
            return f"{parent.path}/{member_code}"
        return f"/{parent.member_code}/{member_code}"

    async def create_member(self, data: DimensionMemberCreate) -> DimensionMember:
        await self._require_active(data.dim_code)

        members = await self._repo.list_members(data.dim_code)

        # 编码唯一性：客户端显式传入时也须校验（uk_dim_member 唯一约束，
        # 不校验会触发 IntegrityError → 500）
        if data.member_code:
            if any(m.member_code == data.member_code for m in members):
                raise ConflictError(
                    f"维度成员编码已存在: {data.dim_code}/{data.member_code}",
                    error_code="DUPLICATE_MEMBER_CODE",
                )
        else:
            data.member_code = await self._generate_member_code(data)

        # 父级校验：存在性 + 自引用
        parent: DimensionMember | None = None
        if data.parent_code:
            if data.parent_code == data.member_code:
                raise ConflictError(
                    "维度成员不能以自身为父级",
                    error_code="SELF_PARENT",
                    ctx={"member_code": data.member_code},
                )
            parent = next(
                (m for m in members if m.member_code == data.parent_code), None
            )
            if parent is None:
                raise NotFoundError(
                    f"父成员不存在: {data.dim_code}/{data.parent_code}",
                    ctx={"parent_code": data.parent_code},
                )

        # 层级路径服务端独占推导：父级为唯一事实源，忽略客户端直传 path 防止层级错位
        data.path = self._resolve_member_path(parent, data.member_code)
        if data.path.count("/") >= _MAX_MEMBER_DEPTH:
            raise ConflictError(
                f"维度成员层级超过上限（{_MAX_MEMBER_DEPTH} 层）",
                error_code="MEMBER_DEPTH_EXCEEDED",
            )

        member = DimensionMember(
            dim_code=data.dim_code,
            member_code=data.member_code,
            member_name=data.member_name,
            parent_code=data.parent_code,
            path=data.path,
            attributes=data.attributes,
            status=data.status,
        )
        return await self._repo.save_member(member)

    async def update_member(
        self, dim_code: str, member_code: str, data: DimensionMemberUpdate
    ) -> DimensionMember:
        """编辑维度成员（member_code 为业务标识不可变更）。

        - 改父级时自动重算 path，并做环防护（不能移动到自身后代之下）。
        - ``parent_code=""`` 表示置为根成员（取消父级）。
        """
        member = await self._repo.get_member(dim_code, member_code)
        if member is None:
            raise NotFoundError(
                f"维度成员不存在: {dim_code}/{member_code}",
                ctx={"member_code": member_code},
            )
        if data.status is not None and data.status not in _VALID_MEMBER_STATUSES:
            raise ValidationError(
                f"未知成员状态: {data.status}",
                error_code="INVALID_MEMBER_STATUS",
                ctx={"status": data.status},
            )
        # 状态机跃迁保护（对齐维度主体 update_dimension 语义）：
        # - 已废弃成员为终态，拒绝任何更新（防止静默复活/篡改）
        # - 已发布成员禁止变更父级（层级是下游消费/血缘的权威来源，须废弃重建）
        if member.status == DimensionStatus.DEPRECATED.value:
            raise UnisenseError(
                f"已废弃成员不可更新: {dim_code}/{member_code}",
                error_code="INVALID_STATE",
            )
        if (
            data.parent_code is not None
            and member.status == DimensionStatus.PUBLISHED.value
            and (data.parent_code or None) != member.parent_code
        ):
            raise UnisenseError(
                f"已发布成员不可变更父级，请先废弃再重建层级: {dim_code}/{member_code}",
                error_code="INVALID_STATE",
            )
        if data.member_name is not None:
            member.member_name = data.member_name
        if data.attributes is not None:
            member.attributes = data.attributes

        # 父级变更（含置根）：重新推导 path（path 为服务端派生字段，不接受客户端直传）
        if data.parent_code is not None:
            new_parent_code = data.parent_code or None  # "" → 置根
            if new_parent_code != member.parent_code:
                await self._validate_reparent(member, new_parent_code, dim_code)

        if member.path and member.path.count("/") >= _MAX_MEMBER_DEPTH:
            raise ConflictError(
                f"维度成员层级超过上限（{_MAX_MEMBER_DEPTH} 层）",
                error_code="MEMBER_DEPTH_EXCEEDED",
            )
        await self._repo.commit()
        return member

    async def publish_member(self, dim_code: str, member_code: str) -> DimensionMember:
        """发布维度成员（DRAFT → PUBLISHED），对齐维度主体状态机。

        仅 DRAFT 可发布；DEPRECATED 为终态不可复活，PUBLISHED 幂等放行。
        """
        await self._require_active(dim_code)
        member = await self._repo.get_member(dim_code, member_code)
        if member is None:
            raise NotFoundError(
                f"维度成员不存在: {dim_code}/{member_code}",
                ctx={"member_code": member_code},
            )
        if member.status == DimensionStatus.DEPRECATED.value:
            raise UnisenseError(
                f"已废弃成员不可发布: {dim_code}/{member_code}",
                error_code="INVALID_STATE",
            )
        if member.status == DimensionStatus.PUBLISHED.value:
            return member
        # 父级状态保护（层级一致性）：父级已废弃时禁止发布子级——否则树形中
        # 出现"废弃父级下的已发布子级"，层级消费语义矛盾（对称于 deprecate_member 的子成员保护）。
        # 父级 DRAFT/PUBLISHED 均可（父子可各自发布），仅废弃父级拦截。
        if member.parent_code:
            siblings = await self._repo.list_members(dim_code)
            parent = next(
                (m for m in siblings if m.member_code == member.parent_code), None
            )
            if (
                parent is not None
                and parent.status == DimensionStatus.DEPRECATED.value
            ):
                raise UnisenseError(
                    f"父成员已废弃，无法发布子级: {dim_code}/{member_code}；请先处理父级层级",
                    error_code="INVALID_STATE",
                )
        member.status = DimensionStatus.PUBLISHED.value
        await self._repo.commit()
        return member

    async def deprecate_member(self, dim_code: str, member_code: str) -> DimensionMember:
        """废弃维度成员（PUBLISHED/DRAFT → DEPRECATED），对齐维度主体状态机。

        废弃保护：存在子成员时禁止废弃——否则子树成员父级悬空（层级权威来源失效）。
        """
        await self._require_active(dim_code)
        member = await self._repo.get_member(dim_code, member_code)
        if member is None:
            raise NotFoundError(
                f"维度成员不存在: {dim_code}/{member_code}",
                ctx={"member_code": member_code},
            )
        if member.status == DimensionStatus.DEPRECATED.value:
            return member
        children = await self._repo.list_members(dim_code)
        has_children = any(m.parent_code == member_code for m in children)
        if has_children:
            raise BusinessError(
                f"成员 {member_code} 存在子成员，无法废弃；请先废弃或迁移子成员",
                error_code="MEMBER_HAS_CHILDREN",
            )
        # 绑定引用保护（跨服务一致性，对称于 deprecate_dimension）：
        # 成员被指标绑定为默认值时禁止废弃——否则指标绑定 default_member 悬空
        bound = await self._repo.count_bindings_by_default_member(dim_code, member_code)
        if bound > 0:
            raise BusinessError(
                f"成员 {member_code} 正被 {bound} 个指标绑定为默认值，无法废弃；请先解绑相关指标",
                error_code="MEMBER_BOUND_BY_METRICS",
            )
        member.status = DimensionStatus.DEPRECATED.value
        await self._repo.commit()
        return member

    async def _validate_reparent(
        self,
        member: DimensionMember,
        new_parent_code: str | None,
        dim_code: str,
    ) -> None:
        """校验并执行父级变更：存在性/自引用/环防护 + path 自动重算。"""
        if new_parent_code == member.member_code:
            raise ConflictError(
                "维度成员不能以自身为父级",
                error_code="SELF_PARENT",
                ctx={"member_code": member.member_code},
            )
        if new_parent_code is None:
            member.parent_code = None
            member.path = self._resolve_member_path(None, member.member_code)
            # 置根后级联重算全部后代 path（父级为唯一事实源，子树 path 前缀须同步）
            members = await self._repo.list_members(dim_code)
            self._cascade_repath(member, members)
            return
        members = await self._repo.list_members(dim_code)
        parent = next((m for m in members if m.member_code == new_parent_code), None)
        if parent is None:
            raise NotFoundError(
                f"父成员不存在: {dim_code}/{new_parent_code}",
                ctx={"parent_code": new_parent_code},
            )
        # 环防护：沿新父向上追溯，若回到当前成员即成环（禁止移动到自身后代之下）
        cursor: DimensionMember | None = parent
        visited: set[str] = set()
        while cursor is not None:
            if cursor.member_code == member.member_code:
                raise ConflictError(
                    "维度成员不能移动到自己后代之下（将形成环）",
                    error_code="CYCLE_PARENT",
                    ctx={"member_code": member.member_code},
                )
            if cursor.member_code in visited:
                break
            visited.add(cursor.member_code)
            cursor = next(
                (m for m in members if m.member_code == cursor.parent_code), None
            )
        member.parent_code = new_parent_code
        member.path = self._resolve_member_path(parent, member.member_code)
        # 改父后级联重算全部后代 path（parent_code 为唯一事实源，子树 path 前缀须同步）
        self._cascade_repath(member, members)

    def _cascade_repath(
        self, moved: DimensionMember, members: list[DimensionMember]
    ) -> None:
        """移动父级后级联重算该成员全部后代的 path。

        path 是服务端派生的层级权威来源，须始终与 parent_code 链一致；
        只重算被移动成员自身会导致后代 path 前缀与父级断裂（前端树形错乱、
        血缘/消费的层级引用失效）。BFS 沿 parent_code 树传播新 path。
        """
        by_parent: dict[str, list[DimensionMember]] = {}
        for m in members:
            if m.parent_code and m.member_code != moved.member_code:
                by_parent.setdefault(m.parent_code, []).append(m)
        stack: list[DimensionMember] = [moved]
        while stack:
            cur = stack.pop()
            for child in by_parent.get(cur.member_code, []):
                child.path = f"{cur.path}/{child.member_code}"
                stack.append(child)

    async def delete_member(self, dim_code: str, member_code: str) -> list[DimensionMember]:
        """删除维度成员（工业级语义：级联删除其全部后代，保留孤儿引用不落库）。

        实现说明：
        - 成员表无 deleted_at 列，采用物理删除（对齐 create/update 无软删约定）。
        - 删除父级连带整个子树：先按 parent_code 建子节点索引，再自顶向下收集
          后代一次性删除，避免深递归。
        """
        member = await self._repo.get_member(dim_code, member_code)
        if member is None:
            raise NotFoundError(
                f"维度成员不存在: {dim_code}/{member_code}",
                ctx={"member_code": member_code},
            )
        members = await self._repo.list_members(dim_code)
        children: dict[str, list[DimensionMember]] = {}
        for m in members:
            if m.parent_code:
                children.setdefault(m.parent_code, []).append(m)
        # BFS 收集子树（member 自身 + 所有后代），防止 parent_code 成环导致死循环
        stack: list[DimensionMember] = [member]
        to_delete: list[DimensionMember] = []
        seen: set[str] = set()
        while stack:
            cur = stack.pop()
            if cur.member_code in seen:
                continue
            seen.add(cur.member_code)
            to_delete.append(cur)
            stack.extend(children.get(cur.member_code, []))
        # 绑定引用保护（跨服务一致性，对称于 deprecate_member 的 MEMBER_BOUND_BY_METRICS）：
        # 被指标绑定为默认值的成员（含子树内任一成员）禁止物理删除——否则指标绑定
        # default_member 悬空。逐成员计数（子树规模通常小，非批量场景无需聚合优化）。
        for doomed in to_delete:
            bound = await self._repo.count_bindings_by_default_member(
                dim_code, doomed.member_code
            )
            if bound > 0:
                raise BusinessError(
                    f"成员 {doomed.member_code} 正被 {bound} 个指标绑定为默认值，无法删除；"
                    f"请先解绑相关指标",
                    error_code="MEMBER_BOUND_BY_METRICS",
                )
        await self._repo.delete_members(to_delete)
        return to_delete

    async def list_members(self, dim_code: str) -> list[DimensionMember]:
        await self._require(dim_code)
        return await self._repo.list_members(dim_code)

    async def publish_all_members(self, dim_code: str) -> dict[str, int]:
        """批量发布维度全部 DRAFT 成员（从表导入工作流的闭环）。

        一次性将 DRAFT 成员置 PUBLISHED（DEPRECATED 终态跳过、PUBLISHED 幂等），
        返回 ``{"published": n, "skipped": m}``。
        """
        await self._require_active(dim_code)
        await self._require(dim_code)
        members = await self._repo.list_members(dim_code)
        published = 0
        skipped = 0
        for m in members:
            if m.status == DimensionStatus.DRAFT.value:
                m.status = DimensionStatus.PUBLISHED.value
                published += 1
            else:
                skipped += 1
        if published:
            await self._repo.commit()
        return {"published": published, "skipped": skipped}

    async def create_mapping(
        self, data: DimensionMappingCreate, actor_id: int | None = None
    ) -> DimensionMapping:
        # 源/目标维度存在性校验（防孤儿映射落到库）
        if not await self._repo.get_dimension(data.source_dim_code):
            raise NotFoundError(
                f"源维度不存在: {data.source_dim_code}",
                ctx={"source_dim_code": data.source_dim_code},
            )
        if not await self._repo.get_dimension(data.target_dim_code):
            raise NotFoundError(
                f"目标维度不存在: {data.target_dim_code}",
                ctx={"target_dim_code": data.target_dim_code},
            )
        # 自映射防护（防自环）
        if data.source_dim_code == data.target_dim_code:
            raise ConflictError(
                "维度不能映射到自身",
                error_code="SELF_MAPPING",
                ctx={"dim_code": data.source_dim_code},
            )
        # mapping_type enum 显式校验（非法值 → 4xx，而非 DB Enum 500）
        if data.mapping_type not in _VALID_MAPPING_TYPES:
            raise ValidationError(
                f"未知维度映射类型: {data.mapping_type}",
                error_code="INVALID_MAPPING_TYPE",
                ctx={"mapping_type": data.mapping_type},
            )
        mapping = DimensionMapping(
            source_dim_code=data.source_dim_code,
            target_dim_code=data.target_dim_code,
            mapping_type=MappingType(data.mapping_type).value,
            expression=data.expression,
            # PLAT-2: 认证身份优先，client 传入的 created_by 仅作降级
            created_by=actor_id if actor_id is not None else data.created_by,
        )
        return await self._repo.save_mapping(mapping)

    async def list_mappings(
        self, source_dim_code: str | None, *, page: int = 1, page_size: int = 20
    ) -> tuple[list[DimensionMapping], int]:
        """分页列出维度映射（P10 服务端分页，对齐主表分页模式）。"""
        limit = min(max(page_size, 1), 200)
        offset = (max(page, 1) - 1) * limit
        return await self._repo.list_mappings(source_dim_code, limit=limit, offset=offset)

    async def get_mapping(self, mapping_id: int) -> DimensionMapping | None:
        return await self._repo.get_mapping(mapping_id)

    async def update_mapping(
        self, mapping_id: int, data: DimensionMappingUpdate
    ) -> DimensionMapping:
        mapping = await self._repo.get_mapping(mapping_id)
        if mapping is None:
            raise NotFoundError(f"维度映射不存在: {mapping_id}")
        if data.mapping_type is not None:
            # 枚举显式校验（非法值 → 4xx，而非 DB Enum 500）
            if data.mapping_type not in _VALID_MAPPING_TYPES:
                raise ValidationError(
                    f"未知维度映射类型: {data.mapping_type}",
                    error_code="INVALID_MAPPING_TYPE",
                    ctx={"mapping_type": data.mapping_type},
                )
            mapping.mapping_type = MappingType(data.mapping_type).value
        if data.expression is not None:
            mapping.expression = data.expression
        await self._repo.commit()
        return mapping

    async def delete_mapping(self, mapping_id: int) -> None:
        mapping = await self._repo.get_mapping(mapping_id)
        if mapping is None:
            raise NotFoundError(f"维度映射不存在: {mapping_id}")
        await self._repo.delete_mapping(mapping)

    async def bind_metric_dimension(
        self, data: MetricDimensionBind, actor_id: int | None = None
    ) -> MetricDimension:
        await self._require(data.dim_code)
        # 指标存在性校验（跨服务一致性，对齐 default_member 已校验存在性）：
        # metric_id 须指向未软删的指标，防孤儿绑定——此前裸 BigInteger 无外键、
        # 不校验则绑定到不存在/已作废指标仍成功（维度详情显示悬空绑定）。
        metric = (
            await self._session.execute(
                select(Metric).where(
                    Metric.id == data.metric_id, Metric.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if metric is None:
            raise NotFoundError(
                f"指标不存在或已删除: {data.metric_id}",
                ctx={"metric_id": data.metric_id},
            )
        # role enum 显式校验（非法值 → 4xx，而非 DB Enum 500）
        if data.role not in _VALID_ROLES:
            raise ValidationError(
                f"未知关联角色: {data.role}",
                error_code="INVALID_ROLE",
                ctx={"role": data.role},
            )
        # 默认成员须为维度内已存在成员（防孤儿引用）
        if data.default_member is not None and not await self._repo.get_member(
            data.dim_code, data.default_member
        ):
            raise NotFoundError(
                f"默认成员不存在: {data.dim_code}/{data.default_member}",
                ctx={"default_member": data.default_member},
            )
        # 默认成员状态校验（跨服务一致性）：仅 PUBLISHED 可作默认值——
        # DRAFT 未发布不可被引用、DEPRECATED 已废弃不再使用（对齐维度主体
        # "未发布/已废弃不可消费"语义；存量绑定不受影响，仅 bind 新增时校验）
        if data.default_member is not None:
            dm = await self._repo.get_member(data.dim_code, data.default_member)
            if dm is not None and dm.status != DimensionStatus.PUBLISHED.value:
                raise ConflictError(
                    f"默认成员状态非法（须已发布，当前 {dm.status}）: "
                    f"{data.dim_code}/{data.default_member}",
                    error_code="DEFAULT_MEMBER_NOT_PUBLISHED",
                )
        binding = MetricDimension(
            metric_id=data.metric_id,
            dim_code=data.dim_code,
            role=MetricDimensionRole(data.role).value,
            default_member=data.default_member,
        )
        saved = await self._repo.save_metric_dimension(binding)
        # 单向打通：绑定成功后回写指标声明维度（definition_json.dimensions），
        # 使「维度管理-绑定指标」对消费链路真正生效（此前 metric_dimension 是信息孤岛，
        # 绑定只对绑定表生效，消费查询实际校验的是 definition_json.dimensions）。
        metric = await self._sync_dimension_to_metric(data.metric_id, data.dim_code, actor_id)
        # 跨服务一致：即时注册血缘 USES_DIMENSION 边（对称于 unbind 的即时移除）。
        # 血缘注册是追加语义（指标创建/编辑/发布时全量重注册），bind 若不同步建边，
        # 新绑定维度的指标血缘图要等下次编辑/发布才出现「指标↔维度」边——不对称。
        if metric is not None:
            try:
                from app.services.lineage.parser import node_dimension
                from app.services.lineage.repository import LineageRepository

                await LineageRepository(self._session).upsert_metric_dimension_edge(
                    metric_code=metric.metric_code,
                    dim_node=node_dimension(data.dim_code),
                    change_reason="metric_dimension_binding",
                )
            except Exception:  # noqa: BLE001 - 血缘注册失败不阻断绑定主流程
                logger.warning(
                    "bind_metric_dimension_lineage_register_failed",
                    metric_id=data.metric_id,
                    dim_code=data.dim_code,
                )
        return saved

    async def list_metric_dimensions(self, metric_id: int) -> list[MetricDimension]:
        return await self._repo.list_metric_dimensions(metric_id)

    async def unbind_metric_dimension(self, metric_id: int, dim_code: str) -> None:
        """解除指标-维度绑定（与 bind 对称）：删除绑定记录 + 从指标声明维度移除。

        绑定是单向关系，解绑是撤销误绑/改绑的唯一路径；解绑后消费链路
        （definition_json.dimensions）同步移除该维度，避免声明维度与绑定表不一致。

        P1-8 来源保护：仅移除**由 bind 追加**（``_bound_dimensions`` 有来源标记）的
        声明维度；用户**手工声明**的维度（无标记 / 存量数据）解绑时**保留**——
        手工声明是用户口径的一部分，bind 幂等跳过时曾未追加来源标记，unbind 不应
        静默抹掉用户原声明（口径丢失）。
        """
        binding = await self._repo.delete_metric_dimension(metric_id, dim_code)
        if binding is None:
            raise NotFoundError(f"绑定关系不存在: metric={metric_id}/dim={dim_code}")
        # 反向同步：仅移除来源标记的维度；手工声明保留（P1-8，防口径丢失）
        stmt = select(Metric).where(Metric.id == metric_id)
        metric = (await self._session.execute(stmt)).scalar_one_or_none()
        if metric is not None:
            defn = dict(metric.definition_json or {})
            bound_dims = list(defn.get("_bound_dimensions") or [])
            if dim_code in bound_dims:
                dims = [d for d in (defn.get("dimensions") or []) if d != dim_code]
                defn["dimensions"] = dims
                defn["_bound_dimensions"] = [
                    d for d in bound_dims if d != dim_code
                ]
                metric.definition_json = defn
                if metric.status not in ("DRAFT", "EXPERIMENTAL"):
                    logger.warning(
                        "unbind_metric_dimension_rewrites_published_metric",
                        metric_id=metric_id,
                        dim_code=dim_code,
                    )
            else:
                # 无来源标记（手工声明/存量）→ 保留声明，避免误删用户口径
                logger.info(
                    "unbind_metric_dimension_keep_manual_declaration",
                    metric_id=metric_id,
                    dim_code=dim_code,
                )
        # 跨服务一致：即时移除血缘 USES_DIMENSION 边（register 是追加语义，
        # 解绑后不删除则血缘残留"指标仍使用已解绑维度"的陈旧边）。best-effort 不阻断解绑。
        if metric is not None:
            try:
                from app.services.lineage.parser import node_dimension, node_metric
                from app.services.lineage.repository import LineageRepository

                await LineageRepository(self._session).soft_delete_edge_by_key(
                    node_metric(metric.metric_code),
                    node_dimension(dim_code),
                    "USES_DIMENSION",
                )
            except Exception:  # noqa: BLE001 - 血缘清理失败不阻断解绑主流程
                logger.warning(
                    "unbind_metric_dimension_lineage_cleanup_failed",
                    metric_id=metric_id,
                    dim_code=dim_code,
                )
        await self._session.flush()

    async def list_dimension_metrics(
        self, dim_code: str
    ) -> list[tuple[MetricDimension, Metric]]:
        """按维度查绑定指标（join Metric 补 metric_code/name/status，治理追溯）。"""
        await self._require(dim_code)
        return await self._repo.list_dimension_metrics(dim_code)

    async def submit_reconciliation(self, data: ReconciliationSubmit) -> Reconciliation:
        # dim_code 可选；若提供则须为已存在维度（防孤儿对账记录）
        if data.dim_code and not await self._repo.get_dimension(data.dim_code):
            raise NotFoundError(
                f"维度不存在: {data.dim_code}",
                ctx={"dim_code": data.dim_code},
            )
        # 指标存在性校验（跨服务一致性，对齐 bind_metric_dimension）：metric_id
        # 裸 BigInteger 无外键，不校验则对账引用不存在/已软删指标形成孤儿记录。
        metric = (
            await self._session.execute(
                select(Metric).where(
                    Metric.id == data.metric_id, Metric.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if metric is None:
            raise NotFoundError(
                f"指标不存在或已删除: {data.metric_id}",
                ctx={"metric_id": data.metric_id},
            )
        rec = Reconciliation(
            metric_id=data.metric_id,
            dim_code=data.dim_code,
            expected_expr=data.expected_expr,
            actual_expr=data.actual_expr,
            diff_summary=data.diff_summary,
            status=ReconciliationStatus.PENDING.value,
        )
        return await self._repo.save_reconciliation(rec)

    async def list_reconciliations(
        self, status: str | None, *, page: int = 1, page_size: int = 20
    ) -> tuple[list[tuple[Reconciliation, Metric | None]], int]:
        """分页列出对账记录（P10 服务端分页，防治理记录增长导致的全量拉取）。"""
        limit = min(max(page_size, 1), 200)
        offset = (max(page, 1) - 1) * limit
        return await self._repo.list_reconciliations(
            status, limit=limit, offset=offset
        )

    async def review_reconciliation(
        self, rec_id: int, data: ReconciliationReview, reviewer_id: int | None = None
    ) -> Reconciliation:
        rec = await self._repo.get_reconciliation(rec_id)
        if rec is None:
            raise NotFoundError(f"对账记录不存在: {rec_id}")
        if data.decision not in (
            ReconciliationStatus.APPROVED.value,
            ReconciliationStatus.REJECTED.value,
        ):
            raise UnisenseError(f"未知复核结论: {data.decision}", error_code="INVALID_DECISION")
        rec.status = data.decision
        # PLAT-2: 以服务端认证身份 reviewer_id 落库，忽略 client 伪造的 reviewer_id
        rec.reviewed_by = reviewer_id if reviewer_id is not None else data.reviewer_id
        rec.reviewed_at = datetime.now(UTC)
        await self._repo.commit()
        return rec

    async def _sync_dimension_to_metric(
        self, metric_id: int, dim_code: str, actor_id: int | None = None
    ) -> Metric | None:
        """回写指标声明维度：让绑定关系对消费链路生效（打通信息孤岛）。

        消费查询（consume 服务）真正校验的是 ``metric.definition_json.dimensions``，
        ``metric_dimension`` 绑定表此前只有本服务读写，对消费无影响。此处绑定成功后
        把 ``dim_code`` 追加进该字段，使绑定即生效。

        - 幂等：``dim_code`` 已存在于声明维度则跳过，不重复追加。
        - DRAFT / EXPERIMENTAL 指标：直接回写，无版本影响。
        - PUBLISHED 等已发布口径（P1-9）：属口径变更，**走 PENDING_VERSION 确认期**，
          不再静默改写主表口径——否则消费方维度白名单/血缘边无声变化，绕过 14 天确认。
          改为：创建 PENDING 版本快照（含新维度）+ 确认记录，live 口径保持原样直至转正。
        - 来源标记（P1-8）：bind 追加的维度记录进 ``_bound_dimensions``，供 unbind
          区分「绑定来源」与「用户手工声明」——解绑只删绑定来源，不抹手工声明。

        Returns:
            回写后的 Metric（供 bind 复用 metric_code 注册血缘边）；
            指标不存在时返回 None。
        """
        stmt = select(Metric).where(Metric.id == metric_id)
        result = await self._session.execute(stmt)
        metric = result.scalar_one_or_none()
        if metric is None:
            # 绑定表已存指标引用，但指标查不到：仅告警跳过回写，不阻断绑定本身
            logger.warning("bind_metric_dimension_metric_missing", metric_id=metric_id)
            return None
        defn = dict(metric.definition_json or {})
        dims = list(defn.get("dimensions") or [])
        if dim_code in dims:
            return metric  # 幂等：已在声明维度中，跳过（指标存在，返回供血缘注册）
        dims.append(dim_code)
        defn["dimensions"] = dims
        # 来源标记：本次由 bind 追加 → 记入 _bound_dimensions，unbind 据此不误删手工声明
        bound_dims = list(defn.get("_bound_dimensions") or [])
        if dim_code not in bound_dims:
            bound_dims.append(dim_code)
        defn["_bound_dimensions"] = bound_dims
        # 已发布口径变更（P1-9）：经版本审批流，不静默改写 live 口径
        if metric.status not in ("DRAFT", "EXPERIMENTAL"):
            await self._create_bind_pending_version(metric, defn, dim_code, actor_id)
            return metric
        metric.definition_json = defn
        await self._repo.commit()
        return metric

    async def _create_bind_pending_version(
        self,
        metric: Metric,
        new_def: dict[str, Any],
        dim_code: str,
        actor_id: int | None,
    ) -> None:
        """已发布指标绑定新维度：创建 PENDING_VERSION 确认期快照（P1-9）。

        不改动 live 口径；维度绑定记录（metric_dimension 表）已落库，
        待版本确认期结束后由语义服务转正时回写 ``definition_json.dimensions``。
        消费方在确认期内不会看到该维度（white-list 仍以旧口径为准），符合治理语义。
        """
        from app.services.semantic.pending_version_manager import PendingVersionManager
        from app.services.semantic.repository import MetricRepository

        metric_repo = MetricRepository(self._session)
        # 防叠加：已存在待确认变更时不再叠 pending，提示用户先确认/等超时
        if await metric_repo.has_pending_version(metric.id):
            raise ConflictError(
                f"指标 {metric.metric_code} 存在待确认的口径变更，"
                "请先完成确认或等待超时后再绑定新维度",
                error_code="METRIC_PENDING_VERSION_EXISTS",
            )
        new_version_num = metric.version + 1
        version = MetricVersion(
            metric_id=metric.id,
            version=new_version_num,
            change_type="UPDATE",
            definition_json=new_def,
            diff_json={"dimensions": {"added": [dim_code]}},
            status="PENDING_CONFIRMATION",
            change_reason=f"绑定维度 {dim_code}（待确认）",
            created_by=actor_id if actor_id is not None else metric.owner_id,
        )
        await metric_repo.create_version(version)
        consumer_ids = [metric.owner_id]
        if metric.backup_owner_id is not None:
            consumer_ids.append(metric.backup_owner_id)
        pvm = PendingVersionManager(self._session)
        await pvm.create_pending(metric, version, consumer_ids)
        logger.info(
            "bind_dimension_pending_version_created",
            metric_id=metric.id,
            metric_code=metric.metric_code,
            version=new_version_num,
            dim_code=dim_code,
            consumers=consumer_ids,
        )

    async def _rename_dimension_in_metric_definitions(
        self, old_code: str, new_code: str
    ) -> None:
        """维度改编码后，同步更新指标口径声明里的维度编码（旧→新）。

        消费校验与血缘 USES_DIMENSION 边的权威来源是 ``metric.definition_json.dimensions``
        （bind 时由 ``_sync_dimension_to_metric`` 写入）。维度改编码只更新了绑定表/成员/映射，
        若不同步指标口径，已发布指标的消费维度白名单与血缘边将指向旧码 → 悬空。

        扫描两条来源（P1-7 加固）：绑定表 + ``definition_json.dimensions`` 手工声明
        （手工声明未绑定的维度此前被遗漏 → 改码后消费 FORBIDDEN_DIMENSION、血缘边悬挂）。
        两路按 metric_id 去重，全量替换旧码为新码。
        """
        bound = await self._repo.list_metrics_by_dimension(old_code)
        declared = await self._repo.list_metrics_declaring_dimension(old_code)
        seen: dict[int, Metric] = {}
        for m in [*bound, *declared]:
            seen[m.id] = m
        metrics = list(seen.values())
        for metric in metrics:
            defn = dict(metric.definition_json or {})
            dims = list(defn.get("dimensions") or [])
            if old_code not in dims:
                continue
            defn["dimensions"] = [new_code if d == old_code else d for d in dims]
            metric.definition_json = defn
        if metrics:
            await self._repo.commit()

    async def _require(self, dim_code: str) -> Dimension:
        """加载维度并校验可操作：不存在或已软删（回收站）均拒绝。

        已软删记录除「恢复」外不可变——防止回收站中的维度被更新/提交/通过/
        发布/废弃/重新启用等操作复活成矛盾态。恢复用 ``_repo.get_dimension``
        直取，不走本守卫。
        """
        dim = await self._repo.get_dimension(dim_code)
        if dim is None:
            raise NotFoundError(f"维度不存在: {dim_code}")
        if getattr(dim, "deleted_at", None) is not None:
            raise UnisenseError(
                f"已删除的维度不可执行该操作（{dim_code}），请先在回收站恢复",
                error_code="INVALID_STATE",
            )
        return dim

    async def _require_active(self, dim_code: str) -> Dimension:
        """校验维度存在且未废弃——废弃维度（终态）下禁止成员/映射写操作。

        与成员 DEPRECATED 终态禁更新的语义一致：维度主体已下线，其成员字典
        不应再被新建/发布（否则"废弃维度下仍活跃成员字典"状态矛盾）。
        """
        dim = await self._require(dim_code)
        if dim.status == DimensionStatus.DEPRECATED.value:
            raise UnisenseError(
                f"维度已废弃，不可再操作其成员: {dim_code}",
                error_code="INVALID_STATE",
            )
        return dim

    async def preview_column_values(
        self, source_id: str, table: str, column: str, limit: int = 200
    ) -> dict[str, Any]:
        """从已注册数据源的指定表列拉取去重枚举值（维度值自动获取）。

        用数据源存储的加密连接配置构建采集器，执行
        ``SELECT DISTINCT `col` FROM `table` LIMIT n`` 并返回去重值。

        Args:
            source_id: 数据源 ID（须已注册）。
            table: 表名（可带库前缀，如 ``dwd.sales``）。
            column: 列名。
            limit: 去重值上限（1-1000）。

        Returns:
            ``{"values": [...], "total": n, "truncated": bool}``。

        Raises:
            NotFoundError: 数据源不存在。
            ExternalDependencyError: 连接/查询失败（源库不可达）。
        """
        from sqlalchemy import select as sa_select

        from app.models.data_source import DataSource
        from app.services.collector.connectors import registry

        src = (
            await self._db.execute(sa_select(DataSource).where(DataSource.source_id == source_id))
        ).scalar_one_or_none()
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        # 校验表名/列名为合法标识符（防注入：只允许字母数字下划线点，且逐段非数字开头）
        _id_re = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*$")
        if not _id_re.match(table) or not _id_re.match(column):
            raise ValidationError("表名/列名不合法（仅允许字母数字下划线，可用点分隔库前缀）")
        # 反引号包裹标识符（MySQL 保留字安全），列名在 SELECT 里限定，避免拼接到表名
        safe_table = ".".join(f"`{p}`" for p in table.split("."))
        safe_col = f"`{column}`"
        sql = f"SELECT DISTINCT {safe_col} FROM {safe_table} LIMIT {int(limit)}"

        collector = registry.build(src.source_type, src.connection_config)
        try:
            rows = await collector.query(sql)
        except ExternalDependencyError:
            raise  # 外部依赖错误（连接/查询超时）已语义化，交由 API 层映射
        except Exception as exc:
            # DBAPIError/OperationalError 等驱动异常带必需构造参数，不能 type(exc)(msg) 重抛
            raise UnisenseError(f"拉取维度枚举值失败: {exc}") from exc
        finally:
            await collector.dispose()

        col_key = column.lower()
        values = [
            str(r.get(col_key, "")).strip()
            for r in rows
            if r.get(col_key) is not None
        ]
        return {
            "values": values,
            "total": len(values),
            "truncated": len(values) >= limit,
        }

    # ------------------------------------------------------------ 引用型维度

    @staticmethod
    def _validate_identifier(name: str) -> None:
        """校验表名/列名为合法标识符（防注入，逐段非数字开头，可用点分隔库前缀）。"""
        _id_re = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*$")
        if not _id_re.match(name):
            raise ValidationError("表名/列名不合法（仅允许字母数字下划线，可用点分隔库前缀）")

    async def _load_source(self, source_id: str):
        """加载已注册数据源（不存在抛 404），供引用型拉取/计数复用。"""
        from app.models.data_source import DataSource

        src = (
            await self._db.execute(
                select(DataSource).where(DataSource.source_id == source_id)
            )
        ).scalar_one_or_none()
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        return src

    async def _fetch_column_values(
        self, source_id: str, table: str, column: str
    ) -> list[str]:
        """从源表列拉取去重非空值全量（keyset 分页，每批 5000，适合百万级大表）。

        首查 ``SELECT DISTINCT col FROM tbl``，之后以 ``WHERE col > :last``
        翻页；NULL 行被比较语义排除，空值率由 ``_count_null_stats`` 单独统计。
        """
        src = await self._load_source(source_id)
        self._validate_identifier(table)
        self._validate_identifier(column)
        safe_table = ".".join(f"`{p}`" for p in table.split("."))
        safe_col = f"`{column}`"
        from app.services.collector.connectors import registry

        collector = registry.build(src.source_type, src.connection_config)
        try:
            values: list[str] = []
            last: str | None = None
            while True:
                where = f"WHERE {safe_col} > :last" if last is not None else ""
                sql = (
                    f"SELECT DISTINCT {safe_col} AS v FROM {safe_table} "
                    f"{where} ORDER BY {safe_col} LIMIT 5000"
                )
                params: dict[str, Any] | None = {"last": last} if last is not None else None
                rows = await collector.query(sql, params)
                if not rows:
                    break
                batch = [
                    str(r["v"]).strip()
                    for r in rows
                    if r.get("v") is not None and str(r["v"]).strip() != ""
                ]
                if not batch:
                    break
                values.extend(batch)
                last = batch[-1]
                if len(rows) < 5000:
                    break
            return list(dict.fromkeys(values))
        except ExternalDependencyError:
            raise
        except Exception as exc:
            raise UnisenseError(f"拉取维度值快照失败: {exc}") from exc
        finally:
            await collector.dispose()

    async def _count_null_stats(
        self, source_id: str, table: str, column: str
    ) -> dict[str, int]:
        """统计源表列的空值数（``COUNT(*) - COUNT(col)``，一次查询两列）。"""
        src = await self._load_source(source_id)
        self._validate_identifier(table)
        self._validate_identifier(column)
        safe_table = ".".join(f"`{p}`" for p in table.split("."))
        safe_col = f"`{column}`"
        sql = f"SELECT COUNT(*) AS total, COUNT({safe_col}) AS non_null FROM {safe_table}"
        from app.services.collector.connectors import registry

        collector = registry.build(src.source_type, src.connection_config)
        try:
            rows = await collector.query(sql)
        finally:
            await collector.dispose()
        row = rows[0] if rows else {}
        total = int(row.get("total") or 0)
        non_null = int(row.get("non_null") or 0)
        return {"total": total, "null_count": max(total - non_null, 0)}

    async def bind_dimension_reference(
        self, dim_code: str, data: DimensionReferenceBind
    ) -> Dimension:
        """绑定引用型值来源（维度值 = 源表列快照，不再维护 member 表）。"""
        dim = await self._require_active(dim_code)
        await self._load_source(data.source_id)
        self._validate_identifier(data.table)
        self._validate_identifier(data.column)
        dim.source_id = data.source_id
        dim.source_table = data.table
        dim.source_column = data.column
        dim.sync_mode = SyncMode.SNAPSHOT.value
        dim.refresh_interval_hours = data.refresh_interval_hours
        await self._db.commit()
        return dim

    async def refresh_dimension_snapshot(
        self, dim_code: str, *, trigger: str = "manual"
    ) -> dict[str, Any]:
        """刷新引用型维度值快照：拉全量 → 与上一批 diff → 写新批 + run 记录。

        - 新批次全部 ACTIVE；上一批存在、本批消失的行置 REMOVED（消失值检测）。
        - 保留最近 2 个批次（``_prune_old_snapshots``），更早批次删除。
        - 空值率 = ``COUNT(*) - COUNT(col)``，随 run 记录落库。
        """
        import time as _time

        dim = await self._require_active(dim_code)
        if dim.sync_mode != SyncMode.SNAPSHOT.value:
            raise ValidationError(
                f"维度未绑定引用型值来源: {dim_code}",
                error_code="NOT_SNAPSHOT_MODE",
            )
        if not dim.source_id or not dim.source_table or not dim.source_column:
            raise ValidationError(
                f"维度引用来源信息不完整: {dim_code}", error_code="REFERENCE_INCOMPLETE"
            )
        started = _time.monotonic()
        now = datetime.now(UTC).replace(microsecond=0)
        run = DimensionSnapshotRun(
            dim_code=dim_code, snapshot_at=now, status=SnapshotRunStatus.RUNNING.value
        )
        self._db.add(run)
        await self._db.flush()
        try:
            values = await self._fetch_column_values(
                dim.source_id, dim.source_table, dim.source_column
            )
            null_stats = await self._count_null_stats(
                dim.source_id, dim.source_table, dim.source_column
            )
            prev_rows = (
                (
                    await self._db.execute(
                        select(DimensionValueSnapshot).where(
                            DimensionValueSnapshot.dim_code == dim_code,
                            DimensionValueSnapshot.snapshot_at < now,
                            DimensionValueSnapshot.status
                            == SnapshotStatus.ACTIVE.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            prev_values = {r.value for r in prev_rows}
            curr_values = set(values)
            added = sorted(curr_values - prev_values)
            removed = sorted(prev_values - curr_values)
            if values:
                self._db.add_all(
                    [
                        DimensionValueSnapshot(
                            dim_code=dim_code,
                            source_id=dim.source_id,
                            source_table=dim.source_table,
                            source_column=dim.source_column,
                            value=v,
                            snapshot_at=now,
                            status=SnapshotStatus.ACTIVE.value,
                        )
                        for v in values
                    ]
                )
            if removed:
                removed_set = set(removed)
                for r in prev_rows:
                    if r.value in removed_set:
                        r.status = SnapshotStatus.REMOVED.value
            await self._prune_old_snapshots(dim_code, now)
            null_count = null_stats["null_count"]
            total_count = len(values)
            null_rate = (
                round(null_count / max(null_stats["total"], 1), 4)
                if null_stats["total"]
                else None
            )
            run.status = SnapshotRunStatus.SUCCESS.value
            run.total_count = total_count
            run.added_count = len(added)
            run.removed_count = len(removed)
            run.null_count = null_count
            run.null_rate = null_rate
            run.added_sample = added[:50]
            run.removed_sample = removed[:50]
            run.duration_ms = int((_time.monotonic() - started) * 1000)
            dim.last_snapshot_at = now
            await self._db.commit()
            return {
                "dim_code": dim_code,
                "snapshot_at": now.isoformat(),
                "total": total_count,
                "added": added,
                "removed": removed,
                "null_count": null_count,
                "null_rate": null_rate,
            }
        except Exception as exc:
            await self._db.rollback()
            self._db.add(run)
            run.status = SnapshotRunStatus.FAILED.value
            run.error_msg = str(exc)[:2000]
            run.duration_ms = int((_time.monotonic() - started) * 1000)
            await self._db.commit()
            raise

    async def _prune_old_snapshots(self, dim_code: str, now: datetime) -> None:
        """保留最近 2 个快照批次，删除更早批次（diff 只需上一批）。"""
        at_rows = (
            (
                await self._db.execute(
                    select(DimensionValueSnapshot.snapshot_at)
                    .where(DimensionValueSnapshot.dim_code == dim_code)
                    .distinct()
                    .order_by(DimensionValueSnapshot.snapshot_at.desc())
                )
            )
            .scalars()
            .all()
        )
        if len(at_rows) <= 2:
            return
        keep = set(at_rows[:2])
        old = await self._db.execute(
            select(DimensionValueSnapshot).where(
                DimensionValueSnapshot.dim_code == dim_code,
                DimensionValueSnapshot.snapshot_at.notin_(keep),
            )
        )
        for row in old.scalars():
            await self._db.delete(row)

    async def list_dimension_snapshots(
        self, dim_code: str, *, page: int = 1, page_size: int = 100
    ) -> tuple[list[DimensionValueSnapshot], int]:
        """分页列出引用型维度快照值（默认按批次倒序 + 值升序）。"""
        await self._require(dim_code)
        limit = min(max(page_size, 1), 500)
        offset = (max(page, 1) - 1) * limit
        total = (
            await self._db.execute(
                select(func.count())
                .select_from(DimensionValueSnapshot)
                .where(DimensionValueSnapshot.dim_code == dim_code)
            )
        ).scalar_one()
        rows = (
            (
                await self._db.execute(
                    select(DimensionValueSnapshot)
                    .where(DimensionValueSnapshot.dim_code == dim_code)
                    .order_by(
                        DimensionValueSnapshot.snapshot_at.desc(),
                        DimensionValueSnapshot.value.asc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), int(total)

    async def get_latest_snapshot_run(
        self, dim_code: str
    ) -> DimensionSnapshotRun | None:
        """最近一次快照刷新运行记录（无则 None）。"""
        await self._require(dim_code)
        return (
            (
                await self._db.execute(
                    select(DimensionSnapshotRun)
                    .where(DimensionSnapshotRun.dim_code == dim_code)
                    .order_by(DimensionSnapshotRun.snapshot_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    # ------------------------------------------------------------ 成员批量操作

    async def batch_publish_members(
        self, dim_code: str, member_codes: list[str]
    ) -> dict[str, Any]:
        """批量发布成员：逐条复用 publish_member 约束（父级废弃拦截、终态跳过）。

        返回 ``{"published": n, "skipped": n, "failed": [{code, reason}]}``。
        """
        await self._require_active(dim_code)
        published = 0
        skipped = 0
        failed: list[dict[str, Any]] = []
        for code in member_codes:
            try:
                member = await self._repo.get_member(dim_code, code)
                if member is None:
                    failed.append({"code": code, "reason": "成员不存在"})
                    continue
                if member.status == DimensionStatus.PUBLISHED.value:
                    skipped += 1
                    continue
                if member.status == DimensionStatus.DEPRECATED.value:
                    failed.append({"code": code, "reason": "已废弃成员不可发布"})
                    continue
                await self.publish_member(dim_code, code)
                published += 1
            except (BusinessError, UnisenseError, NotFoundError, ConflictError) as exc:
                failed.append({"code": code, "reason": str(exc)})
        return {"published": published, "skipped": skipped, "failed": failed}

    async def batch_deprecate_members(
        self, dim_code: str, member_codes: list[str]
    ) -> dict[str, Any]:
        """批量废弃成员：逐条复用 deprecate_member 约束（子成员/绑定引用保护）。"""
        await self._require_active(dim_code)
        deprecated = 0
        skipped = 0
        failed: list[dict[str, Any]] = []
        for code in member_codes:
            try:
                member = await self._repo.get_member(dim_code, code)
                if member is None:
                    failed.append({"code": code, "reason": "成员不存在"})
                    continue
                if member.status == DimensionStatus.DEPRECATED.value:
                    skipped += 1
                    continue
                await self.deprecate_member(dim_code, code)
                deprecated += 1
            except (BusinessError, UnisenseError, NotFoundError, ConflictError) as exc:
                failed.append({"code": code, "reason": str(exc)})
        return {"deprecated": deprecated, "skipped": skipped, "failed": failed}

    async def batch_delete_members(
        self, dim_code: str, member_codes: list[str]
    ) -> dict[str, Any]:
        """批量删除成员：先 BFS 收集全部子树并集（父+子去重），再做绑定保护，一次性删。

        ``delete_member`` 是级联子树删除；批量勾选父+子若不并集会重复删——本方法
        先按 parent_code 建索引，对每个选中成员收集其整棵子树并入集合，随后逐成员
        校验 default_member 引用保护（对称单条语义），最后一次性物理删除。
        """
        await self._require_active(dim_code)
        members = await self._repo.list_members(dim_code)
        children: dict[str, list[DimensionMember]] = {}
        for m in members:
            if m.parent_code:
                children.setdefault(m.parent_code, []).append(m)
        by_code = {m.member_code: m for m in members}
        to_delete: dict[str, DimensionMember] = {}
        failed: list[dict[str, Any]] = []
        for code in member_codes:
            member = by_code.get(code)
            if member is None:
                failed.append({"code": code, "reason": "成员不存在"})
                continue
            stack: list[DimensionMember] = [member]
            seen: set[str] = set()
            while stack:
                cur = stack.pop()
                if cur.member_code in seen:
                    continue
                seen.add(cur.member_code)
                to_delete[cur.member_code] = cur
                stack.extend(children.get(cur.member_code, []))
        for doomed in to_delete.values():
            bound = await self._repo.count_bindings_by_default_member(
                dim_code, doomed.member_code
            )
            if bound > 0:
                raise BusinessError(
                    f"成员 {doomed.member_code} 正被 {bound} 个指标绑定为默认值，无法删除；"
                    f"请先解绑相关指标",
                    error_code="MEMBER_BOUND_BY_METRICS",
                )
        await self._repo.delete_members(list(to_delete.values()))
        return {"deleted": len(to_delete), "failed": failed}

    # ------------------------------------------------------------ 值级映射

    async def create_mapping_value(
        self, mapping_id: int, data: MappingValueCreate, actor_id: int | None = None
    ) -> DimensionMappingValue:
        mapping = await self._repo.get_mapping(mapping_id)
        if mapping is None:
            raise NotFoundError(f"维度映射不存在: {mapping_id}")
        existing = (
            await self._db.execute(
                select(DimensionMappingValue).where(
                    DimensionMappingValue.mapping_id == mapping_id,
                    DimensionMappingValue.source_value == data.source_value,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ConflictError(
                f"源值已配置映射: {data.source_value}",
                error_code="MAPPING_VALUE_EXISTS",
            )
        mv = DimensionMappingValue(
            mapping_id=mapping_id,
            source_value=data.source_value,
            target_value=data.target_value,
            created_by=actor_id if actor_id is not None else 0,
        )
        self._db.add(mv)
        await self._db.commit()
        return mv

    async def list_mapping_values(
        self, mapping_id: int, *, page: int = 1, page_size: int = 100
    ) -> tuple[list[DimensionMappingValue], int]:
        mapping = await self._repo.get_mapping(mapping_id)
        if mapping is None:
            raise NotFoundError(f"维度映射不存在: {mapping_id}")
        limit = min(max(page_size, 1), 500)
        offset = (max(page, 1) - 1) * limit
        total = (
            await self._db.execute(
                select(func.count())
                .select_from(DimensionMappingValue)
                .where(DimensionMappingValue.mapping_id == mapping_id)
            )
        ).scalar_one()
        rows = (
            (
                await self._db.execute(
                    select(DimensionMappingValue)
                    .where(DimensionMappingValue.mapping_id == mapping_id)
                    .order_by(DimensionMappingValue.source_value.asc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), int(total)

    async def delete_mapping_value(self, value_id: int) -> None:
        mv = (
            await self._db.execute(
                select(DimensionMappingValue).where(
                    DimensionMappingValue.id == value_id
                )
            )
        ).scalar_one_or_none()
        if mv is None:
            raise NotFoundError(f"值级映射不存在: {value_id}")
        await self._db.delete(mv)
        await self._db.commit()

    async def _source_value_set(self, dim_code: str) -> set[str]:
        """源维度当前值集合：引用型取最新快照 ACTIVE 值，枚举型取成员编码。"""
        dim = await self._repo.get_dimension(dim_code)
        if dim is None:
            raise NotFoundError(f"维度不存在: {dim_code}")
        if dim.sync_mode == SyncMode.SNAPSHOT.value:
            latest = await self.get_latest_snapshot_run(dim_code)
            if latest is None:
                return set()
            rows = (
                (
                    await self._db.execute(
                        select(DimensionValueSnapshot.value).where(
                            DimensionValueSnapshot.dim_code == dim_code,
                            DimensionValueSnapshot.snapshot_at == latest.snapshot_at,
                            DimensionValueSnapshot.status == SnapshotStatus.ACTIVE.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return set(rows)
        members = await self._repo.list_members(dim_code)
        return {m.member_code for m in members}

    async def translate_value(
        self, source_dim_code: str, target_dim_code: str, value: str
    ) -> TranslateResult:
        """单值翻译：值级映射优先，未命中时 expression 仅原样返回（不执行）。"""
        mapping = (
            (
                await self._db.execute(
                    select(DimensionMapping)
                    .where(
                        DimensionMapping.source_dim_code == source_dim_code,
                        DimensionMapping.target_dim_code == target_dim_code,
                    )
                    .order_by(
                        DimensionMapping.mapping_type
                        == MappingType.EQUIVALENT.value
                    )
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if mapping is None:
            return TranslateResult(
                source_value=value,
                target_value=None,
                covered=False,
                source_dim_code=source_dim_code,
                target_dim_code=target_dim_code,
            )
        mv = (
            await self._db.execute(
                select(DimensionMappingValue).where(
                    DimensionMappingValue.mapping_id == mapping.id,
                    DimensionMappingValue.source_value == value,
                )
            )
        ).scalar_one_or_none()
        if mv is not None:
            return TranslateResult(
                source_value=value,
                target_value=mv.target_value,
                covered=True,
                source_dim_code=source_dim_code,
                target_dim_code=target_dim_code,
            )
        # expression 兜底：仅原样返回（值级映射未配置时不做机器翻译）
        return TranslateResult(
            source_value=value,
            target_value=value,
            covered=False,
            source_dim_code=source_dim_code,
            target_dim_code=target_dim_code,
        )

    async def translate_values(
        self, source_dim_code: str, target_dim_code: str, values: list[str]
    ) -> list[TranslateResult]:
        """批量翻译（供前端翻译预览，逐值走 translate_value）。"""
        return [
            await self.translate_value(source_dim_code, target_dim_code, v)
            for v in values
        ]

    async def mapping_coverage(self, mapping_id: int) -> MappingCoverageResponse:
        """值级映射覆盖率：源维度当前值集合中已配置逐值映射的统计与未映射清单。"""
        mapping = await self._repo.get_mapping(mapping_id)
        if mapping is None:
            raise NotFoundError(f"维度映射不存在: {mapping_id}")
        source_vals = await self._source_value_set(mapping.source_dim_code)
        configured = (
            (
                await self._db.execute(
                    select(DimensionMappingValue.source_value).where(
                        DimensionMappingValue.mapping_id == mapping_id
                    )
                )
            )
            .scalars()
            .all()
        )
        covered_set = set(configured)
        total = len(source_vals)
        covered = len(source_vals & covered_set)
        uncovered = sorted(source_vals - covered_set)[:50]
        return MappingCoverageResponse(
            mapping_id=mapping_id,
            total=total,
            covered=covered,
            uncovered=uncovered,
        )
