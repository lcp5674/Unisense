"""metric 表补原始口径 SQL 列（raw_sql）。

背景：批量注册的指标口径仅以聚合表达式（definition_json.expression）落库，
候选来自 SQL 智能推断，但**整句原始 SQL 不持久化**——创建后无法从 batch_id
溯源候选口径原文（口径核对/审计追溯缺口，生产就绪审查 P2 口径溯源项）。

本迁移为 metric 表补 raw_sql 可空列：
- SQL 批量创建/口径 SQL 模式创建时携带整句原始 SQL（ETL 脚本原文切片）
- 便于详情页/审计从 batch_id 反查候选口径全文，且不改变既有行（存量 raw_sql 为 NULL）

revision 挂 0090_conflict_governance_fields（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0091_metric_raw_sql"
down_revision = "0090_conflict_governance_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "metric",
        sa.Column(
            "raw_sql",
            sa.Text(),
            nullable=True,
            comment="原始口径 SQL（SQL 推断/口径 SQL 模式创建时携带，供 batch_id 溯源）",
        ),
    )


def downgrade() -> None:
    op.drop_column("metric", "raw_sql")
