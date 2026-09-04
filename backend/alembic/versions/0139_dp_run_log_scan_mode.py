"""dp_sync_run_log 增加 scan_mode（扫描模式 full/incremental）。

背景：运行记录此前不记录每轮是全量还是增量——周期增量轮无变更时记一条
「success 0/0」，运维视图无法区分「增量空扫（正常）」与「全量扫到 0（异常）」。
补 scan_mode 列（默认 incremental 兼容存量行），scan_once 收尾写 full/incremental，
前端运行记录按模式 Tag 展示并可识别空扫。

Revision ID: 0139
Revises: 0138
Create Date: 2026-09-04
"""

from __future__ import annotations

from alembic import op

revision = "0139_dp_run_log_scan_mode"
down_revision = "0138_dp_ticket_task_refs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MySQL 8 支持一条 ADD COLUMN IF NOT EXISTS（幂等自愈）
    op.execute(
        "ALTER TABLE dp_sync_run_log "
        "ADD COLUMN IF NOT EXISTS scan_mode VARCHAR(16) NOT NULL DEFAULT 'incremental' "
        "COMMENT '扫描模式：full=全量 / incremental=增量'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE dp_sync_run_log "
        "DROP COLUMN IF EXISTS scan_mode"
    )
