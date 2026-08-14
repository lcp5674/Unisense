"""新增 column_descriptions 表：独立字段描述存储（采集不覆盖）。

对齐 TD §4.1 column_descriptions 表定义。
优先级链：manual > llm > schema_json 原始 comment。

可逆：downgrade DROP TABLE column_descriptions。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_column_descriptions"
down_revision = "0035_metric_enum_expand"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "column_descriptions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column(
            "catalog_id",
            sa.BigInteger(),
            sa.ForeignKey("db_catalog.id", name="fk_column_desc_catalog"),
            nullable=False,
            comment="关联目录实体",
        ),
        sa.Column("column_name", sa.String(256), nullable=False, comment="字段名"),
        sa.Column("description", sa.Text(), nullable=False, comment="描述文本"),
        sa.Column(
            "source",
            sa.Enum("manual", "llm", "schema", name="description_source_enum"),
            nullable=False,
            server_default="schema",
            comment="描述来源",
        ),
        sa.Column(
            "updated_by",
            sa.BigInteger(),
            sa.ForeignKey("user.id", name="fk_column_desc_user"),
            nullable=True,
            comment="编辑者用户 ID（LLM 推断时为 NULL）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
            comment="更新时间（UTC）",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
        sa.UniqueConstraint("catalog_id", "column_name", name="uk_column_desc_catalog_col"),
    )
    op.create_index("idx_column_desc_source", "column_descriptions", ["source"])


def downgrade() -> None:
    op.drop_index("idx_column_desc_source", table_name="column_descriptions")
    op.drop_table("column_descriptions")
