"""用户与组织模型。

对齐 TD §4.1 user 表和 PRD §3.1 用户画像矩阵。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Enum, ForeignKey, Index, String, UniqueConstraint
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
    #: 团队可绑定的业务域（方案 B）：用户侧「所属域 + 所属组织」合并为「所属团队」，
    #: 团队绑定域后其成员用户的 ``user.domain`` 自动继承；可为空 = 不限域（成员无默认域，
    #: 需经 grants 授权才能操作指标）。仅存主题域 code（与 ``user.domain`` 同口径，不设外键）。
    domain: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="所属业务域（可空=不限域，成员自动继承）"
    )
    status: Mapped[str] = mapped_column(
        Enum("active", "suspended", "deleted", name="org_status"),
        nullable=False,
        default="active",
        comment="组织状态",
    )

    users: Mapped[list[User]] = relationship("User", back_populates="org", lazy="selectin")

    __table_args__ = (Index("idx_organization_code", "code"),)


class UserRole(Base, BaseModel):
    """用户角色关联表（多角色，TD §4.1 增强：user.role 保留为主角色冗余）。

    方案 A 多角色：``user_role`` 为**权威角色源**（一个用户可挂多角色），
    ``user.role`` 为主角色（权限最高者，向后兼容所有既有单角色读取/责任链）。
    存量单角色用户迁移回填为一行 ``user_role(user_id, role=user.role)``。

    Attributes:
        user_id: 用户 ID（关联 user.id）。
        role: 角色名（内置七角色或自定义角色名）。
    """

    __tablename__ = "user_role"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", name="fk_user_role_user"), nullable=False, comment="用户 ID"
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False, comment="角色名")

    __table_args__ = (
        Index("idx_user_role_user", "user_id"),
        UniqueConstraint("user_id", "role", name="uk_user_role_user_role"),
    )


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
    totp_secret: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="TOTP 双因子密钥（Fernet 加密存储，setup 时写入、confirm 时启用）",
    )
    totp_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否已启用 TOTP 双因子认证"
    )

    org: Mapped[Organization] = relationship("Organization", back_populates="users")
    #: 多角色关联（方案 A）：权威角色源为 ``user_role`` 表；``user.role`` 为主角色冗余。
    role_items: Mapped[list[UserRole]] = relationship(
        "UserRole", lazy="selectin", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_user_org", "org_id"),
        Index("idx_user_role", "role"),
    )

    def roles_all(self) -> list[str]:
        """返回用户全部角色：主角色（``user.role``）+ ``user_role`` 表扩展角色。

        主角色恒在首位；``user_role`` 含主角色重复值时去重。
        通过 ``__dict__`` 读取 relationship 避免触发未加载实例的 lazy load
        （测试/构造期直接 ``User(**base)`` 未挂 role_items 时安全回退为主角色）。
        """
        primary = str(self.role.value if hasattr(self.role, "value") else self.role)
        extra = [str(r.role) for r in (self.__dict__.get("role_items") or [])]
        seen: set[str] = {primary}
        result = [primary]
        for r in extra:
            if r not in seen:
                seen.add(r)
                result.append(r)
        return result

    def has_role(self, role: str) -> bool:
        """判断用户是否拥有指定角色（主角色或 ``user_role`` 扩展角色）。"""
        return role in self.roles_all()
