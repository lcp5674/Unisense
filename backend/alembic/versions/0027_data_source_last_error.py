"""data_source 增加 last_error 列（P1-3 健康端点真实错误信息）。

背景：GET /api/v1/data-sources/{source_id}/health 此前返回 ``last_error: null``
（模型无此字段），探活/采集失败的错误信息无处落库，健康状态无法给出可诊断的
失败原因。本迁移为 data_source 增加 ``last_error`` 列（String(512) NULL），
并对已存在的库幂等（列已存在则跳过），确保全新环境与存量环境行为一致。

对齐模型：``app.models.data_source.DataSource.last_error``。

可回滚：downgrade 删除该列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0027_data_source_last_error"
down_revision = "0026"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _column_exists("data_source", "last_error"):
        op.add_column(
            "data_source",
            sa.Column(
                "last_error",
                sa.String(512),
                nullable=True,
                comment="最近一次采集/探活错误信息",
            ),
        )


def downgrade() -> None:
    if _column_exists("data_source", "last_error"):
        op.drop_column("data_source", "last_error")
