"""用户多角色关联表 user_role（方案 A，TD §4.1 增强）。

背景：用户管理需支持多角色（如某用户既是域管理员、也是指标负责人、还是评审员）。
``user.role`` 保留为主角色（权限最高者，向后兼容所有既有单角色读取/责任链），
新增 ``user_role`` 表为**权威角色源**（一用户可挂多角色，``(user_id, role)`` 唯一）。

本迁移：
1. 建表 ``user_role``（含 id/created_at/updated_at/deleted_at 公共字段）。
2. 回填：存量未软删用户各落一行 ``user_role(user_id, role=user.role)``，
   使存量单角色用户的角色进入权威表（主角色不变，语义等价）。

revision 挂 0088_batch_infer_history_soft_delete（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0089_user_role"
down_revision = "0088_batch_infer_history_soft_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_role",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            nullable=False,
            comment="用户 ID",
        ),
        sa.Column(
            "role",
            sa.String(length=32),
            nullable=False,
            comment="角色名（内置七角色或自定义角色名）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_user_role_user"),
        sa.UniqueConstraint("user_id", "role", name="uk_user_role_user_role"),
    )
    op.create_index("idx_user_role_user", "user_role", ["user_id"])

    # 回填存量：每个未软删用户主角色落一行 user_role（权威角色源）。
    op.execute(
        "INSERT INTO user_role (user_id, role, created_at, updated_at) "
        "SELECT id, role, NOW(), NOW() FROM user WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    op.drop_index("idx_user_role_user", table_name="user_role")
    op.drop_table("user_role")
