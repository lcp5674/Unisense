"""quality 事件操作人留痕 + ACK 备注（TD §12.8 / FR-10）

新增 quality_event 操作人留痕列（ack_*/resolved_*/closed_*）与 ack_note 备注列，
用于审计异常事件状态转移的责任人，闭环治理可回溯（修复 §6.3 已知 Medium：事件表缺操作人留痕）。

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("quality_event", sa.Column("ack_note", sa.Text(), nullable=True))
    op.add_column("quality_event", sa.Column("ack_by", sa.BigInteger(), nullable=True))
    op.add_column("quality_event", sa.Column("ack_at", sa.DateTime(), nullable=True))
    op.add_column("quality_event", sa.Column("resolved_by", sa.BigInteger(), nullable=True))
    op.add_column("quality_event", sa.Column("resolved_at", sa.DateTime(), nullable=True))
    op.add_column("quality_event", sa.Column("closed_by", sa.BigInteger(), nullable=True))
    op.add_column("quality_event", sa.Column("closed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("quality_event", "closed_at")
    op.drop_column("quality_event", "closed_by")
    op.drop_column("quality_event", "resolved_at")
    op.drop_column("quality_event", "resolved_by")
    op.drop_column("quality_event", "ack_at")
    op.drop_column("quality_event", "ack_by")
    op.drop_column("quality_event", "ack_note")
