"""新增 collection_run 表：一次采集运行的持久化历史记录。

背景（TD §12.1 / 采集记录产品定位修复）：采集任务（job）是 ephemeral 运行时数据
（JobStore：内存 / Redis，终态 7 天 TTL），「采集运行历史」从未落库——采集记录页
此前展示的是 Schema 漂移而非真正的采集历史。本表每次采集（手动/定时、同步/异步）
落一行，状态 RUNNING → COMPLETED/FAILED，含全部关键指标与失败明细，满足审计与
排障可追溯。

可逆：downgrade DROP 表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049_collection_run"
down_revision = "0048_favorite"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_run",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True, comment="主键 ID"),
        sa.Column(
            "source_id",
            sa.String(64),
            sa.ForeignKey("data_source.source_id", name="fk_collection_run_source"),
            nullable=False,
            comment="数据源标识",
        ),
        sa.Column("job_id", sa.String(128), nullable=True, comment="关联异步任务 ID"),
        sa.Column(
            "trigger",
            sa.Enum("manual", "scheduled", name="collection_run_trigger_enum"),
            nullable=False,
            server_default="manual",
            comment="触发方式",
        ),
        sa.Column(
            "mode",
            sa.Enum("FULL", "INCREMENTAL", name="collection_run_mode_enum"),
            nullable=False,
            server_default="FULL",
            comment="请求采集模式",
        ),
        sa.Column("effective_mode", sa.String(16), nullable=True, comment="实际执行模式"),
        sa.Column(
            "status",
            sa.Enum("RUNNING", "COMPLETED", "FAILED", name="collection_run_status_enum"),
            nullable=False,
            server_default="RUNNING",
            comment="运行状态",
        ),
        sa.Column("actor_id", sa.BigInteger(), nullable=True, comment="触发人 ID"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, comment="开始时间"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True, comment="结束时间"),
        sa.Column("scanned", sa.Integer(), nullable=False, server_default="0", comment="扫描实体数"),
        sa.Column("registered", sa.Integer(), nullable=False, server_default="0", comment="注册/更新实体数"),
        sa.Column("pii_registered", sa.Integer(), nullable=False, server_default="0", comment="PII 实体数"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0", comment="失败实体数"),
        sa.Column("drift_count", sa.Integer(), nullable=False, server_default="0", comment="Schema 漂移数"),
        sa.Column("deprecated_count", sa.Integer(), nullable=False, server_default="0", comment="对账废弃数"),
        sa.Column("coverage", sa.Float(), nullable=True, comment="采集后资产覆盖率"),
        sa.Column("error", sa.String(512), nullable=True, comment="失败原因（截断）"),
        sa.Column("detail_json", sa.JSON(), nullable=True, comment="明细"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Index("idx_collection_run_source", "source_id", "started_at"),
        sa.Index("idx_collection_run_status", "status", "started_at"),
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("collection_run")
