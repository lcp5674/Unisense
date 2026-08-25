"""跨表批量 LLM 推断历史表（服务端持久化）。

背景：描述缺失治理「批量推断所选表」此前历史仅存前端 localStorage（单设备易丢、
团队不可见）。新增 batch_infer_history 表：每次批量会话落一行（触发人快照 + 表集 +
成功/失败/取消/新增字段/耗时/失败表），跨设备、团队可追溯，支持历史视图一键重跑。

revision 挂 0086_data_source_hive_metastore_type（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0087_batch_infer_history"
down_revision = "0086_data_source_hive_metastore_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "batch_infer_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "actor_id",
            sa.BigInteger(),
            nullable=True,
            comment="触发人 ID",
        ),
        sa.Column(
            "actor_name",
            sa.String(length=64),
            nullable=True,
            comment="触发人姓名快照",
        ),
        sa.Column(
            "tables_json",
            sa.JSON(),
            nullable=False,
            comment="涉及的表 [{catalog_id, entity_name}]",
        ),
        sa.Column(
            "done",
            sa.Integer(),
            nullable=False,
            default=0,
            comment="成功表数",
        ),
        sa.Column(
            "failed",
            sa.Integer(),
            nullable=False,
            default=0,
            comment="失败表数",
        ),
        sa.Column(
            "cancelled",
            sa.Integer(),
            nullable=False,
            default=0,
            comment="取消表数",
        ),
        sa.Column(
            "added",
            sa.Integer(),
            nullable=False,
            default=0,
            comment="新增字段描述数",
        ),
        sa.Column(
            "elapsed",
            sa.Integer(),
            nullable=False,
            default=0,
            comment="总耗时（秒）",
        ),
        sa.Column(
            "failed_tables_json",
            sa.JSON(),
            nullable=False,
            comment="失败表 [{catalog_id, entity_name}]",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.Index("ix_batch_history_created", "created_at"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        comment="跨表批量 LLM 推断历史（服务端持久化）",
    )


def downgrade() -> None:
    op.drop_table("batch_infer_history")
