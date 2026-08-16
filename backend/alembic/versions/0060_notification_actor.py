"""event_log / notification 表新增操作人字段（actor_id + actor_name）。

背景（TD §12.9 / FR-16）：通知应表达「谁在什么时间对什么对象做了什么、对我有何影响」。
此前 EventLog/Notification 无 actor 字段，事件发布时 EventBus 的 ``actor_id`` 被消费端丢弃，
通知只呈现"发生了什么"的技术视角，缺"谁操作的"。

设计：
- ``actor_id``：BigInteger 可空，事件发起者用户 ID（NULL=系统/定时任务触发）。
- ``actor_name``：String(64) 可空，操作人姓名**快照**——通知生成时物化，
  历史通知不受后续改名影响（与 title/body 物化策略一致）。

可逆：downgrade 删除两表 actor 列（存量通知的 actor 信息随列删除，符合回退语义）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0060_notification_actor"
down_revision = "0059_organization_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event_log",
        sa.Column(
            "actor_id",
            sa.BigInteger(),
            nullable=True,
            comment="操作人 ID（谁发起，NULL=系统）",
        ),
    )
    op.add_column(
        "event_log",
        sa.Column("actor_name", sa.String(length=64), nullable=True, comment="操作人姓名快照"),
    )
    op.add_column(
        "notification",
        sa.Column(
            "actor_id",
            sa.BigInteger(),
            nullable=True,
            comment="操作人 ID（谁发起，NULL=系统）",
        ),
    )
    op.add_column(
        "notification",
        sa.Column("actor_name", sa.String(length=64), nullable=True, comment="操作人姓名快照"),
    )
    op.create_index("ix_event_log_actor_id", "event_log", ["actor_id"])
    op.create_index("ix_notification_actor_id", "notification", ["actor_id"])


def downgrade() -> None:
    op.drop_index("ix_notification_actor_id", table_name="notification")
    op.drop_index("ix_event_log_actor_id", table_name="event_log")
    op.drop_column("notification", "actor_name")
    op.drop_column("notification", "actor_id")
    op.drop_column("event_log", "actor_name")
    op.drop_column("event_log", "actor_id")
