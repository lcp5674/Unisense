"""维度值管理工业化——引用型快照 + 值级映射（TD §12.15 扩展）。

背景（2026-09 用户反馈）：维度值可能是一张维度表里的列值（客户/商品/医生等
百万级），而非几个枚举值；现有 ``dimension_member`` 纯枚举模型在大基数场景
物化爆炸、人工维护成本高。本迁移引入：

1. ``dimension`` 加引用型字段：source_id/source_table/source_column/sync_mode/
   refresh_interval_hours/last_snapshot_at（sync_mode=snapshot 时值集合来自
   维度表列快照，不写 member 表）。
2. 新表 ``dimension_value_snapshot``：版本化快照（uk(dim_code, snapshot_at, value)，
   保留最近 2 批，diff=两批集合差；REMOVED 标记上批有本批消失的值）。
3. 新表 ``dimension_snapshot_run``：每次刷新的 total/added/removed/null_rate 统计
   与差异样本（空值率检测需 COUNT(*) vs COUNT(col) 两次计数）。
4. 新表 ``dimension_mapping_value``：值级映射（source_value → target_value 逐值
   对应，供 translate_value 翻译服务消费；expression 仅人工参考）。

幂等：add_column/create_table 由 alembic 版本表保证只执行一次；downgrade 删除
新增列与三张表（不触碰既有 dimension/dimension_member/dimension_mapping 数据）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0120_dimension_value_management"
down_revision = "0119_metric_term_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. dimension 加引用型字段（幂等：add_column 只执行一次）
    op.add_column(
        "dimension",
        sa.Column("source_id", sa.String(128), nullable=True, comment="数据源 ID（引用型值来源）"),
    )
    op.add_column(
        "dimension",
        sa.Column("source_table", sa.String(256), nullable=True, comment="维度值来源表"),
    )
    op.add_column(
        "dimension",
        sa.Column("source_column", sa.String(256), nullable=True, comment="维度值来源列"),
    )
    op.add_column(
        "dimension",
        sa.Column(
            "sync_mode",
            sa.Enum("none", "snapshot", name="sync_mode_enum"),
            nullable=False,
            server_default="none",
            comment="值来源模式（none 枚举型 / snapshot 引用型）",
        ),
    )
    op.add_column(
        "dimension",
        sa.Column("refresh_interval_hours", sa.Integer(), nullable=True, comment="快照刷新间隔（小时）"),
    )
    op.add_column(
        "dimension",
        sa.Column("last_snapshot_at", sa.DateTime(), nullable=True, comment="最近一次快照时间"),
    )

    # 2. 维度值快照表（版本化，uk 支持 diff）
    op.create_table(
        "dimension_value_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("dim_code", sa.String(64), nullable=False, comment="维度编码"),
        sa.Column("source_id", sa.String(128), nullable=False, comment="数据源 ID"),
        sa.Column("source_table", sa.String(256), nullable=False, comment="来源表"),
        sa.Column("source_column", sa.String(256), nullable=False, comment="来源列"),
        sa.Column("value", sa.String(512), nullable=False, comment="维度值"),
        sa.Column("snapshot_at", sa.DateTime(), nullable=False, comment="快照批次时间"),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "REMOVED", name="snapshot_status_enum"),
            nullable=False,
            server_default="ACTIVE",
            comment="ACTIVE 当前批 / REMOVED 上批有本批消失",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "dim_code", "snapshot_at", "value", name="uk_dim_snapshot_value"
        ),
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_dim_snapshot_dim_at", "dimension_value_snapshot", ["dim_code", "snapshot_at"]
    )

    # 3. 快照刷新运行记录表
    op.create_table(
        "dimension_snapshot_run",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("dim_code", sa.String(64), nullable=False, comment="维度编码"),
        sa.Column("snapshot_at", sa.DateTime(), nullable=False, comment="快照批次时间"),
        sa.Column(
            "status",
            sa.Enum("RUNNING", "SUCCESS", "FAILED", name="snapshot_run_status_enum"),
            nullable=False,
            server_default="RUNNING",
            comment="运行状态",
        ),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0", comment="值总数"),
        sa.Column("added_count", sa.Integer(), nullable=False, server_default="0", comment="新增值数"),
        sa.Column("removed_count", sa.Integer(), nullable=False, server_default="0", comment="消失值数"),
        sa.Column("null_count", sa.Integer(), nullable=False, server_default="0", comment="空值数"),
        sa.Column("null_rate", sa.Numeric(5, 4), nullable=True, comment="空值率"),
        sa.Column("added_sample", sa.JSON(), nullable=True, comment="新增值样本"),
        sa.Column("removed_sample", sa.JSON(), nullable=True, comment="消失值样本"),
        sa.Column("error_msg", sa.Text(), nullable=True, comment="失败原因"),
        sa.Column("duration_ms", sa.Integer(), nullable=True, comment="耗时（毫秒）"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_dim_snapshot_run_dim", "dimension_snapshot_run", ["dim_code", "snapshot_at"])

    # 4. 值级映射表
    op.create_table(
        "dimension_mapping_value",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("mapping_id", sa.BigInteger(), nullable=False, comment="映射 ID"),
        sa.Column("source_value", sa.String(512), nullable=False, comment="源值"),
        sa.Column("target_value", sa.String(512), nullable=False, comment="目标值"),
        sa.Column("created_by", sa.BigInteger(), nullable=False, comment="创建人 ID"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "mapping_id", "source_value", name="uk_dim_mapping_value"
        ),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_dim_mapping_value_map", "dimension_mapping_value", ["mapping_id"])


def downgrade() -> None:
    op.drop_index("ix_dim_mapping_value_map", table_name="dimension_mapping_value")
    op.drop_table("dimension_mapping_value")
    op.drop_index("ix_dim_snapshot_run_dim", table_name="dimension_snapshot_run")
    op.drop_table("dimension_snapshot_run")
    op.drop_index("ix_dim_snapshot_dim_at", table_name="dimension_value_snapshot")
    op.drop_table("dimension_value_snapshot")
    op.drop_column("dimension", "last_snapshot_at")
    op.drop_column("dimension", "refresh_interval_hours")
    op.drop_column("dimension", "sync_mode")
    op.drop_column("dimension", "source_column")
    op.drop_column("dimension", "source_table")
    op.drop_column("dimension", "source_id")
