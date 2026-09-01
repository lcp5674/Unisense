"""query_log 复合索引（P12 性能审查：health_scorer 按 metric_code+created_at 高频过滤）。

背景：``health_scorer`` 对每指标刷新健康分时按 ``metric_code + created_at`` 过滤
query_log（30d/90d 活跃度），迁移 0080 仅建了单列 ``ix_query_log_created(created_at)``
与 ``metric_code`` 单列索引，组合过滤需回表。补复合索引覆盖该高频路径，
指标量大后健康刷新不再随 query_log 增长而线性恶化。

幂等：create_index 由 alembic 版本表保证只执行一次；downgrade 删除复合索引
（保留既有单列索引，不触碰业务数据）。
"""

from __future__ import annotations

from alembic import op

revision = "0123_query_log_composite_index"
down_revision = "0122_audit_worm_triggers"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_query_log_metric_created"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "query_log",
        ["metric_code", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="query_log")
