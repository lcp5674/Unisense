"""维度缓慢变化类型扩展：SCD1/SCD2 → SCD0-SCD6 生产全集（TD §12.15 / FR-05）。

背景：``dimension.type`` MySQL ENUM 列仅含 ``SCD1``/``SCD2``，而生产缓慢变化维
标准还包括 SCD0（原样保留）/SCD3（有限历史）/SCD4（历史表）/SCD6（混合），
前端下拉已展示全量选项，选中后写入会抛 ``Data truncated for column``（MySQL
1265）→ 500。

本迁移将 ``dimension.type`` 扩展为与 ORM ``DimensionType`` 枚举完全一致的值集。
可逆：downgrade 收回新增枚举值（需确保无存量数据使用这些值时方可执行）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_dimension_scd_expand"
down_revision = "0041_table_descriptions"
branch_labels = None
depends_on = None

#: 与 app.models.dimension.DimensionType 完全一致。
_DIM_TYPES = ("SCD0", "SCD1", "SCD2", "SCD3", "SCD4", "SCD6")
_OLD_TYPES = ("SCD1", "SCD2")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "dimension",
        "type",
        existing_type=sa.Enum(*_OLD_TYPES, name="dimension_type"),
        type_=sa.Enum(*_DIM_TYPES, name="dimension_type"),
        existing_nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "dimension",
        "type",
        existing_type=sa.Enum(*_DIM_TYPES, name="dimension_type"),
        type_=sa.Enum(*_OLD_TYPES, name="dimension_type"),
        existing_nullable=False,
    )
