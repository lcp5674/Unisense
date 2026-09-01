"""修复维度值管理三表缺失审计列（updated_at/deleted_at）。

背景：0120 建表时仅写了 created_at，而三个模型类继承 ``BaseModel(TimestampMixin,
SoftDeleteMixin)``，要求 ``created_at + updated_at + deleted_at`` 三列。缺少
updated_at/deleted_at 会导致 ORM 全列查询报 ``Unknown column`` 500（一致性检查
``check_schema_consistency.py`` 抓到 6 个 FAIL）。

本迁移为三表各补 ``updated_at``（NOT NULL + server_default now()，存量行自动回填
当前时间）与 ``deleted_at``（可空软删列），与 ``metric`` 等既有表的审计列语义一致。

幂等：add_column 由 alembic 版本表保证只执行一次；downgrade 删除两列不触碰业务数据。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0121_dimension_snapshot_audit_columns"
down_revision = "0120_dimension_value_management"
branch_labels = None
depends_on = None

_TABLES = (
    "dimension_value_snapshot",
    "dimension_snapshot_run",
    "dimension_mapping_value",
)


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
                comment="更新时间（UTC）",
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "deleted_at",
                sa.DateTime(),
                nullable=True,
                comment="软删除时间（UTC），NULL 表示未删除",
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "deleted_at")
        op.drop_column(table, "updated_at")
