"""固化临时 SQL 修补：tracking_event 表 + metric_template.deleted_at 列（TD §12.10 / FR-16 / US9）。

背景：历史上 tracking_event 表与 metric_template.deleted_at 列仅通过对运行中库的
临时 SQL 修补添加，未固化到迁移链。全新环境执行 ``alembic upgrade head`` 后，
recommend 协同过滤（依赖 tracking_event 表）与语义模板查询（依赖 deleted_at 列）
会直接报错（``Table 'tracking_event' doesn't exist`` / ``Unknown column
'metric_template.deleted_at'``）。

本迁移将两处修补固化到迁移链，并对已存在的库幂等（对象已存在则跳过），
确保全新环境与存量环境行为一致。

对齐模型：
- ``app.models.tracking.TrackingEvent``（__tablename__ = "tracking_event"，不含软删除列）
- ``app.models.base.SoftDeleteMixin``（metric_template.deleted_at，DateTime(timezone=True) NULL）

可回滚：downgrade 删除 deleted_at 列并 drop tracking_event 表（含索引）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.mysql import DATETIME

revision = "0023_tracking_softdelete"
down_revision = "0022_notify_channel_enum"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _column_exists(table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def _index_exists(table: str, index: str) -> bool:
    return index in {ix["name"] for ix in inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    # 1) tracking_event 表（对齐 app/models/tracking.py TrackingEvent）
    if not _table_exists("tracking_event"):
        op.create_table(
            "tracking_event",
            sa.Column("id", sa.String(36), primary_key=True, comment="UUID 主键"),
            sa.Column(
                "event_type",
                sa.String(32),
                nullable=False,
                comment="事件类型(search/query/approve/browse/nps)",
            ),
            sa.Column("actor_id", sa.String(36), nullable=False, comment="操作人 ID"),
            sa.Column("target_id", sa.String(36), nullable=True, comment="目标对象 ID(指标/术语等)"),
            sa.Column("target_type", sa.String(32), nullable=True, comment="目标类型(metric/term/glossary)"),
            sa.Column(
                "context_json",
                sa.JSON(),
                nullable=True,
                comment="事件上下文(搜索关键词/查询参数等)",
            ),
            sa.Column(
                "created_at",
                DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
                comment="事件时间",
            ),
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_unicode_ci",
            comment="埋点事件",
        )
        op.create_index(
            "ix_tracking_event_type_created",
            "tracking_event",
            ["event_type", "created_at"],
        )
        op.create_index(
            "ix_tracking_actor_type",
            "tracking_event",
            ["actor_id", "event_type"],
        )

    # 2) metric_template.deleted_at（对齐 app/models/base.py SoftDeleteMixin）
    if not _column_exists("metric_template", "deleted_at"):
        op.add_column(
            "metric_template",
            sa.Column(
                "deleted_at",
                sa.DateTime(timezone=True),
                nullable=True,
                comment="软删除时间（UTC），NULL 表示未删除",
            ),
        )


def downgrade() -> None:
    if _column_exists("metric_template", "deleted_at"):
        op.drop_column("metric_template", "deleted_at")
    if _table_exists("tracking_event"):
        if _index_exists("tracking_event", "ix_tracking_actor_type"):
            op.drop_index("ix_tracking_actor_type", table_name="tracking_event")
        if _index_exists("tracking_event", "ix_tracking_event_type_created"):
            op.drop_index("ix_tracking_event_type_created", table_name="tracking_event")
        op.drop_table("tracking_event")
