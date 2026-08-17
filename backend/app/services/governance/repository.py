"""governance 仓储（TD §12.5 / FR-11）。

仅负责数据存取，不含业务判定；事务提交由 API 层负责（与 conflict 模块一致）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_source import DBCatalog
from app.models.governance import (
    Classification,
    Grant,
    GrantStatus,
    GrantType,
    Role,
    RolePermission,
    SensitivityLevel,
    UserPermission,
)
from app.models.metric import Metric


class GovernanceRepository:
    """权限与合规数据访问层。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------ role

    async def get_role_by_name(self, name: str) -> Role | None:
        stmt = select(Role).where(Role.name == name, Role.deleted_at.is_(None))
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def create_role(self, role: Role) -> Role:
        self._db.add(role)
        await self._db.flush()
        await self._db.refresh(role)
        return role

    async def list_custom_roles(self) -> list[Role]:
        """列出全部自定义角色（``is_custom=True``，按名称排序）。"""
        stmt = (
            select(Role)
            .where(Role.is_custom.is_(True), Role.deleted_at.is_(None))
            .order_by(Role.name)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def list_all_roles(self) -> list[Role]:
        """列出全部未删除角色行（内置登记 + 自定义），供授权下拉 id→name 映射。"""
        stmt = (
            select(Role)
            .where(Role.deleted_at.is_(None))
            .order_by(Role.is_custom, Role.name)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def count_users_by_role(self, role: str) -> int:
        """统计使用该角色的用户数（删除自定义角色前的占用校验）。"""
        from app.models.user import User

        stmt = (
            select(func.count())
            .select_from(User)
            .where(User.role == role, User.deleted_at.is_(None))
        )
        return int((await self._db.execute(stmt)).scalar() or 0)

    async def delete_role(self, role: Role) -> None:
        """软删角色行（自定义角色删除）。"""
        now = datetime.now(UTC)
        role.deleted_at = now
        await self._db.flush()

    # -------------------------------------------------------- role permission

    async def list_role_permissions(self) -> list[RolePermission]:
        """列出全部未删除的角色权限点覆盖行（按角色/动作排序）。"""
        stmt = (
            select(RolePermission)
            .where(RolePermission.deleted_at.is_(None))
            .order_by(RolePermission.role, RolePermission.action)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def replace_role_permissions(self, role: str, actions: list[str]) -> None:
        """整表替换某角色的权限点覆盖（先软删既有行，再插入新集合）。"""
        now = datetime.now(UTC)
        stmt = (
            update(RolePermission)
            .where(RolePermission.role == role, RolePermission.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        await self._db.execute(stmt)
        for action in actions:
            self._db.add(RolePermission(role=role, action=action))
        await self._db.flush()

    async def reset_role_permissions(self, role: str) -> int:
        """清除某角色全部权限点覆盖（软删），返回清除行数；此后沿用默认基线。"""
        count_stmt = (
            select(func.count())
            .select_from(RolePermission)
            .where(RolePermission.role == role, RolePermission.deleted_at.is_(None))
        )
        affected = int((await self._db.execute(count_stmt)).scalar() or 0)
        now = datetime.now(UTC)
        stmt = (
            update(RolePermission)
            .where(RolePermission.role == role, RolePermission.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        await self._db.execute(stmt)
        await self._db.flush()
        return affected

    # ------------------------------------------------------ user permission

    async def list_user_ui_permissions(self, user_id: int) -> set[str]:
        """返回某用户直挂的 UI 权限点集合（未删除行）。"""
        stmt = (
            select(UserPermission.action)
            .where(UserPermission.user_id == user_id, UserPermission.deleted_at.is_(None))
        )
        rows = await self._db.execute(stmt)
        return {str(r) for r in rows.scalars().all()}

    async def replace_user_ui_permissions(
        self,
        user_id: int,
        actions: list[str],
        granted_by: int | None,
        reason: str | None,
    ) -> None:
        """整表替换某用户直挂的 UI 权限点（先软删既有行，再插入新集合）。"""
        now = datetime.now(UTC)
        stmt = (
            update(UserPermission)
            .where(UserPermission.user_id == user_id, UserPermission.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        await self._db.execute(stmt)
        for action in actions:
            self._db.add(
                UserPermission(
                    user_id=user_id, action=action, granted_by=granted_by, reason=reason
                )
            )
        await self._db.flush()

    # ----------------------------------------------------------------- grant

    async def create_grant(self, grant: Grant) -> Grant:
        self._db.add(grant)
        await self._db.flush()
        await self._db.refresh(grant)
        return grant

    async def get_grant(self, grant_id: int) -> Grant | None:
        stmt = select(Grant).where(Grant.id == grant_id, Grant.deleted_at.is_(None))
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def find_active_grant(
        self,
        user_id: int,
        role_id: int | None,
        domain: str | None,
        grant_type: GrantType,
    ) -> Grant | None:
        """查找同一 (user, role, domain, grant_type) 的 ACTIVE 授权（服务层幂等依据）。"""
        conditions: list[Any] = [
            Grant.user_id == user_id,
            Grant.grant_type == grant_type,
            Grant.status == GrantStatus.ACTIVE,
            Grant.deleted_at.is_(None),
        ]
        conditions.append(Grant.role_id.is_(None) if role_id is None else Grant.role_id == role_id)
        conditions.append(Grant.domain.is_(None) if domain is None else Grant.domain == domain)
        stmt = select(Grant).where(*conditions).limit(1)
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def list_grants(
        self,
        user_id: int | None,
        domain: str | None,
        status: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[Grant], int]:
        conditions: list[Any] = [Grant.deleted_at.is_(None)]
        if user_id is not None:
            conditions.append(Grant.user_id == user_id)
        if domain is not None:
            conditions.append(Grant.domain == domain)
        if status is not None:
            conditions.append(Grant.status == GrantStatus(status))
        count_stmt = select(func.count()).select_from(Grant).where(*conditions)
        total = int((await self._db.execute(count_stmt)).scalar() or 0)
        stmt = (
            select(Grant)
            .where(*conditions)
            .order_by(Grant.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return list(rows), total

    async def active_grants_for_user(self, user_id: int) -> list[Grant]:
        stmt = (
            select(Grant)
            .where(
                Grant.user_id == user_id,
                Grant.status == GrantStatus.ACTIVE,
                Grant.deleted_at.is_(None),
            )
            .order_by(Grant.created_at.desc())
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def set_grant_status(
        self, grant: Grant, status: GrantStatus, reason: str | None = None
    ) -> Grant:
        grant.status = status
        if reason:
            grant.reason = reason
        await self._db.flush()
        return grant

    async def expire_due_grants(self, now: datetime | None = None) -> list[Grant]:
        """扫描到期授权并置为 EXPIRED（TD §12.5 自动回收 Worker）。"""
        ref = now or datetime.now(UTC)
        stmt = select(Grant).where(
            Grant.status == GrantStatus.ACTIVE,
            Grant.expires_at.is_not(None),
            Grant.expires_at < ref,
            Grant.deleted_at.is_(None),
        )
        rows = list((await self._db.execute(stmt)).scalars().all())
        for row in rows:
            row.status = GrantStatus.EXPIRED
        if rows:
            await self._db.flush()
        return rows

    async def list_expiring_grants(
        self, window: timedelta, now: datetime | None = None
    ) -> list[Grant]:
        """扫描「即将到期且未提醒」的授权（TD §5.5 grant.expiring_soon）。

        口径：status=ACTIVE、expires_at 在 (now, now+window] 内、且未提醒过
        （``expiring_reminded_at IS NULL``）。返回后由 Service 定向通知被授权人，
        再批量标记提醒时间，避免 Worker 每轮重复提醒。
        """
        ref = now or datetime.now(UTC)
        deadline = ref + window
        stmt = select(Grant).where(
            Grant.status == GrantStatus.ACTIVE,
            Grant.expires_at.is_not(None),
            Grant.expires_at > ref,
            Grant.expires_at <= deadline,
            Grant.expiring_reminded_at.is_(None),
            Grant.deleted_at.is_(None),
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def mark_expiring_reminded(
        self, grant_ids: list[int], now: datetime | None = None
    ) -> None:
        """批量标记授权已提醒（grant.expiring_soon 去重）。"""
        if not grant_ids:
            return
        ref = now or datetime.now(UTC)
        stmt = select(Grant).where(Grant.id.in_(grant_ids))
        rows = list((await self._db.execute(stmt)).scalars().all())
        for row in rows:
            row.expiring_reminded_at = ref
        await self._db.flush()

    # ---------------------------------------------------------------- metric

    async def get_metric_by_code(self, metric_code: str) -> Metric | None:
        stmt = select(Metric).where(Metric.metric_code == metric_code, Metric.deleted_at.is_(None))
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def set_compliance_reviewed(self, metric: Metric, reviewed: bool) -> Metric:
        metric.compliance_reviewed = reviewed
        await self._db.flush()
        return metric

    # --------------------------------------------------------------- catalog

    async def list_catalog(
        self,
        source_id: str | None,
        catalog_ids: list[int] | None,
        limit: int,
    ) -> list[DBCatalog]:
        conditions: list[Any] = [DBCatalog.deleted_at.is_(None)]
        if source_id is not None:
            conditions.append(DBCatalog.source_id == source_id)
        if catalog_ids:
            conditions.append(DBCatalog.id.in_(catalog_ids))
        stmt = select(DBCatalog).where(*conditions).order_by(DBCatalog.id).limit(limit)
        return list((await self._db.execute(stmt)).scalars().all())

    async def update_catalog_sensitivity(self, catalog_id: int, level: SensitivityLevel) -> None:
        # db_catalog.sensitivity_level 枚举不含 UNKNOWN（降级标记仅落 classification 表）
        stmt = (
            update(DBCatalog)
            .where(DBCatalog.id == catalog_id)
            .values(sensitivity_level=level.value)
        )
        await self._db.execute(stmt)

    # -------------------------------------------------------- classification

    async def get_classification(self, catalog_id: int) -> Classification | None:
        stmt = (
            select(Classification)
            .where(Classification.catalog_id == catalog_id, Classification.deleted_at.is_(None))
            .order_by(Classification.created_at.desc())
            .limit(1)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def upsert_classification(
        self,
        catalog_id: int,
        level: SensitivityLevel,
        pii_columns: list[dict[str, Any]],
        classified_by: str,
        model_version: str,
    ) -> Classification:
        existing = await self.get_classification(catalog_id)
        if existing is not None:
            existing.sensitivity_level = level
            existing.pii_columns = pii_columns
            existing.classified_by = classified_by
            existing.model_version = model_version
            await self._db.flush()
            return existing
        row = Classification(
            catalog_id=catalog_id,
            sensitivity_level=level,
            pii_columns=pii_columns,
            classified_by=classified_by,
            model_version=model_version,
        )
        self._db.add(row)
        await self._db.flush()
        await self._db.refresh(row)
        return row
