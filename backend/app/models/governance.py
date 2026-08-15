"""权限与合规治理领域模型（TD §4.1 / §12.5，FR-11）。

覆盖三张表：

- ``role``           角色枚举（对齐 PRD 4.9.2 六角色）
- ``grants``         域授权 + 指标白名单 + 行级开关 + 临时授权 TTL
- ``classification`` 分级分类结果（sensitivity_level / pii_columns / model_version）

说明（与 TD §4.1 的实现偏差，已同步文档）：

- TD 原 ``uk_grant UNIQUE(user_id, role_id, domain, metric_whitelist)`` 无法落地——
  MySQL 不支持在 JSON 列上建唯一索引；改为服务层保证
  「同一 (user_id, role_id, domain, grant_type) 至多一条 ACTIVE 授权」，白名单做并集合并。
- ``grants.status`` 为 TD §12.5 到期自动回收（PRD 4.9.6）所需，TD §4.1 已补列。
- ``classification.sensitivity_level`` 增加 ``UNKNOWN``，用于 TD §12.5 分级引擎不可用时的
  降级标记（标 UNKNOWN 不阻断）。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


def _values(e: type[enum.Enum]) -> list[str]:
    """StrEnum 以 ``.value`` 而非成员名落库（对齐 models/conflict.py）。"""
    return [str(m.value) for m in e]


class RoleName(enum.StrEnum):
    """七角色模型（TD §12.5.1 / PRD 4.9.2）。

    与 User.role 数据库枚举、api/users.py UserRole Literal 对齐，
    analyst 为只读消费者角色（兼容 0001 迁移）。
    """

    PLATFORM_ADMIN = "platform_admin"
    DOMAIN_ADMIN = "domain_admin"
    METRIC_OWNER = "metric_owner"
    REVIEWER = "reviewer"
    COMPLIANCE_OFFICER = "compliance_officer"
    ANALYST = "analyst"
    VIEWER = "viewer"


class GrantType(enum.StrEnum):
    """授权类型：跨域只读引用（READ）vs 源域读写（WRITE）。"""

    READ = "READ"
    WRITE = "WRITE"
    READ_WRITE = "READ_WRITE"


class GrantStatus(enum.StrEnum):
    """授权状态机：ACTIVE →（到期）EXPIRED /（人工）REVOKED。"""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class SensitivityLevel(enum.StrEnum):
    """敏感级别（与 db_catalog.sensitivity_level 对齐，额外含降级标记 UNKNOWN）。"""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    PII = "PII"
    UNKNOWN = "UNKNOWN"


class Role(Base, BaseModel):
    """角色表（TD §4.1 ``role``）。"""

    __tablename__ = "role"

    name: Mapped[RoleName] = mapped_column(
        Enum(RoleName, values_callable=_values),
        nullable=False,
        unique=True,
        comment="角色名（对齐 PRD 4.9.2）",
    )
    description: Mapped[str | None] = mapped_column(String(256), nullable=True, comment="角色说明")


class RolePermission(Base, BaseModel):
    """角色权限点覆盖表（RBAC 可配置化，TD §12.5 增强）。

    记录对 ``policy.ROLE_ACTIONS`` 默认基线的**覆盖**：某角色在该表中出现的动作集合
    即该角色的生效权限点；未出现的角色沿用默认基线。``(role, action)`` 唯一，
    覆盖以「整表替换该角色动作」语义更新（先删该角色全部行，再插入新集合）。

    Attributes:
        role: 角色名（对齐 User.role 7 值，含 analyst）。
        action: 权限点（read/write/approve/export/review，取自
            ``policy.CONFIGURABLE_ACTIONS`` 白名单）。
    """

    __tablename__ = "role_permission"

    role: Mapped[str] = mapped_column(String(32), nullable=False, comment="角色名")
    action: Mapped[str] = mapped_column(String(32), nullable=False, comment="权限点（动作）")

    __table_args__ = (
        Index("idx_role_permission_role", "role"),
        UniqueConstraint("role", "action", name="uk_role_permission_role_action"),
    )


class Grant(Base, BaseModel):
    """授权表（TD §4.1 ``grants``；规避 MySQL 保留字 grant）。"""

    __tablename__ = "grants"

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="被授权用户 ID")
    role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="关联角色 ID")
    domain: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="授权主题域")
    metric_whitelist: Mapped[list[Any] | None] = mapped_column(
        JSON, nullable=True, comment="指标白名单（metric_code 列表）"
    )
    row_level: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="行级权限开关（一期仅标记 restricted）"
    )
    grant_type: Mapped[GrantType] = mapped_column(
        Enum(GrantType, values_callable=_values),
        nullable=False,
        default=GrantType.READ,
        comment="授权类型",
    )
    status: Mapped[GrantStatus] = mapped_column(
        Enum(GrantStatus, values_callable=_values),
        nullable=False,
        default=GrantStatus.ACTIVE,
        comment="授权状态（到期自动回收，PRD 4.9.6）",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="临时授权 TTL，NULL=永久"
    )
    expiring_reminded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="授权到期提醒时间（非空=已提醒过，到期提醒 Worker 跳过，TD §5.5）",
    )
    granted_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="授权操作人 ID"
    )
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="授权/回收事由")

    __table_args__ = (
        Index("idx_grant_user", "user_id"),
        Index("idx_grant_domain", "domain"),
        Index("idx_grant_status_expires", "status", "expires_at"),
    )


class Classification(Base, BaseModel):
    """分级分类结果表（TD §4.1 ``classification``，FR-11）。"""

    __tablename__ = "classification"

    catalog_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联 db_catalog.id"
    )
    sensitivity_level: Mapped[SensitivityLevel] = mapped_column(
        Enum(SensitivityLevel, values_callable=_values),
        nullable=False,
        default=SensitivityLevel.INTERNAL,
        comment="敏感级别",
    )
    pii_columns: Mapped[list[Any] | None] = mapped_column(
        JSON, nullable=True, comment="命中 PII 的字段明细"
    )
    classified_by: Mapped[str] = mapped_column(
        String(32), nullable=False, default="rule_engine", comment="分级来源"
    )
    model_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="rules-v1", comment="规则/模型版本"
    )

    __table_args__ = (Index("idx_classification_catalog", "catalog_id"),)
