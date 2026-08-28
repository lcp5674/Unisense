"""采集运行状态枚举扩展：collection_run.status 增加 CANCELLED（用户主动取消）

Revision ID: 0110_collection_run_status_cancelled
Revises: 0109_sensitivity_enum_unify
Create Date: 2026-08-28

背景（任务状态集合漂移）：
- JobStore（内存/Redis）任务状态含 QUEUED/RUNNING/COMPLETED/FAILED/CANCELLED；
- ``collection_run.status`` 持久表枚举仅 RUNNING/COMPLETED/FAILED——用户主动取消
  的任务（cancel API 已把 JobStore 置 CANCELLED）在运行历史里被标记 FAILED
  （「任务已取消」），语义不精确且与 JobStore 终态无法一一对应。

本迁移为 ``collection_run.status`` 增加 ``CANCELLED`` 枚举值，worker 取消收尾
改标 CANCELLED（而非 FAILED），与 JobStore 终态对齐；purge 清理纳入 CANCELLED。

可逆：downgrade 收回 CANCELLED（需确保无 CANCELLED 存量数据时方可执行）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0110_collection_run_status_cancelled"
down_revision = "0109_sensitivity_enum_unify"
branch_labels = None
depends_on = None

_OLD = ("RUNNING", "COMPLETED", "FAILED")
_NEW = ("RUNNING", "COMPLETED", "FAILED", "CANCELLED")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "collection_run",
        "status",
        existing_type=sa.Enum(*_OLD, name="collection_run_status_enum"),
        type_=sa.Enum(*_NEW, name="collection_run_status_enum"),
        existing_nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "collection_run",
        "status",
        existing_type=sa.Enum(*_NEW, name="collection_run_status_enum"),
        type_=sa.Enum(*_OLD, name="collection_run_status_enum"),
        existing_nullable=False,
    )
