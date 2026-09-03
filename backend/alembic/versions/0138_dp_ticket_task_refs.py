"""dp_resolution_ticket 增加 task_refs_json（裁决还原完整任务/节点元数据）。

背景（P2-9 #12）：裁决记忆复用路径此前只用 ticket 的 task_id/task_name/out_table
构造 task/step，build_task_ref 产出 ref 缺 director/cycle 等准静态字段——裁决入库
的边 dp_task_refs 元数据不完整（与首次入库不一致）。ticket 建单时快照完整 ref，
裁决时还原。

Revision ID: 0138
Revises: 0137
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dp_resolution_ticket",
        sa.Column("task_refs_json", sa.JSON(), nullable=True, comment="建单时任务/节点静态身份快照"),
    )


def downgrade() -> None:
    op.drop_column("dp_resolution_ticket", "task_refs_json")
