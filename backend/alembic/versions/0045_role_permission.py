"""新增 role_permission 角色权限点覆盖表（RBAC 可配置化，TD §12.5 增强）。

背景：权限治理角色的「本域动作」原先硬编码在 ``policy.ROLE_ACTIONS``，产品无法自助调整。
本迁移新建 ``role_permission`` 表作为默认基线的覆盖层：``(role, action)`` 唯一，
某角色在该表中的动作集合即生效权限点；未覆盖的角色沿用 ``policy.ROLE_ACTIONS`` 默认。
可逆：downgrade 删除该表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045_role_permission"
down_revision = "0044_user_must_change_password"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.create_table(
        "role_permission",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "role",
            sa.String(32),
            nullable=False,
            comment="角色名",
        ),
        sa.Column(
            "action",
            sa.String(32),
            nullable=False,
            comment="权限点（动作）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
            comment="更新时间",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删时间",
        ),
        sa.UniqueConstraint("role", "action", name="uk_role_permission_role_action"),
    )
    op.create_index("idx_role_permission_role", "role_permission", ["role"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.drop_index("idx_role_permission_role", table_name="role_permission")
    op.drop_table("role_permission")
