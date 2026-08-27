"""metric_mount 多挂载：business_filter 列 + 放开 uk_mount_metric 唯一约束

Revision ID: 0105_metric_mount_multi_mount
Revises: 0104_lineage_edge_based_on
Create Date: 2026-08-27

背景：OneData 变体承接（DEV_GUIDE §7a）——派生指标一次创建可挂多个变体
（不同粒度/业务限定/周期组合，即多条 metric_mount 行）。此前 uk_mount_metric
唯一约束限定一指标一挂载（首期语义），现放开为普通索引：

- 加 ``business_filter`` 列（变体级业务限定，如 病种=门特；缺省继承指标级
  definition_json.business_filter）；
- 删除 ``uk_mount_metric`` 唯一约束 → 普通索引 ``idx_mount_metric(metric_id)``；
  存量 1:1 数据天然兼容（N=1 是 N 挂载的特例），无需数据迁移。

MySQL 语义与幂等性：
- 唯一约束即唯一索引；InnoDB 外键 ``fk_mount_metric`` 依赖 ``metric_id`` 上的索引，
  必须先建普通索引顶替唯一索引的角色再 drop 唯一约束（否则 1553）。
- MySQL DDL 隐式提交：本迁移曾因 drop 顺序在旧实现下失败留下半应用态
  （business_filter 已加、唯一索引未删、alembic_version 未推进），故本迁移
  全部操作幂等（存在/不存在判断），干净库与半应用态均能正确收敛。

downgrade 恢复唯一约束（存量若已有重复 metric_id 将失败，由运维显式处理——
本迁移只保证正向兼容多挂载）。
"""

from alembic import op
import sqlalchemy as sa

revision = "0105_metric_mount_multi_mount"
down_revision = "0104_lineage_edge_based_on"  # 0104 的 revision 是长名（对齐既有教训）
branch_labels = None
depends_on = None


def _column_exists(bind: sa.Connection, table: str, column: str) -> bool:
    """幂等：判断列是否已存在（MySQL DDL 隐式提交致半应用态自愈）。"""
    rows = bind.exec_driver_sql(
        "SELECT 1 FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table, column),
    ).fetchall()
    return bool(rows)


def _index_exists(bind: sa.Connection, table: str, index: str) -> bool:
    rows = bind.exec_driver_sql(
        "SELECT 1 FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s",
        (table, index),
    ).fetchall()
    return bool(rows)


def upgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "metric_mount", "business_filter"):
        op.add_column(
            "metric_mount",
            sa.Column(
                "business_filter",
                sa.String(length=512),
                nullable=True,
                comment="业务限定（变体级，如 病种=门特；缺省继承指标级）",
            ),
        )
    # 放开唯一约束：一指标多挂载（多变体）。
    # 顺序：先建普通索引 idx_mount_metric 顶替唯一索引（外键 fk_mount_metric 需要
    # metric_id 上的索引），再 drop 唯一约束（否则 1553: Cannot drop index needed
    # in a foreign key constraint）。
    if not _index_exists(bind, "metric_mount", "idx_mount_metric"):
        op.create_index("idx_mount_metric", "metric_mount", ["metric_id"], unique=False)
    if _index_exists(bind, "metric_mount", "uk_mount_metric"):
        op.drop_constraint("uk_mount_metric", "metric_mount", type_="unique")


def downgrade() -> None:
    bind = op.get_bind()
    # 反向：先恢复唯一约束（作为外键索引），再 drop 普通索引
    if not _index_exists(bind, "metric_mount", "uk_mount_metric"):
        op.create_unique_constraint("uk_mount_metric", "metric_mount", ["metric_id"])
    if _index_exists(bind, "metric_mount", "idx_mount_metric"):
        op.drop_index("idx_mount_metric", table_name="metric_mount")
    if _column_exists(bind, "metric_mount", "business_filter"):
        op.drop_column("metric_mount", "business_filter")
