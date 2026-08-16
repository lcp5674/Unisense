"""notification 表新增送达失败原因与待办已处理标记（last_error + handled_at）。

背景（TD §12.9 / FR-16 产品化收件箱）：通知中心的送达失败处置与待办闭环缺失——
webhook/email 等外发渠道投递失败时只标 FAILED，无法查看原因/重试；待办类通知
（冲突待仲裁等）处理完成后仍是旧快照，无法表达「已处理」状态。

设计：
- ``last_error``：Text 可空，最近一次投递失败的简短原因（渠道未配置/HTTP 状态/异常），
  供前端 FAILED 卡片展示与运营定位，重试成功后清空。
- ``handled_at``：DateTime 可空，待办类通知被「标记已处理」的时间（NULL=未处理）。
  列表「仅待处理」筛选同时排除 handled_at 非空项，闭环后不再打扰用户。

可逆：downgrade 删除两列（存量数据随列删除，符合回退语义）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0062_notification_delivery"
down_revision = "0061_term_relation_type_extend"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification",
        sa.Column("last_error", sa.Text(), nullable=True, comment="最近一次投递失败原因"),
    )
    op.add_column(
        "notification",
        sa.Column(
            "handled_at",
            sa.DateTime(),
            nullable=True,
            comment="待办已处理时间（NULL=未处理）",
        ),
    )


def downgrade() -> None:
    op.drop_column("notification", "handled_at")
    op.drop_column("notification", "last_error")
