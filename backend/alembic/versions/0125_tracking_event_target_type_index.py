"""tracking_event 推荐行为查询复合索引（P11 性能审查）。

背景：``popular_metrics`` / ``related_by_behavior`` 对无界增长的 tracking_event
做全量 GROUP BY / 自连接，过滤条件为 ``target_type + event_type + created_at(90天窗口)``。
既有 ``ix_tracking_event_type_created(event_type, created_at)`` 不覆盖 target_type
前缀，需回表。补 ``(target_type, event_type, created_at)`` 复合索引覆盖该高频路径。

幂等：create_index 由 alembic 版本表保证只执行一次；downgrade 删除复合索引
（保留既有单列/双列索引，不触碰业务数据）。
"""

from __future__ import annotations

from alembic import op

revision = "0124_tracking_event_target_type_index"
down_revision = "0124_metric_fulltext"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_tracking_target_type_event_created"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "tracking_event",
        ["target_type", "event_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="tracking_event")
