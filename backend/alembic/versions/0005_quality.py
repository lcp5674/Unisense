"""quality rule + event（数据质量，TD §12.8 / FR-10）

Revision ID: 0005_quality
Revises: 0004_governance
Create Date: 2026-08-08

对齐 TD §4.1 quality_rule / quality_event 表。质量规则配置（随指标 PUBLISHED 注册）
+ 质量异常事件（分级 P0/P1/P2，状态机 OPEN→ACK→RESOLVED→CLOSED）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_quality"
down_revision = "0004_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quality_rule",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column("metric_id", sa.BigInteger(), nullable=False, comment="关联指标 ID"),
        sa.Column(
            "rule_type",
            sa.Enum(
                "COMPLETENESS",
                "ACCURACY",
                "TIMELINESS",
                "CONSISTENCY",
                "UNIQUENESS",
                "VALIDITY",
                "WAVE_DIFF",
                "CROSS_SOURCE",
                name="quality_rule_type",
            ),
            nullable=False,
            comment="规则类型",
        ),
        sa.Column("threshold", sa.JSON(), nullable=False, comment="阈值参数"),
        sa.Column(
            "rule_mode",
            sa.Enum(
                "static", "dynamic_baseline", "yoy_woy", "cross_source", name="quality_rule_mode"
            ),
            nullable=False,
            server_default=sa.text("'static'"),
            comment="求值模式",
        ),
        sa.Column(
            "severity",
            sa.Enum("P0", "P1", "P2", name="quality_severity"),
            nullable=False,
            server_default=sa.text("'P2'"),
            comment="严重级",
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("1"), comment="是否启用"
        ),
        sa.Column("notify_targets", sa.JSON(), nullable=True, comment="告警通知目标"),
        sa.Column("created_by", sa.BigInteger(), nullable=False, comment="创建人 ID"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_quality_rule_metric", "quality_rule", ["metric_id"], unique=False)
    op.create_index("idx_quality_rule_type", "quality_rule", ["rule_type"], unique=False)
    op.create_index("idx_quality_rule_sev", "quality_rule", ["severity"], unique=False)

    op.create_table(
        "quality_event",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column("metric_id", sa.BigInteger(), nullable=False, comment="关联指标 ID"),
        sa.Column(
            "level",
            sa.Enum("P0", "P1", "P2", name="quality_severity"),
            nullable=False,
            comment="异常分级",
        ),
        sa.Column(
            "rule_type",
            sa.Enum(
                "COMPLETENESS",
                "ACCURACY",
                "TIMELINESS",
                "CONSISTENCY",
                "UNIQUENESS",
                "VALIDITY",
                "WAVE_DIFF",
                "CROSS_SOURCE",
                name="quality_rule_type",
            ),
            nullable=False,
            comment="触发规则类型",
        ),
        sa.Column("obs_value", sa.Numeric(precision=18, scale=4), nullable=True, comment="观测值"),
        sa.Column("threshold", sa.Numeric(precision=18, scale=4), nullable=True, comment="阈值"),
        sa.Column(
            "status",
            sa.Enum("OPEN", "ACK", "RESOLVED", "CLOSED", name="quality_event_status"),
            nullable=False,
            server_default=sa.text("'OPEN'"),
            comment="事件状态",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_quality_event_metric", "quality_event", ["metric_id"], unique=False)
    op.create_index("idx_quality_event_rule_type", "quality_event", ["rule_type"], unique=False)
    op.create_index("idx_quality_event_status", "quality_event", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_quality_event_status", table_name="quality_event")
    op.drop_index("idx_quality_event_rule_type", table_name="quality_event")
    op.drop_index("idx_quality_event_metric", table_name="quality_event")
    op.drop_table("quality_event")
    op.drop_index("idx_quality_rule_sev", table_name="quality_rule")
    op.drop_index("idx_quality_rule_type", table_name="quality_rule")
    op.drop_index("idx_quality_rule_metric", table_name="quality_rule")
    op.drop_table("quality_rule")
