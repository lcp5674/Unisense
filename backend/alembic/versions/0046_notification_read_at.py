"""notification 增补 read_at（用户已读时间）字段。

背景：通知模块新增"已读时间"能力（Notification.read_at，NULL 表示未读），
但 DB 表缺少该列，导致 ORM 查询通知列表时 SELECT 携带 read_at → Unknown column 500。
本迁移为 notification 表补上 read_at 列（DateTime nullable + 索引，与模型一致）。

可逆：downgrade 回退该列与索引。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_notification_read_at"
down_revision = "0045_role_permission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification",
        sa.Column(
            "read_at",
            sa.DateTime(),
            nullable=True,
            comment="用户已读时间（NULL 表示未读）",
        ),
    )
    op.create_index("ix_notification_read_at", "notification", ["read_at"])


def downgrade() -> None:
    op.drop_index("ix_notification_read_at", table_name="notification")
    op.drop_column("notification", "read_at")
