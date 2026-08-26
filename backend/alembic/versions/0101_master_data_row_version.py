"""term/dimension 加 row_version 乐观锁列（防主数据编辑并发静默覆盖，P11 C-2）

Revision ID: 0101
Revises: 0100
Create Date: 2026-08-27

背景：维度/术语编辑（update_dimension/update_term）为纯 read-modify-write，
无 CAS——两用户同时编辑同一术语/维度会静默覆盖（指标模块 update_metric 已有
row_version 乐观锁）。新增 ``row_version`` 列承载跨请求乐观锁（编辑回传当前
版本，不一致即 409）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "term",
        sa.Column(
            "row_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="乐观锁版本（编辑回传当前值，不一致即 409 防覆盖）",
        ),
    )
    op.add_column(
        "dimension",
        sa.Column(
            "row_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="乐观锁版本（编辑回传当前值，不一致即 409 防覆盖）",
        ),
    )


def downgrade() -> None:
    op.drop_column("dimension", "row_version")
    op.drop_column("term", "row_version")
