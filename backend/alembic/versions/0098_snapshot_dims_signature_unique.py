"""metric_value_snapshot 加 dims_signature 唯一约束（防同口径重复快照）

Revision ID: 0098
Revises: 0097_audit_actor_id_nullable
Create Date: 2026-08-26

背景：快照 WORM 只写不删，模型注释声称"唯一约束防同口径重复快照"但实际
无任何唯一约束——同指标同版本同区间（dims 不同）查询会重复落快照，表无界
膨胀。JSON dims 无法直接建 MySQL 唯一索引，新增 ``dims_signature`` 确定性
签名列（sorted JSON 摘要）承载唯一键 ``(metric_code, version, date_range,
dims_signature)``。存量行 dims_signature 为 NULL（MySQL 唯一索引允许多个
NULL，不冲突，去重从新写入开始生效）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "metric_value_snapshot",
        sa.Column(
            "dims_signature",
            sa.String(length=64),
            nullable=True,
            comment="维度组合确定性签名（唯一键承载）",
        ),
    )
    op.create_unique_constraint(
        "uk_snapshot_metric_version_range_dims",
        "metric_value_snapshot",
        ["metric_code", "version", "date_range", "dims_signature"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uk_snapshot_metric_version_range_dims", "metric_value_snapshot", type_="unique"
    )
    op.drop_column("metric_value_snapshot", "dims_signature")
