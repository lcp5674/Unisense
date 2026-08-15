"""用户与组织模型。

对齐 TD §4.1 user 表和 PRD §3.1 用户画像矩阵。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Enum, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.mysql import Base
from app.models.base import BaseModel


class Organization(Base, BaseModel):
    """组织（租户）实体。

    顶级数据隔离单元。

    Attributes:
        name: 组织名称。
        code: 组织编码（唯一）。
        status: 组织状态。
    """

    __tablename__ = "organization"

    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="组织名称")
    code: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="组织编码（唯一）"
    )
    status: Mapped[str] = mapped_column(
        Enum("active", "suspended", "deleted", name="org_status"),
        nullable=False,
        default="active",
        comment="组织状态",
    )

    users: Mapped[list[User]] = relationship("User", back_populates="org", lazy="selectin")

    __table_args__ = (Index("idx_organization_code", "code"),)


class User(Base, BaseModel):
    """用户实体。

    Attributes:
        org_id: 所属组织 ID。
        username: 用户名。
        email: 邮箱（唯一）。
        password_hash: 密码哈希（bcrypt）。
        display_name: 显示名称。
        role: 角色（platform_admin/domain_admin/metric_owner/reviewer/
            compliance_officer/analyst/viewer，对齐 PRD 4.9.2 六角色 + 历史 analyst）。
        domain: 所属域。
        status: 用户状态。
        must_change_password: 首次登录须强制改密（管理员创建/重置后为 True）。
        last_login_at: 最后登录时间。
    """

    __tablename__ = "user"

    org_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id", name="fk_user_organization"), nullable=False
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False, comment="用户名")
    email: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, comment="邮箱（唯一）"
    )
    password_hash: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="密码哈希（bcrypt）"
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, comment="显示名称")
    role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="viewer",
        comment="用户角色（内置七角色或自定义角色名，方案 A：String 承载）",
    )
    domain: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="所属域")
    status: Mapped[str] = mapped_column(
        Enum("active", "disabled", "deleted", name="user_status"),
        nullable=False,
        default="active",
        comment="用户状态",
    )
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True, comment="最后登录时间")
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="首次登录须强制改密"
    )

    org: Mapped[Organization] = relationship("Organization", back_populates="users")

    __table_args__ = (
        Index("idx_user_org", "org_id"),
        Index("idx_user_role", "role"),
    )
