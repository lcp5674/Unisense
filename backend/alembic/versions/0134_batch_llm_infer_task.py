"""跨表批量 LLM 推断后台任务表（方案 B：后端任务化）。

背景：描述缺失治理「批量推断所选表」原为前端有界并发逐表调用同步端点，
进度/结果仅存于组件 state——切换页面后组件卸载，进行中进度与结果不可见。
本迁移新增 ``batch_llm_infer_task``：批量推断改为提交后台任务（arq worker
逐表执行），任务与逐表进度落库，任意页面/刷新后可查询/取消，彻底解决
「切页失明」。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME, JSON

revision = "0134_batch_llm_infer_task"
down_revision = "0133_llm_config_disable_thinking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "batch_llm_infer_task",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("actor_id", sa.Integer(), nullable=True, comment="发起人 ID"),
        sa.Column("actor_name", sa.String(64), nullable=True, comment="发起人姓名快照"),
        sa.Column("org_id", sa.Integer(), nullable=True, comment="发起人组织 ID"),
        sa.Column("tasks_json", JSON(), nullable=False, comment="待处理表 [{catalog_id, entity_name, ...}]"),
        sa.Column("progress_json", JSON(), nullable=False, comment="逐表进度（worker 实时更新）"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", comment="pending/running/completed/cancelled/failed"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0", comment="任务表数"),
        sa.Column("done", sa.Integer(), nullable=False, server_default="0", comment="成功表数"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0", comment="失败表数"),
        sa.Column("cancelled", sa.Integer(), nullable=False, server_default="0", comment="取消表数"),
        sa.Column("added_total", sa.Integer(), nullable=False, server_default="0", comment="新增字段描述总数"),
        sa.Column("concurrency", sa.Integer(), nullable=False, server_default="3", comment="有界并发表数"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("0"), comment="用户请求取消标记"),
        sa.Column("error", sa.String(512), nullable=True, comment="任务级失败原因（逐表失败不置）"),
        sa.Column("created_at", DATETIME(), nullable=False),
        sa.Column("updated_at", DATETIME(), nullable=False),
        sa.Column("started_at", DATETIME(), nullable=True, comment="任务开始执行时间"),
        sa.Column("finished_at", DATETIME(), nullable=True, comment="任务结束时间"),
        sa.Column("deleted_at", DATETIME(), nullable=True),
        sa.Index("idx_batch_task_actor_created", "actor_id", "created_at"),
        sa.Index("idx_batch_task_status", "status"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )


def downgrade() -> None:
    op.drop_table("batch_llm_infer_task")
