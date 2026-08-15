"""metric_template 表新增 owner_id 责任人列。

背景（总览仪表 Owner 责任分布跨资产）：
- Owner 责任分布原仅聚合指标，产品上应覆盖各类数据资产。
- 指标模板此前只有 created_by（创建人）无责任人（Owner），
  导致模板无法纳入 Owner 责任统计，也无"模板由谁负责"的业务语义。
- 新增可空 owner_id（外键 user.id），存量模板置 NULL（无责任人），
  由资产工作台/模板表单后续补齐。

可逆：downgrade 删除 owner_id 列与索引。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0051_metric_template_owner"
down_revision = "0050_lineage_ingest_run_detail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "metric_template",
        sa.Column(
            "owner_id",
            sa.BigInteger(),
            nullable=True,
            comment="责任人（Owner）ID",
        ),
    )
    op.create_index("idx_template_owner", "metric_template", ["owner_id"], unique=False)
    op.create_foreign_key(
        "fk_template_owner", "metric_template", "user", ["owner_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_template_owner", "metric_template", type_="foreignkey")
    op.drop_index("idx_template_owner", table_name="metric_template")
    op.drop_column("metric_template", "owner_id")
