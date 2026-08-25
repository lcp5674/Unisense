"""data_source 类型枚举扩展：新增 hive_metastore 类型（TD §12.1 / FR-002）。

背景：Hive Metastore 采集器（connectors/hive_metastore.py，@registry.register("hive_metastore")）
与前端类型列表均已支持该类型，但共享枚举 ``app.models.enums.SourceTypeEnum`` 遗漏了
``hive_metastore``，导致 ``data_source.source_type`` 的 MySQL ENUM（source_type_enum）
未含该值——Pydantic 校验层对所有用 ``SourceType`` 的请求（test-connection/databases/
tables/create/update）直接 422，即使绕过校验，写入也会抛 ``Data truncated for column
'source_type'``（MySQL 1265）→ 500。

本迁移将 source_type_enum 扩展为 9 值，与 ``app.models.enums.SourceTypeEnum`` 完全对齐。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0086_data_source_hive_metastore_type"
down_revision = "0085_metric_template_onedata"
branch_labels = None
depends_on = None

#: 与 app.models.enums.SourceTypeEnum 值集完全一致（9 种生产类型）。
_SOURCE_TYPES = (
    "mysql",
    "postgres",
    "hive",
    "hive_metastore",
    "spark",
    "doris",
    "clickhouse",
    "kafka",
    "starrocks",
)

#: 迁移前的 8 值枚举（downgrade 回退目标）。
_OLD_SOURCE_TYPES = (
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
        existing_type=sa.Enum(*_OLD_SOURCE_TYPES, name="source_type_enum"),
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
        type_=sa.Enum(*_OLD_SOURCE_TYPES, name="source_type_enum"),
        existing_nullable=False,
    )
