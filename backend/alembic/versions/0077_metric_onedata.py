"""metric 表 OneData 重构：粒度下沉 + 逻辑度量引用。

背景（界限文档 §2.3 第 3 条）：粒度属挂载层，不进指标定义——granularity 从 metric
下沉到 metric_mount（0076）。同时原子指标不再绑物理表，改为引用逻辑度量目录
（measure_catalog，0075）：新增 metric.measure_id 可空 FK。

处置：
- metric.granularity 改为 nullable（存量数据保留；派生指标创建时由挂载冗余回填，
  列表/排序/展示零改动；新口径以 metric_mount.granularity 为准）
- metric 新增 measure_id 可空 FK → measure_catalog.id（原子指标必填，派生/复合继承）

revision 挂 0076_metric_mount（当前线性链后继）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0077_metric_onedata"
down_revision = "0076_metric_mount"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "metric",
        "granularity",
        existing_type=sa.String(length=64),
        nullable=True,
        existing_comment="粒度",
        comment="粒度（已下沉挂载实体 metric_mount，保留兼容）",
    )
    op.add_column(
        "metric",
        sa.Column(
            "measure_id",
            sa.BigInteger(),
            nullable=True,
            comment="关联逻辑度量 ID（原子指标必填，派生/复合继承可空）",
        ),
    )
    op.create_foreign_key(
        "fk_metric_measure", "metric", "measure_catalog", ["measure_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_metric_measure", "metric", type_="foreignkey")
    op.drop_column("metric", "measure_id")
    op.alter_column(
        "metric",
        "granularity",
        existing_type=sa.String(length=64),
        nullable=False,
        existing_comment="粒度（已下沉挂载实体 metric_mount，保留兼容）",
        comment="粒度",
    )
