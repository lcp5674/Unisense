"""指标业务描述字段（对齐 DBCatalog 表级描述模式 TD §12.1）。

背景：``metric`` 表此前无业务描述能力，资产地图点击指标仅能跳转到
指标详情，无法在本页补充说明。本迁移为 ``metric`` 增加描述四件套
（description / description_source / description_updated_by /
description_updated_at），与 ``db_catalog`` 表级描述完全同构，供
前端资产地图抽屉展示与编辑。可逆：downgrade 删除四列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043_metric_description"
down_revision = "0042_dimension_scd_expand"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.add_column(
        "metric",
        sa.Column("description", sa.Text(), nullable=True, comment="指标业务描述"),
    )
    op.add_column(
        "metric",
        sa.Column(
            "description_source",
            sa.Enum("manual", "llm", "schema", name="description_source_enum"),
            nullable=True,
            comment="描述来源（manual/llm/schema）",
        ),
    )
    op.add_column(
        "metric",
        sa.Column(
            "description_updated_by",
            sa.BigInteger(),
            nullable=True,
            comment="描述更新人 ID",
        ),
    )
    op.add_column(
        "metric",
        sa.Column("description_updated_at", sa.DateTime(), nullable=True, comment="描述更新时间"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.drop_column("metric", "description_updated_at")
    op.drop_column("metric", "description_updated_by")
    op.drop_column("metric", "description_source")
    op.drop_column("metric", "description")
