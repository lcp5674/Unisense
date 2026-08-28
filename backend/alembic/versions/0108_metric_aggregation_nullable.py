"""指标聚合方式列可空：派生/复合指标 aggregation 允许 NULL（无聚合语义）

Revision ID: 0108_metric_aggregation_nullable
Revises: 0107_collection_run_log
Create Date: 2026-08-28

背景：派生/复合指标的聚合语义由口径表达式/依赖承载（如客单价 =
ROUND(SUM(amount)/NULLIF(COUNT(user_id),0),2) 整体是除法，非 SUM）。
此前 ``aggregation`` 为 NOT NULL，批量/单条创建派生比率/条件列时后端
以 ``"SUM"`` 占位落库，详情页/目录展示「聚合方式 SUM」语义失真。

本迁移将 ``metric.aggregation`` 改为可空：
- 派生/复合指标（无聚合语义）落 NULL，详情页展示「派生表达式」；
- 原子/普通聚合派生仍填真实枚举值（行为不变）；
- 存量 ``"SUM"`` 占位数据不回写（展示层按类型/占位判定，避免误判真实 SUM）。

可逆：downgrade 收回 nullable（需确保无 NULL 存量数据时方可执行）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0108_metric_aggregation_nullable"
down_revision = "0107_collection_run_log"
branch_labels = None
depends_on = None

_AGG = ("SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", "MAX", "MIN", "MEDIAN", "PERCENTILE")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "metric",
        "aggregation",
        existing_type=sa.Enum(*_AGG, name="agg_type"),
        nullable=True,
        existing_nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "metric",
        "aggregation",
        existing_type=sa.Enum(*_AGG, name="agg_type"),
        nullable=False,
        existing_nullable=True,
    )
