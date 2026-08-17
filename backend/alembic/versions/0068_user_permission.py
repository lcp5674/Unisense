"""用户直挂按钮权限点表（RBAC 细粒度增强，TD §12.5）。

背景：授权链路此前仅有「角色间接授权」（role_permission 覆盖表 + user.role），
「给某用户直接配按钮权限」需先建/找角色再挂角色，链路割裂。本迁移新增
``user_permission`` 表，支持对用户**直接挂载**按钮级权限点（不经角色），
``my_permissions`` 将「角色继承 ui_actions」与「用户直挂 ui_actions」做并集
返回——以角色间接授权为主 + 用户直挂按钮为辅。

revision 挂 0067_pii_compliance_enhance（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0068_user_permission"
down_revision = "0067_pii_compliance_enhance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_permission",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="用户 ID"),
        sa.Column("action", sa.String(length=32), nullable=False, comment="UI 权限点（模块:功能）"),
        sa.Column("granted_by", sa.BigInteger(), nullable=True, comment="授权操作人 ID"),
        sa.Column("reason", sa.String(length=512), nullable=True, comment="直挂授权事由"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()"), comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()"), comment="更新时间（UTC）"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间（UTC）"),
        sa.UniqueConstraint("user_id", "action", name="uk_user_permission_user_action"),
    )
    op.create_index("idx_user_permission_user", "user_permission", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_user_permission_user", table_name="user_permission")
    op.drop_table("user_permission")
