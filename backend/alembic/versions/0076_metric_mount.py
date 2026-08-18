"""指标挂载实体表 metric_mount（OneData 挂载层）。

背景：粒度由挂载表决定、不进指标定义（界限文档 §2.3 第 3 条 / §6）。原子指标不挂
物理表，挂载只出现在派生指标上（派生 = 原子 + 时间 + 业务限定 + 挂载）。
本表承载源表/源列/粒度/默认统计周期/业务域，granularity 从 metric 下沉到此。
一个派生指标一个挂载点（uk_mount_metric 唯一约束，首期语义）。

revision 挂 0075_measure_catalog（当前线性链后继）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0076_metric_mount"
down_revision = "0075_measure_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metric_mount",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("metric_id", sa.BigInteger(), nullable=False, comment="所属指标 ID（派生指标）"),
        sa.Column("source_table", sa.String(length=255), nullable=False, comment="源表"),
        sa.Column("source_column", sa.String(length=255), nullable=False, comment="度量列"),
        sa.Column("granularity", sa.String(length=64), nullable=False, comment="粒度"),
        sa.Column(
            "default_period",
            sa.String(length=32),
            nullable=True,
            comment="默认统计周期（day/month/quarter…）",
        ),
        sa.Column("domain", sa.String(length=64), nullable=False, comment="业务域"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("metric_id", name="uk_mount_metric"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        comment="指标挂载实体（OneData 挂载层）",
    )
    op.create_foreign_key("fk_mount_metric", "metric_mount", "metric", ["metric_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_mount_metric", "metric_mount", type_="foreignkey")
    op.drop_table("metric_mount")
