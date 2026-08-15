"""feedback 表新增 nps_score / resolver_id / resolved_at（TD §12.10 / FR-16）。

背景：
- NPS 数据语义解耦：此前 submit_nps 将 0-10 的 NPS 分数写入 rating 列（1-5 语义）
  造成数据冲突。新增独立 nps_score 列承载 0-10 评分，rating 只存 1-5 反馈评分。
- 反馈处理增强：新增 resolver_id（处理人）与 resolved_at（处理时间），支撑反馈
  采纳闭环的处理留痕与审计。

可逆：downgrade 删除三列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_feedback_nps"
down_revision = "0054_grant_expiring_reminded_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.add_column(
        "feedback",
        sa.Column(
            "nps_score",
            sa.Integer(),
            nullable=True,
            comment="NPS 评分 0-10",
        ),
    )
    op.add_column(
        "feedback",
        sa.Column(
            "resolver_id",
            sa.BigInteger(),
            nullable=True,
            comment="处理人 ID",
        ),
    )
    op.add_column(
        "feedback",
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="处理时间",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.drop_column("feedback", "resolved_at")
    op.drop_column("feedback", "resolver_id")
    op.drop_column("feedback", "nps_score")
