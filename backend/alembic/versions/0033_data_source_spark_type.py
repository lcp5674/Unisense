"""data_source 类型枚举扩展：新增 spark 类型（TD §12.1 / FR-002）。

背景：新增 Spark Thrift Server 采集器（connectors/spark.py，@registry.register("spark")），
但 data_source.source_type 的 MySQL ENUM（source_type_enum）未含 spark 值，
写入会抛 ``Data truncated for column 'source_type'``（MySQL 1265）→ 500。

本迁移将 source_type_enum 扩展为 8 值，与 ``app.models.enums.SourceTypeEnum`` 完全对齐。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_data_source_spark_type"
down_revision = "0032_audit_remediation_tables"
branch_labels = None
depends_on = None

#: 与 app.models.enums.SourceTypeEnum 值集完全一致（8 种生产类型）。
_SOURCE_TYPES = (
    "mysql",
    "postgres",
    "hive",
    "spark",
    "doris",
    "clickhouse",
    "kafka",
    "starrocks",
)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect != "mysql":
        # 非 MySQL（如 SQLite 测试库）无需修改，类型在 autogenerate 时自行兼容
        return
    op.alter_column(
        "data_source",
        "source_type",
        existing_type=sa.Enum(
            "mysql",
            "postgres",
            "hive",
            "doris",
            "clickhouse",
            "kafka",
            "starrocks",
            name="source_type_enum",
        ),
        type_=sa.Enum(*_SOURCE_TYPES, name="source_type_enum"),
        existing_nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect != "mysql":
        return
    op.alter_column(
        "data_source",
        "source_type",
        existing_type=sa.Enum(*_SOURCE_TYPES, name="source_type_enum"),
        type_=sa.Enum(
            "mysql",
            "postgres",
            "hive",
            "doris",
            "clickhouse",
            "kafka",
            "starrocks",
            name="source_type_enum",
        ),
        existing_nullable=False,
    )
