"""指标消费指南来源与编辑元数据（对齐 description_source 模式 TD §12.1）。

背景：``metric.consumption_guide`` 此前仅有 JSON 值，无法区分「人工维护」与
「自动生成」，且指标字段变更后自动生成的指南无法感知来源。本迁移为
``metric`` 增加指南三件套（guide_source / guide_updated_by /
guide_updated_at），与 description_source 完全同构；并对存量已有
consumption_guide 的行回填 guide_source='manual'（存量值多为人工写入或
update_metric 白名单写入，presence 判定须以 DB 值优先，勿错弃）。
可逆：downgrade 删除三列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0093_metric_guide_source"
down_revision = "0092_metric_list_pagination_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.add_column(
        "metric",
        sa.Column(
            "guide_source",
            sa.Enum("auto", "manual", name="guide_source_enum"),
            nullable=False,
            server_default="auto",
            comment="指南来源（auto/manual）",
        ),
    )
    op.add_column(
        "metric",
        sa.Column(
            "guide_updated_by",
            sa.BigInteger(),
            nullable=True,
            comment="指南更新人 ID",
        ),
    )
    op.add_column(
        "metric",
        sa.Column("guide_updated_at", sa.DateTime(), nullable=True, comment="指南更新时间"),
    )
    # 存量回填：已有 consumption_guide 的视为人工维护（presence 判定优先 DB 值）
    op.execute(
        "UPDATE metric SET guide_source='manual' WHERE consumption_guide IS NOT NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.drop_column("metric", "guide_updated_at")
    op.drop_column("metric", "guide_updated_by")
    op.drop_column("metric", "guide_source")
