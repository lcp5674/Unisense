"""notify 服务迁移：通知记录 / 事件日志 / 订阅偏好（TD §12.9 / FR-16 / FR-17）。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0010_notify"
down_revision = "0009_dimension"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True, comment="主键 ID"),
        sa.Column("subscriber_id", sa.BigInteger(), nullable=False, comment="订阅人 ID"),
        sa.Column(
            "channel",
            sa.Enum("EMAIL", "SMS", "WEBHOOK", "IN_APP", name="notify_channel"),
            nullable=False,
            comment="通知渠道",
        ),
        sa.Column("template_code", sa.String(64), nullable=True, comment="模板编码"),
        sa.Column("title", sa.String(255), nullable=False, comment="标题"),
        sa.Column("body", sa.Text(), nullable=True, comment="正文"),
        sa.Column("payload", mysql.JSON(), nullable=True, comment="扩展负载"),
        sa.Column(
            "status",
            sa.Enum("PENDING", "SENT", "FAILED", name="notify_status"),
            nullable=False,
            server_default="PENDING",
            comment="状态",
        ),
        sa.Column("send_at", sa.DateTime(), nullable=True, comment="计划发送时间"),
        sa.Column("sent_at", sa.DateTime(), nullable=True, comment="实际发送时间"),
        sa.Column("ref_type", sa.String(64), nullable=True, comment="关联类型"),
        sa.Column("ref_id", sa.BigInteger(), nullable=True, comment="关联 ID"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_notification_subscriber", "notification", ["subscriber_id"])
    op.create_index("ix_notification_status", "notification", ["status"])

    op.create_table(
        "event_log",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True, comment="主键 ID"),
        sa.Column("event_type", sa.String(64), nullable=False, comment="事件类型"),
        sa.Column("source", sa.String(64), nullable=True, comment="事件来源"),
        sa.Column("payload", mysql.JSON(), nullable=True, comment="事件负载"),
        sa.Column(
            "level",
            sa.Enum("INFO", "WARN", "ERROR", name="event_level"),
            nullable=False,
            server_default="INFO",
            comment="事件级别",
        ),
        sa.Column(
            "notified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="是否已通知",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_event_log_type", "event_log", ["event_type"])

    op.create_table(
        "subscription_pref",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True, comment="主键 ID"),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="用户 ID"),
        sa.Column(
            "channel",
            sa.Enum("EMAIL", "SMS", "WEBHOOK", "IN_APP", name="notify_channel"),
            nullable=False,
            comment="通知渠道",
        ),
        sa.Column("event_type", sa.String(64), nullable=False, comment="事件类型"),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="是否启用",
        ),
        sa.Column("threshold", sa.Integer(), nullable=True, comment="阈值"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "channel", "event_type", name="uk_sub_pref"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_subscription_pref_user", "subscription_pref", ["user_id"])


def downgrade() -> None:
    op.drop_table("subscription_pref")
    op.drop_table("event_log")
    op.drop_table("notification")
