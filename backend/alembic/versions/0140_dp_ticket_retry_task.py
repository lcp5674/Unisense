"""dp 待抉择单 LLM 重试后台任务表（方案 A：异步任务化）。

背景：dp 血缘同步「待抉择」的 LLM 重试原为**同步 HTTP 请求**——``POST
/tickets/retry-llm`` 在请求内逐张串行调 LLM（实测 241s/批），前端阻塞等待期间
切换到其他页面即「看不到进度与结果」。本迁移新增 ``dp_ticket_retry_task``：
LLM 重试改为提交后台任务（arq worker 逐张执行），任务与逐张进度落库，经
右下角任务中心跨页面可见/可取消——与采集目录批量推断（``batch_llm_infer_task``，
方案 B）同一套用户体感。

任务与进度字段语义对齐 batch_llm_infer_task（任务中心双源复用）：
- ``tickets_json``：候选单快照 [{ticket_id, task_name, out_table, status}]
- ``progress_json``：逐张进度 [{ticket_id, task_name, out_table, status
  (pending/running/done/error/cancelled), action, summary, detail}]
- ``status``: pending/running/completed/cancelled/failed
- ``counts_json``: {auto_resolved, refreshed, kept, failed} 终态语义计数
- ``cancel_requested``: 用户请求取消标记（worker 每张完成检查后收敛终态）
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME, JSON

revision = "0140_dp_ticket_retry_task"
down_revision = "0139_dp_run_log_scan_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dp_ticket_retry_task",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("actor_id", sa.Integer(), nullable=True, comment="发起人 ID"),
        sa.Column("actor_name", sa.String(64), nullable=True, comment="发起人姓名快照"),
        sa.Column("org_id", sa.Integer(), nullable=True, comment="发起人组织 ID"),
        sa.Column("tickets_json", JSON(), nullable=False, comment="候选单快照 [{ticket_id, task_name, out_table, status}]"),
        sa.Column("progress_json", JSON(), nullable=False, comment="逐张进度（worker 实时更新）"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", comment="pending/running/completed/cancelled/failed"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0", comment="候选单数"),
        sa.Column("done", sa.Integer(), nullable=False, server_default="0", comment="成功动作单数"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0", comment="失败单数"),
        sa.Column("cancelled", sa.Integer(), nullable=False, server_default="0", comment="取消单数"),
        sa.Column("counts_json", JSON(), nullable=False, comment="终态语义计数 {auto_resolved, refreshed, kept, failed}"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("0"), comment="用户请求取消标记"),
        sa.Column("error", sa.String(512), nullable=True, comment="任务级失败原因（逐单失败不置）"),
        sa.Column("created_at", DATETIME(), nullable=False),
        sa.Column("updated_at", DATETIME(), nullable=False),
        sa.Column("started_at", DATETIME(), nullable=True, comment="任务开始执行时间"),
        sa.Column("finished_at", DATETIME(), nullable=True, comment="任务结束时间"),
        sa.Column("deleted_at", DATETIME(), nullable=True),
        sa.Index("idx_dp_retry_task_actor_created", "actor_id", "created_at"),
        sa.Index("idx_dp_retry_task_status", "status"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )


def downgrade() -> None:
    op.drop_table("dp_ticket_retry_task")
