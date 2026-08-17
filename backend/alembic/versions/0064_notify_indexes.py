"""通知表索引补齐（性能做实：筛选/去重/purge/扇出查询走索引）。

背景（TD §12.9 通知中心产品化收尾）：真实 EXPLAIN 暴露四类索引缺口——
1. ``todo_only`` 筛选（``subscriber_id + template_code IN + handled_at IS NULL``）
   走 PRIMARY Backward index scan、filtered 仅 ~2%，数据量大时退化；
2. 迁移 0062 建的 ``handled_at`` 列未建索引（模型 index=True 与库不一致）；
3. ``purge_old_notifications``/``purge_old_event_logs`` 按 ``created_at`` 清理
   无索引，每日全表扫描；
4. ``list_enabled_subscriptions``（事件发布每次扇出）按 ``event_type`` 查询，
   唯一约束最左前缀是 user_id，event_type 单独查询无法走索引。

设计：
- ``ix_notification_subscriber_id``：``(subscriber_id, id)`` 复合索引——按用户
  过滤后沿 id 倒序扫描，消除 Backward index scan 与 filesort；
- ``ix_notification_handled_at``：补齐 0062 遗漏（todo_only/去重依赖）；
- ``ix_notification_created_at`` / ``ix_event_log_created_at``：purge 按时间清理；
- ``ix_subscription_pref_event_type``：发布扇出高频查询。

可逆：downgrade 删除全部索引（不影响列与数据）。
"""

from __future__ import annotations

from alembic import op

revision = "0064_notify_indexes"
down_revision = "0063_metric_reject_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 用户收件箱主查询：subscriber 过滤 + id 倒序分页（消除 Backward scan）
    op.create_index(
        "ix_notification_subscriber_id",
        "notification",
        ["subscriber_id", "id"],
        unique=False,
    )
    # 补齐迁移 0062 遗漏：handled_at 索引（todo_only / 去重 / purge 依赖）
    op.create_index("ix_notification_handled_at", "notification", ["handled_at"], unique=False)
    # purge 按时间清理（通知 90 天 / 事件日志 180 天）
    op.create_index("ix_notification_created_at", "notification", ["created_at"], unique=False)
    op.create_index("ix_event_log_created_at", "event_log", ["created_at"], unique=False)
    # 发布扇出按事件类型查订阅（唯一约束最左前缀 user_id 无法服务此查询）
    op.create_index(
        "ix_subscription_pref_event_type",
        "subscription_pref",
        ["event_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_subscription_pref_event_type", table_name="subscription_pref")
    op.drop_index("ix_event_log_created_at", table_name="event_log")
    op.drop_index("ix_notification_created_at", table_name="notification")
    op.drop_index("ix_notification_handled_at", table_name="notification")
    op.drop_index("ix_notification_subscriber_id", table_name="notification")
