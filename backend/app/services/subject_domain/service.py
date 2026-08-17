"""主题域业务逻辑层。"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessError, ConflictError, NotFoundError
from app.models.subject_domain import SubjectDomain
from app.services.subject_domain.repository import SubjectDomainRepository
from app.services.subject_domain.schemas import (
    SubjectDomainCreate,
    SubjectDomainDefaultsUpdate,
    SubjectDomainResponse,
    SubjectDomainTreeNode,
    SubjectDomainUpdate,
)

logger = structlog.get_logger("unisense.subject_domain.service")

# 最大层级深度
MAX_LEVEL = 3
#: 自动生成编码的最大长度（对齐模型 String(64)）
_MAX_CODE_LEN = 64
#: 自动生成编码冲突自增后缀上限
_MAX_CODE_ATTEMPTS = 100
#: 域默认值字段 → 字典类型映射（对齐前端 DICT_FIELD_MAPS）。
#: 仅这些字典枚举字段做合法性校验；description 等自由文本字段不校验。
_DEFAULT_FIELD_DICT_TYPES: dict[str, str] = {
    "granularity": "granularity",
    "unit": "unit",
    "aggregation": "aggregation",
    "time_semantics": "time_semantics",
    "freshness": "freshness",
    "dw_layer": "dw_layer",
    "metric_tier": "metric_tier",
    "serving_mode": "serving_mode",
    "additivity": "additivity",
    "type": "metric_type",
}


async def _validate_defaults_json(db: Any, defaults_json: dict[str, Any]) -> None:
    """校验域默认值：字典枚举字段值必须合法（防配置非法枚举致注册预填失效）。

    ``update_defaults`` 与 ``update_domain``（编辑域时携带 defaults_json）共用，
    消除两处写路径校验不一致（此前仅 update_defaults 校验，update_domain 可绕过）。
    """
    # 延迟导入避免循环依赖
    from app.services.system_dict.repository import SystemDictRepository

    invalid: list[str] = []
    for field, value in (defaults_json or {}).items():
        dict_type = _DEFAULT_FIELD_DICT_TYPES.get(field)
        if dict_type is None or value is None:
            continue
        if not isinstance(value, str) or not await SystemDictRepository(db).item_exists(
            dict_type, value
        ):
            invalid.append(f"{field}={value!r}")
    if invalid:
        raise BusinessError(
            "域默认值含非法字典枚举值: "
            + "；".join(invalid)
            + "。请从字典项中选择合法值。",
            error_code="VALIDATION_ERROR",
        )


class SubjectDomainService:
    """主题域服务。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repo = SubjectDomainRepository(db)

    async def get_domain(self, code: str) -> SubjectDomain:
        domain = await self._repo.get_by_code(code)
        if domain is None:
            raise NotFoundError(f"主题域不存在: {code}")
        return domain

    async def get_domain_with_count(self, code: str) -> SubjectDomainResponse:
        domain = await self.get_domain(code)
        count = await self._repo.get_metric_count(code)
        dim_count = await self._repo.get_dimension_count(code)
        return SubjectDomainResponse(
            id=domain.id,
            code=domain.code,
            name=domain.name,
            parent_id=domain.parent_id,
            level=domain.level,
            path=domain.path,
            sort_order=domain.sort_order,
            status=domain.status,
            defaults_json=domain.defaults_json,
            description=domain.description,
            owner_id=domain.owner_id,
            metric_count=count,
            dimension_count=dim_count,
            created_at=domain.created_at,
            updated_at=domain.updated_at,
        )

    async def list_tree(self, status: str | None = None) -> list[SubjectDomainTreeNode]:
        """获取域树（3层）。"""
        all_domains = await self._repo.list_all(status)
        count_map: dict[str, int] = {}
        dim_count_map: dict[str, int] = {}
        for d in all_domains:
            count_map[d.code] = await self._repo.get_metric_count(d.code)
            dim_count_map[d.code] = await self._repo.get_dimension_count(d.code)

        id_map: dict[int, SubjectDomainTreeNode] = {}
        for d in all_domains:
            id_map[d.id] = SubjectDomainTreeNode(
                id=d.id,
                code=d.code,
                name=d.name,
                parent_id=d.parent_id,
                level=d.level,
                sort_order=d.sort_order,
                status=d.status,
                metric_count=count_map.get(d.code, 0),
                dimension_count=dim_count_map.get(d.code, 0),
                children=[],
            )

        roots: list[SubjectDomainTreeNode] = []
        for node in id_map.values():
            if node.parent_id is None:
                roots.append(node)
            else:
                parent = id_map.get(node.parent_id)
                if parent:
                    parent.children.append(node)

        roots.sort(key=lambda n: (n.sort_order, n.code))
        return roots

    async def create_domain(
        self, data: SubjectDomainCreate, owner_id: int | None = None
    ) -> SubjectDomain:
        # 层级校验
        level = 1
        parent_path = ""
        parent: SubjectDomain | None = None
        if data.parent_id is not None:
            parent = await self._repo.get_by_id(data.parent_id)
            if parent is None:
                raise NotFoundError(f"父域不存在: {data.parent_id}")
            if parent.status != "active":
                raise BusinessError("父域已停用，不可在其下创建子域", error_code="PARENT_INACTIVE")
            level = parent.level + 1
            if level > MAX_LEVEL:
                raise BusinessError(
                    f"主题域最多 {MAX_LEVEL} 层，当前父域已是第 {parent.level} 层",
                    error_code="LEVEL_EXCEEDED",
                )
            parent_path = parent.path or str(parent.id)

        # 编码唯一性校验（未传时自动生成）
        code = data.code
        if code:
            if await self._repo.code_exists(code):
                raise ConflictError(f"域编码已存在: {code}", error_code="DUPLICATE_CODE")
        else:
            code = await self._generate_unique_code(data.name, parent)

        # 同父域下名称唯一（不同父域允许同名；大小写不敏感、忽略首尾空格）
        if await self._repo.name_exists(data.name, data.parent_id):
            raise ConflictError(
                f"同父域下已存在同名主题域: {data.name}",
                error_code="DUPLICATE_NAME",
            )

        # 域默认值字典枚举校验（对齐 update_domain/update_defaults 两处写路径，三条路径一致）
        await _validate_defaults_json(self._db, data.defaults_json)

        domain = SubjectDomain(
            code=code,
            name=data.name,
            parent_id=data.parent_id,
            level=level,
            path="",  # flush 后更新
            sort_order=data.sort_order,
            status="active",
            defaults_json=data.defaults_json,
            description=data.description,
            owner_id=owner_id if owner_id is not None else data.owner_id,
        )
        domain = await self._repo.create(domain)

        # 更新 path（复用父域已校验路径，避免重复查询；父域在此前已做 None 校验）
        domain.path = f"{parent_path}.{domain.id}" if data.parent_id is not None else str(domain.id)
        await self._repo.update(domain)

        logger.info("domain_created", code=code, level=level, auto_generated=not data.code)
        return domain

    @staticmethod
    def _slugify_code(name: str) -> str:
        """把显示名规范化为域编码片段（委托 ``app.core.codegen.slugify_code``）。

        规则：中文转英文（术语字典 + 拼音兜底）、ASCII 保留、段落用下划线连接；
        返回空串表示无可提取字符（纯标点/空白名）。
        """
        from app.core.codegen import slugify_code

        return slugify_code(name)

    async def _generate_unique_code(self, name: str, parent: SubjectDomain | None) -> str:
        """自动生成唯一域编码。

        规则：显示名中文转英文（术语字典 + 拼音兜底）、ASCII 保留生成 slug；
        子域拼接父域编码前缀（保持树形语义）；纯标点/空白等无可提取字符时
        回退 ``{父域}_sub`` / ``domain``；冲突时追加 ``_2/_3/...`` 后缀
        （上限 100 次）。与数据源 source_id 自动生成约定保持一致。
        """
        slug = self._slugify_code(name)
        if slug:
            base_id = f"{parent.code}_{slug}" if parent else slug
        else:
            # 无可提取字符（纯标点/空白）：子域用「父域_sub」，根域用「domain」兜底
            base_id = f"{parent.code}_sub" if parent else "domain"

        candidate = base_id[:_MAX_CODE_LEN]
        n = 2
        while await self._repo.code_exists(candidate):
            suffix = f"_{n}"
            candidate = f"{base_id[: _MAX_CODE_LEN - len(suffix)]}{suffix}"
            n += 1
            if n > _MAX_CODE_ATTEMPTS:
                raise BusinessError(
                    f"无法为 {name} 生成唯一域编码，请手动指定",
                    error_code="DOMAIN_CODE_EXHAUSTED",
                )
        return candidate

    async def update_domain(self, code: str, data: SubjectDomainUpdate) -> SubjectDomain:
        domain = await self.get_domain(code)
        if data.name is not None:
            # 改名时检测同父域同名（排除自身；名称未实际变化则不查）
            if data.name.strip() != (domain.name or "").strip() and await self._repo.name_exists(
                data.name, domain.parent_id, exclude_id=domain.id
            ):
                raise ConflictError(
                    f"同父域下已存在同名主题域: {data.name}",
                    error_code="DUPLICATE_NAME",
                )
            domain.name = data.name
        if data.sort_order is not None:
            domain.sort_order = data.sort_order
        if data.description is not None:
            domain.description = data.description
        if data.owner_id is not None:
            domain.owner_id = data.owner_id
        if data.defaults_json is not None:
            # 与 update_defaults 同款字典枚举校验（消除两处写路径校验不一致）
            await _validate_defaults_json(self._db, data.defaults_json)
            domain.defaults_json = data.defaults_json
        domain = await self._repo.update(domain)
        logger.info("domain_updated", code=code)
        return domain

    async def deactivate_domain(self, code: str) -> SubjectDomain:
        domain = await self.get_domain(code)
        # 级联一致性：存在 active 子域时禁止停用父域（须先停用子域），
        # 避免「父 inactive、子仍 active」导致子域下指标在父停用后仍可服务。
        # 与 activate_domain 校验父域 active 对称（fail-closed）。
        active_children = await self._repo.list_children(domain.id)
        active_children = [c for c in active_children if c.status == "active"]
        if active_children:
            raise BusinessError(
                "存在启用中的子域，请先停用全部子域再停用本域",
                error_code="ACTIVE_CHILDREN",
                ctx={"child_codes": [c.code for c in active_children]},
            )
        domain.status = "inactive"
        domain = await self._repo.update(domain)
        logger.info("domain_deactivated", code=code)
        return domain

    async def activate_domain(self, code: str) -> SubjectDomain:
        domain = await self.get_domain(code)
        # 父域也必须是 active
        if domain.parent_id is not None:
            parent = await self._repo.get_by_id(domain.parent_id)
            if parent and parent.status != "active":
                raise BusinessError("父域已停用，请先启用父域", error_code="PARENT_INACTIVE")
        domain.status = "active"
        domain = await self._repo.update(domain)
        logger.info("domain_activated", code=code)
        return domain

    async def delete_domain(self, code: str) -> None:
        domain = await self.get_domain(code)

        # 校验关联指标
        metric_count = await self._repo.get_metric_count(code)
        if metric_count > 0:
            raise BusinessError(
                f"该域下存在 {metric_count} 个关联指标，请先迁移或归档",
                error_code="HAS_REFERENCED_METRICS",
            )

        # 校验关联维度（与指标保护对称：域下维度存在时删除将产生孤儿维度）
        dim_count = await self._repo.get_dimension_count(code)
        if dim_count > 0:
            raise BusinessError(
                f"该域下存在 {dim_count} 个关联维度，请先迁移或废弃",
                error_code="HAS_REFERENCED_DIMENSIONS",
            )

        # 校验子域
        child_count = await self._repo.count_children(domain.id)
        if child_count > 0:
            raise BusinessError(
                f"该域下存在 {child_count} 个子域，请先删除或迁移子域",
                error_code="HAS_CHILDREN",
            )

        await self._repo.soft_delete(domain)
        logger.info("domain_deleted", code=code)

    async def get_defaults(self, code: str) -> dict[str, Any]:
        domain = await self.get_domain(code)
        return domain.defaults_json or {}

    async def update_defaults(self, code: str, data: SubjectDomainDefaultsUpdate) -> SubjectDomain:
        """更新域默认值预设；字典枚举字段校验值合法（防配置非法枚举致注册预填失效）。"""
        await _validate_defaults_json(self._db, data.defaults_json)
        domain = await self.get_domain(code)
        domain.defaults_json = data.defaults_json
        domain = await self._repo.update(domain)
        logger.info("domain_defaults_updated", code=code)
        return domain

    async def get_domain_metrics(self, code: str) -> list[dict[str, Any]]:
        """获取域下指标列表。"""
        from sqlalchemy import select

        from app.models.metric import Metric

        stmt = (
            select(Metric)
            .where(
                Metric.domain == code,
                Metric.deleted_at.is_(None),
            )
            .order_by(Metric.metric_code)
        )
        result = await self._db.execute(stmt)
        metrics = list(result.scalars().all())
        return [
            {
                "id": m.id,
                "metric_code": m.metric_code,
                "name": m.name,
                "status": m.status,
                "type": m.type,
            }
            for m in metrics
        ]

    async def validate_domain_active(self, code: str) -> SubjectDomain:
        """校验域存在且 active（供指标注册时调用）。"""
        domain = await self._repo.get_by_code(code)
        if domain is None:
            raise NotFoundError(f"主题域不存在: {code}", error_code="DOMAIN_NOT_FOUND")
        if domain.status != "active":
            raise BusinessError(f"主题域已停用: {code}", error_code="DOMAIN_INACTIVE")
        return domain
