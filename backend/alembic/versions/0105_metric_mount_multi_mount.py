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

MySQL 语义：唯一约束即唯一索引，drop_constraint(type_="unique") 生成
DROP INDEX；downgrade 恢复唯一约束（存量若已有重复 metric_id 将失败，
由运维显式处理——本迁移只保证正向兼容多挂载）。
"""

from alembic import op
import sqlalchemy as sa

revision = "0105_metric_mount_multi_mount"
down_revision = "0104_lineage_edge_based_on"  # 0104 的 revision 是长名（对齐既有教训）
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "metric_mount",
        sa.Column(
            "business_filter",
            sa.String(length=512),
            nullable=True,
            comment="业务限定（变体级，如 病种=门特；缺省继承指标级）",
        ),
    )
    # 放开唯一约束：一指标多挂载（多变体）
    op.drop_constraint("uk_mount_metric", "metric_mount", type_="unique")
    op.create_index("idx_mount_metric", "metric_mount", ["metric_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_mount_metric", table_name="metric_mount")
    op.create_unique_constraint("uk_mount_metric", "metric_mount", ["metric_id"])
    op.drop_column("metric_mount", "business_filter")
