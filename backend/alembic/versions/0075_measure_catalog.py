"""逻辑度量目录表 measure_catalog（OneData 原子层）。

背景：原子指标 = 逻辑度量 + 基础统计粒度（日），不绑定物理表/粒度（界限文档 §2.1 / §2.3 第 3 条）。
逻辑度量承载"度量格式/默认单位/默认小数位/源头系统/同义词"（PRD FR-02-08），
供原子指标继承——One Metric 一处定义多处复用。状态机 DRAFT/PUBLISHED/DEPRECATED
（照 dimension 发布式主数据）。

revision 挂 0074_metric_responsible_owners（当前线性链后继）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0075_measure_catalog"
down_revision = "0074_metric_responsible_owners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "measure_catalog",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("measure_code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "measure_format",
            mysql.ENUM("AMOUNT", "RATIO", "NUMERIC"),
            nullable=False,
            comment="度量格式（AMOUNT/RATIO/NUMERIC）",
        ),
        sa.Column(
            "default_unit",
            sa.String(length=32),
            nullable=False,
            comment="默认单位（金额:元/比率:小数/数值:自定义）",
        ),
        sa.Column(
            "default_decimal_places",
            sa.Integer(),
            nullable=True,
            comment="默认小数位数（金额2/比率4/数值按需，NULL=未定）",
        ),
        sa.Column("source_system", mysql.JSON(), nullable=True, comment="源头系统"),
        sa.Column("synonyms", mysql.JSON(), nullable=True, comment="同义词"),
        sa.Column("domain", sa.String(length=64), nullable=False, comment="业务域"),
        sa.Column("owner_id", sa.BigInteger(), nullable=False, comment="负责人 ID"),
        sa.Column(
            "status",
            mysql.ENUM("DRAFT", "PUBLISHED", "DEPRECATED"),
            nullable=False,
            comment="状态（DRAFT/PUBLISHED/DEPRECATED）",
        ),
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
        sa.UniqueConstraint("measure_code", name="uq_measure_code"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        comment="逻辑度量目录（OneData 原子层）",
    )
    op.create_index("idx_measure_domain", "measure_catalog", ["domain"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_measure_domain", table_name="measure_catalog")
    op.drop_table("measure_catalog")
