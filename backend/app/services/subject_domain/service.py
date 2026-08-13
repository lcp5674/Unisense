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
            created_at=domain.created_at,
            updated_at=domain.updated_at,
        )

    async def list_tree(self, status: str | None = None) -> list[SubjectDomainTreeNode]:
        """获取域树（3层）。"""
        all_domains = await self._repo.list_all(status)
        count_map: dict[str, int] = {}
        for d in all_domains:
            count_map[d.code] = await self._repo.get_metric_count(d.code)

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

    async def create_domain(self, data: SubjectDomainCreate) -> SubjectDomain:
        # 编码唯一性校验
        if await self._repo.code_exists(data.code):
            raise ConflictError(f"域编码已存在: {data.code}", error_code="DUPLICATE_CODE")

        # 层级校验
        level = 1
        parent_path = ""
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

        domain = SubjectDomain(
            code=data.code,
            name=data.name,
            parent_id=data.parent_id,
            level=level,
            path="",  # flush 后更新
            sort_order=data.sort_order,
            status="active",
            defaults_json=data.defaults_json,
            description=data.description,
            owner_id=data.owner_id,
        )
        domain = await self._repo.create(domain)

        # 更新 path（复用父域已校验路径，避免重复查询；父域在此前已做 None 校验）
        domain.path = f"{parent_path}.{domain.id}" if data.parent_id is not None else str(domain.id)
        await self._repo.update(domain)

        logger.info("domain_created", code=data.code, level=level)
        return domain

    async def update_domain(self, code: str, data: SubjectDomainUpdate) -> SubjectDomain:
        domain = await self.get_domain(code)
        if data.name is not None:
            domain.name = data.name
        if data.sort_order is not None:
            domain.sort_order = data.sort_order
        if data.description is not None:
            domain.description = data.description
        if data.owner_id is not None:
            domain.owner_id = data.owner_id
        if data.defaults_json is not None:
            domain.defaults_json = data.defaults_json
        domain = await self._repo.update(domain)
        logger.info("domain_updated", code=code)
        return domain

    async def deactivate_domain(self, code: str) -> SubjectDomain:
        domain = await self.get_domain(code)
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
