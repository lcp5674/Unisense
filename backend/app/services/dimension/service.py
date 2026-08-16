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

from sqlalchemy import select
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
    DimensionMember,
    DimensionStatus,
    DimensionType,
    MappingType,
    MetricDimension,
    MetricDimensionRole,
    Reconciliation,
    ReconciliationStatus,
)
from app.models.metric import Metric
from app.services.dimension.repository import DimensionRepository
from app.services.dimension.schemas import (
    DimensionCreate,
    DimensionMappingCreate,
    DimensionMappingUpdate,
    DimensionMemberCreate,
    DimensionMemberUpdate,
    DimensionUpdate,
    MetricDimensionBind,
    ReconciliationReview,
    ReconciliationSubmit,
)

logger = get_logger("unisense.dimension")

#: 维度成员层级深度上限（含根，1 层 = 根；防止深链/环导致的递归遍历风险）。
_MAX_MEMBER_DEPTH = 10

#: 合法映射类型 / 关联角色 / 缓慢变化维类型取值（DB Enum 列，非法值须在服务层转 4xx，而非 DB 500）。
_VALID_MAPPING_TYPES = {e.value for e in MappingType}
_VALID_ROLES = {e.value for e in MetricDimensionRole}
_VALID_DIM_TYPES = {e.value for e in DimensionType}
_VALID_MEMBER_STATUSES = {e.value for e in DimensionStatus}


class DimensionService(BaseService):
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
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[tuple[Dimension, int]], int]:
        """分页列出维度，返回 (列表, total)（服务端分页，对齐 glossary）。"""
        limit = min(max(page_size, 1), 200)
        offset = (max(page, 1) - 1) * limit
        return await self._repo.list_dimensions(
            domain, status, keyword, owner_id, limit=limit, offset=offset
        )

    async def update_dimension(self, dim_code: str, data: DimensionUpdate) -> Dimension:
        dim = await self._require(dim_code)
        if dim.status == DimensionStatus.DEPRECATED.value:
            raise UnisenseError(f"已废弃维度不可更新: {dim_code}", error_code="INVALID_STATE")
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
        await self._repo.commit()
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
        await self._require(data.dim_code)

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
        member.status = DimensionStatus.PUBLISHED.value
        await self._repo.commit()
        return member

    async def deprecate_member(self, dim_code: str, member_code: str) -> DimensionMember:
        """废弃维度成员（PUBLISHED/DRAFT → DEPRECATED），对齐维度主体状态机。

        废弃保护：存在子成员时禁止废弃——否则子树成员父级悬空（层级权威来源失效）。
        """
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

    async def list_mappings(self, source_dim_code: str | None) -> list[DimensionMapping]:
        return await self._repo.list_mappings(source_dim_code)

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

    async def bind_metric_dimension(self, data: MetricDimensionBind) -> MetricDimension:
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
        metric = await self._sync_dimension_to_metric(data.metric_id, data.dim_code)
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
        """
        binding = await self._repo.delete_metric_dimension(metric_id, dim_code)
        if binding is None:
            raise NotFoundError(f"绑定关系不存在: metric={metric_id}/dim={dim_code}")
        # 反向同步：从指标声明维度移除 dim_code（绑定表与消费声明保持一致）
        stmt = select(Metric).where(Metric.id == metric_id)
        metric = (await self._session.execute(stmt)).scalar_one_or_none()
        if metric is not None:
            defn = dict(metric.definition_json or {})
            dims = [d for d in (defn.get("dimensions") or []) if d != dim_code]
            defn["dimensions"] = dims
            metric.definition_json = defn
            if metric.status not in ("DRAFT", "EXPERIMENTAL"):
                logger.warning(
                    "unbind_metric_dimension_rewrites_published_metric",
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
        self, status: str | None
    ) -> list[tuple[Reconciliation, Metric | None]]:
        return await self._repo.list_reconciliations(status)

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

    async def _sync_dimension_to_metric(self, metric_id: int, dim_code: str) -> Metric | None:
        """回写指标声明维度：让绑定关系对消费链路生效（打通信息孤岛）。

        消费查询（consume 服务）真正校验的是 ``metric.definition_json.dimensions``，
        ``metric_dimension`` 绑定表此前只有本服务读写，对消费无影响。此处绑定成功后
        把 ``dim_code`` 追加进该字段，使绑定即生效。

        - 幂等：``dim_code`` 已存在于声明维度则跳过，不重复追加。
        - DRAFT / EXPERIMENTAL 指标：直接回写，无版本影响。
        - PUBLISHED 等已发布口径：属口径变更，理想应走版本审批流（pending_version）；
          此处不过度设计——仅回写并告警，生产环境应经版本审批（TD §12.6 FR）。

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
        metric.definition_json = defn
        if metric.status not in ("DRAFT", "EXPERIMENTAL"):
            # 已发布口径变更：告警提示应走版本审批（此处仅回写，不触发版本流程）
            logger.warning(
                "bind_metric_dimension_rewrites_published_metric",
                metric_id=metric_id,
                dim_code=dim_code,
                status=metric.status,
            )
        await self._repo.commit()
        return metric

    async def _rename_dimension_in_metric_definitions(
        self, old_code: str, new_code: str
    ) -> None:
        """维度改编码后，同步更新绑定指标口径声明里的维度编码（旧→新）。

        消费校验与血缘 USES_DIMENSION 边的权威来源是 ``metric.definition_json.dimensions``
        （bind 时由 ``_sync_dimension_to_metric`` 写入）。维度改编码只更新了绑定表/成员/映射，
        若不同步指标口径，已发布指标的消费维度白名单与血缘边将指向旧码 → 悬空。
        """
        metrics = await self._repo.list_metrics_by_dimension(old_code)
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
        dim = await self._repo.get_dimension(dim_code)
        if dim is None:
            raise NotFoundError(f"维度不存在: {dim_code}")
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
