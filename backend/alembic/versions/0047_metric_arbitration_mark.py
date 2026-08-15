"""metric 增补仲裁裁决标记（arbitration_mark，TD §12.4）。

背景：冲突仲裁后需在指标上留下裁决结果（胜方「权威口径」/ 保留差异「已裁定共存」），
供指标详情页展示、并让消费方一眼识别权威口径。此前仲裁只写 conflict 表结论，
指标本身无任何裁决痕迹，落败口径仍以原名被消费（口径分裂未根治）。
本迁移为 metric 表加 arbitration_mark JSON 列（可空），配合仲裁联动写入。

可逆：downgrade 删除该列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0047_metric_arbitration_mark"
down_revision = "0046_notification_read_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.add_column(
        "metric",
        sa.Column(
            "arbitration_mark",
            sa.JSON(),
            nullable=True,
            comment="仲裁裁决标记（canonical/coexist）",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.drop_column("metric", "arbitration_mark")
