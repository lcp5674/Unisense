"""lineage_edge / lineage_edge_history 边类型枚举新增 BASED_ON（派生↔基础原子基础边）

Revision ID: 0104_lineage_edge_based_on
Revises: 0103_metric_name_morpheme_dict_expand
Create Date: 2026-08-27

背景：OneData（DEV_GUIDE §7a）派生指标 = 基础原子指标 + 业务限定 + 时间周期。
注册派生指标时可绑定 ``definition_json.base_atomic``（基础原子指标编码），血缘
注册生成 ``metric:{base} → metric:{code}`` 的 ``BASED_ON`` 边，与普通 ``DERIVED_FROM``
上游引用区分（血缘图可识别"哪个原子是指标的基底"）。

方案：lineage_edge / lineage_edge_history 的 ``lineage_edge_type`` ENUM 新增
``BASED_ON``（MySQL 原生 ENUM，ALTER MODIFY 全量重写枚举；历史表同步）。
"""

from alembic import op

revision = "0104_lineage_edge_based_on"
down_revision = "0103_metric_name_morpheme_dict_expand"  # 0103 的 revision 是长名（对齐既有教训）
branch_labels = None
depends_on = None

#: 加 BASED_ON 后的完整边类型枚举
_NEW_ENUM = (
    "('DERIVED_FROM','LINEAGE_UP','LINEAGE_DOWN','CONSUMED_BY',"
    "'EXTERNAL_BREAK','USES_DIMENSION','READS_COLUMN','BASED_ON')"
)
#: 原枚举（downgrade 恢复）
_OLD_ENUM = (
    "('DERIVED_FROM','LINEAGE_UP','LINEAGE_DOWN','CONSUMED_BY',"
    "'EXTERNAL_BREAK','USES_DIMENSION','READS_COLUMN')"
)


def upgrade() -> None:
    for table in ("lineage_edge", "lineage_edge_history"):
        op.execute(
            f"ALTER TABLE {table} "
            f"MODIFY COLUMN edge_type ENUM{_NEW_ENUM} "
            f"NOT NULL COMMENT '血缘边类型'"
        )


def downgrade() -> None:
    for table in ("lineage_edge", "lineage_edge_history"):
        op.execute(
            f"ALTER TABLE {table} "
            f"MODIFY COLUMN edge_type ENUM{_OLD_ENUM} "
            f"NOT NULL COMMENT '血缘边类型'"
        )
