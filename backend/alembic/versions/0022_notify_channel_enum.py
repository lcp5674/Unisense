"""notify 渠道枚举扩展：修复模型与迁移漂移（TD §12.9 / FR-16 / FR-17）。

背景：模型 NotifyChannel 已扩展为 6 个渠道（EMAIL/SMS/WEBHOOK/IN_APP/DINGTALK/console），
但 0010_notify 迁移只创建了 4 个（EMAIL/SMS/WEBHOOK/IN_APP）。导致写入 DINGTALK/console
渠道订阅或通知时抛 ``Data truncated for column 'channel'``（MySQL 1265）→ 500。

本迁移将 notification.channel 与 subscription_pref.channel 的 ENUM 扩展为 6 值，
与 ``app.models.notify.NotifyChannel`` 完全对齐。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_notify_channel_enum"
down_revision = "0021_metric_submitted_by"
branch_labels = None
depends_on = None

#: 与 app.models.notify.NotifyChannel 值集完全一致（含小写 console）。
_NOTIFY_CHANNELS = ("EMAIL", "SMS", "WEBHOOK", "IN_APP", "DINGTALK", "console")


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect != "mysql":
        # 非 MySQL（如 SQLite 测试库）无需修改，类型在 autogenerate 时自行兼容
        return
    op.alter_column(
        "notification",
        "channel",
        existing_type=sa.Enum("EMAIL", "SMS", "WEBHOOK", "IN_APP", name="notify_channel"),
        type_=sa.Enum(*_NOTIFY_CHANNELS, name="notify_channel"),
        existing_nullable=False,
    )
    op.alter_column(
        "subscription_pref",
        "channel",
        existing_type=sa.Enum("EMAIL", "SMS", "WEBHOOK", "IN_APP", name="notify_channel"),
        type_=sa.Enum(*_NOTIFY_CHANNELS, name="notify_channel"),
        existing_nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect != "mysql":
        return
    op.alter_column(
        "notification",
        "channel",
        existing_type=sa.Enum(*_NOTIFY_CHANNELS, name="notify_channel"),
        type_=sa.Enum("EMAIL", "SMS", "WEBHOOK", "IN_APP", name="notify_channel"),
        existing_nullable=False,
    )
    op.alter_column(
        "subscription_pref",
        "channel",
        existing_type=sa.Enum(*_NOTIFY_CHANNELS, name="notify_channel"),
        type_=sa.Enum("EMAIL", "SMS", "WEBHOOK", "IN_APP", name="notify_channel"),
        existing_nullable=False,
    )
