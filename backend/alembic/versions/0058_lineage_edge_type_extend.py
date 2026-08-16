"""lineage_edge / lineage_edge_history 的 edge_type 枚举扩展（TD §12.2 / FR-18）。

背景：
- 指标级血缘图谱需支持「指标 ↔ 维度」与「指标 ↔ 字段」两类新关系：
  - USES_DIMENSION：指标基于维度分析（dimension:{code} 节点）
  - READS_COLUMN：指标来源于表的具体字段（column:{db}.{tbl}.{col} 节点）
- 此前枚举仅 5 种（DERIVED_FROM / LINEAGE_UP / LINEAGE_DOWN / CONSUMED_BY /
  EXTERNAL_BREAK），无法表达维度/字段节点，故扩展。

可逆：downgrade 从枚举中移除两个新值（存量 USES_DIMENSION/READS_COLUMN
数据在回退前需清理——本迁移仅改枚举定义，不触碰数据行）。
"""

from __future__ import annotations

from alembic import op

revision = "0058_lineage_edge_type_extend"
down_revision = "0057_feedback_richness"
branch_labels = None
depends_on = None

# 完整的 7 值枚举（保持与 app/models/lineage.py 的 SQLEnum 严格一致）
_FULL_ENUM = (
    "('DERIVED_FROM','LINEAGE_UP','LINEAGE_DOWN','CONSUMED_BY',"
    "'EXTERNAL_BREAK','USES_DIMENSION','READS_COLUMN')"
)
# 回退为 5 值枚举
_BASE_ENUM = (
    "('DERIVED_FROM','LINEAGE_UP','LINEAGE_DOWN','CONSUMED_BY','EXTERNAL_BREAK')"
)

_TABLES = ("lineage_edge", "lineage_edge_history")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    for table in _TABLES:
        op.execute(
            f"ALTER TABLE {table} MODIFY COLUMN edge_type "
            f"ENUM{_FULL_ENUM} NOT NULL COMMENT '血缘边类型'"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    for table in _TABLES:
        op.execute(
            f"ALTER TABLE {table} MODIFY COLUMN edge_type "
            f"ENUM{_BASE_ENUM} NOT NULL COMMENT '血缘边类型'"
        )
