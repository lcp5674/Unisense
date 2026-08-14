"""db_catalog 新增表级描述列：治理补全的表业务描述（人工/LLM）。

背景（TD §12.1 / FR-18 资产地图）：字段级描述已有独立 ``column_descriptions``
表，但表/视图本身缺业务描述存储。本次在 ``db_catalog`` 直接加列（一行一实体，
统计一条 SQL 即可聚合覆盖率），优先级链 manual > llm > 无。

采集 upsert 显式设置既有字段，不会覆盖这些新列。

可逆：downgrade DROP 新增列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_table_descriptions"
down_revision = "0040_data_source_enabled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "db_catalog",
        sa.Column("description", sa.Text(), nullable=True, comment="表级业务描述（治理补全）"),
    )
    op.add_column(
        "db_catalog",
        sa.Column(
            "description_source",
            sa.Enum("manual", "llm", "schema", name="description_source_enum"),
            nullable=True,
            comment="表级描述来源",
        ),
    )
    op.add_column(
        "db_catalog",
        sa.Column(
            "description_updated_by",
            sa.BigInteger(),
            sa.ForeignKey("user.id", name="fk_db_catalog_desc_user"),
            nullable=True,
            comment="表级描述编辑者（LLM 推断为 NULL）",
        ),
    )
    op.add_column(
        "db_catalog",
        sa.Column(
            "description_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="表级描述更新时间（UTC）",
        ),
    )


def downgrade() -> None:
    op.drop_column("db_catalog", "description_updated_at")
    op.drop_column("db_catalog", "description_updated_by")
    op.drop_column("db_catalog", "description_source")
    op.drop_column("db_catalog", "description")
