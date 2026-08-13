"""escalation_record 告警升级状态表（B2 重试/升级 cron 的状态载体）。

背景：告警升级此前仅 ``escalation.triggered`` 事件一次性触达，无状态、无重试、
无法确认。本迁移新增 ``escalation_record`` 表，持久化每次升级的
级别/触达次数/下次重试时刻/确认状态，供周期任务 ``check_escalation_retries``
扫描驱动重试与逐级升级（P2→P1→P0）。

对齐模型：``app.models.escalation.EscalationRecord``。

可回滚：downgrade 删除该表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_escalation_record"
down_revision = "0027_data_source_last_error"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "escalation_record",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("source_ref", sa.String(64), nullable=True),
        sa.Column("level", sa.String(8), nullable=False),
        sa.Column("label", sa.String(16), nullable=False),
        sa.Column("attempts", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("max_attempts", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="ESCALATED"),
        sa.Column("last_payload", sa.JSON(), nullable=True),
        sa.Column("actor_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_escalation_record_event_type", "escalation_record", ["event_type"])
    op.create_index("ix_escalation_record_source_ref", "escalation_record", ["source_ref"])


def downgrade() -> None:
    op.drop_index("ix_escalation_record_source_ref", table_name="escalation_record")
    op.drop_index("ix_escalation_record_event_type", table_name="escalation_record")
    op.drop_table("escalation_record")
