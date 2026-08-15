"""lineage_ingest_run 新增 detail_json 列：运行详情快照。

背景（血缘采集通道产品化）：点击「采集通道 → 运行历史」具体行时，此前只能看到
变更摘要计数（新增/更新/未再出现/失效/恢复），无法查看本次运行的具体信息。
本列以 JSON 文本存结构化快照：
- SQL 解析（source=sqlglot）：SQL 原文 / dialect / target_table / source_node /
  actor_id / 本次表级与字段级边明细；
- 批量采集（source=dp_csv 等）：本次新增/更新边的明细列表。

可逆：downgrade 删除该列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050_lineage_ingest_run_detail"
down_revision = "0049_collection_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lineage_ingest_run",
        sa.Column(
            "detail_json",
            sa.Text(),
            nullable=True,
            comment="本次运行详情快照（JSON）：SQL 原文/方言/落点/边明细 或 批量变更边明细",
        ),
    )


def downgrade() -> None:
    op.drop_column("lineage_ingest_run", "detail_json")
