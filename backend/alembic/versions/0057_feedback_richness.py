"""feedback 表新增 category / priority / source_url（TD §12.10 / FR-16）。

背景：
- 反馈模块数据丰富度增强：运营处理反馈时需要按类分派（bug/feature/improvement/
  question/praise）与排期（high/medium/low），此前仅有笼统 comment 文本。
- 来源页面 URL 便于复现问题与了解用户操作路径（提交时自动捕获，不要求用户填写）。

可逆：downgrade 删除三列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057_feedback_richness"
down_revision = "0056_custom_role_string"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.add_column(
        "feedback",
        sa.Column(
            "category",
            sa.String(32),
            nullable=False,
            server_default="improvement",
            comment="反馈分类：bug/feature/improvement/question/praise",
        ),
    )
    op.add_column(
        "feedback",
        sa.Column(
            "priority",
            sa.String(16),
            nullable=False,
            server_default="medium",
            comment="反馈优先级：high/medium/low",
        ),
    )
    op.add_column(
        "feedback",
        sa.Column(
            "source_url",
            sa.String(512),
            nullable=True,
            comment="反馈来源页面 URL",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.drop_column("feedback", "source_url")
    op.drop_column("feedback", "priority")
    op.drop_column("feedback", "category")
