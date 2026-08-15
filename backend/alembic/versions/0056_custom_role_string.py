"""细粒度权限管控（方案 A）：user.role 与 role.name 放宽为 String(32) + role.is_custom。

背景：
- ``user.role`` 原为 MySQL Enum（7 内置角色），无法承载自定义角色名 → 放宽为
  String(32)，内置七角色值保持不变，自定义角色名（``[a-z][a-z0-9_]{2,32}``）写入。
- ``role.name`` 原为 Enum(RoleName) → 放宽为 String(32)，与 ``role_permission.role``
  及 ``user.role`` 同口径；新增 ``is_custom`` 标记区分内置 / 自定义角色。

可逆：downgrade 恢复 Enum（内置七角色值均在），并删除 ``is_custom`` 列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0056_custom_role_string"
down_revision = "0055_feedback_nps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    # user.role: Enum(7) → varchar(32)
    op.alter_column(
        "user",
        "role",
        existing_type=sa.Enum(
            "platform_admin",
            "domain_admin",
            "metric_owner",
            "reviewer",
            "compliance_officer",
            "analyst",
            "viewer",
            name="user_role",
        ),
        type_=sa.String(32),
        existing_nullable=False,
        existing_server_default=None,
        comment="用户角色（内置七角色或自定义角色名，方案 A：String 承载）",
    )
    # role.name: Enum(RoleName) → varchar(32)
    op.alter_column(
        "role",
        "name",
        existing_type=sa.Enum(
            "platform_admin",
            "domain_admin",
            "metric_owner",
            "reviewer",
            "compliance_officer",
            "analyst",
            "viewer",
            name="role_name",
        ),
        type_=sa.String(32),
        existing_nullable=False,
        comment="角色名（内置七角色或自定义角色名）",
    )
    op.add_column(
        "role",
        sa.Column(
            "is_custom",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="是否自定义角色",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.drop_column("role", "is_custom")
    op.alter_column(
        "role",
        "name",
        existing_type=sa.String(32),
        type_=sa.Enum(
            "platform_admin",
            "domain_admin",
            "metric_owner",
            "reviewer",
            "compliance_officer",
            "analyst",
            "viewer",
            name="role_name",
        ),
        existing_nullable=False,
    )
    op.alter_column(
        "user",
        "role",
        existing_type=sa.String(32),
        type_=sa.Enum(
            "platform_admin",
            "domain_admin",
            "metric_owner",
            "reviewer",
            "compliance_officer",
            "analyst",
            "viewer",
            name="user_role",
        ),
        existing_nullable=False,
        existing_server_default=None,
        comment="用户角色",
    )
