"""user 表新增首登强制改密标记（首次登录须强制改密）。

背景：管理员创建用户/重置密码时设置的初始密码需强制用户在首次登录后修改，
本迁移为 ``user`` 表增加 ``must_change_password`` 布尔列（默认 False）。
server_default 用于存量行回填，避免 NOT NULL 加列失败。可逆：downgrade 删除该列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_user_must_change_password"
down_revision = "0043_metric_description"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.add_column(
        "user",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="首次登录须强制改密",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.drop_column("user", "must_change_password")
