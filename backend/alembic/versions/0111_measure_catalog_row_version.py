"""逻辑度量目录增加乐观锁行版本 row_version

Revision ID: 0111_measure_catalog_row_version
Revises: 0110_collection_run_status_cancelled
Create Date: 2026-08-28

背景（T5 审查修复）：
- measure_catalog 此前无乐观锁（metric/dimension/glossary 均有 row_version）；
- 挂载更新/并发编辑 last-write-wins，使破坏性字段判定（格式/单位/小数位联动）失真。
本迁移为 measure_catalog 增加 row_version 列（默认 1），service 更新改用乐观锁校验。

可逆：downgrade 删除列（默认值丢失，仅回滚用）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0111_measure_catalog_row_version"
down_revision = "0110_collection_run_status_cancelled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "measure_catalog",
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("measure_catalog", "row_version")
