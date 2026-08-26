"""指标列表深分页复合索引。

背景（第六轮生产就绪审查 P1-4）：``MetricRepository.list_metrics`` 默认
``ORDER BY updated_at desc, id desc`` 且支持 ``created_after/before`` 范围过滤、
``deleted_at`` 软删过滤——此前 ``created_at/updated_at`` 均无索引，5 万+ 指标时
深分页 O(offset) 全表扫 + filesort，每次请求还跑 COUNT 全表。新增
``(deleted_at, updated_at)`` 复合索引：软删过滤 + 更新时间排序 + 时间范围过滤
共享同一索引，消除排序回表与全表扫。

revision 挂 0091_metric_raw_sql（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0092_metric_list_pagination_index"
down_revision = "0091_metric_raw_sql"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 幂等：既有库可能已通过其它途径创建同名索引（并行会话），存在则跳过
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    indexes = {ix["name"] for ix in inspector.get_indexes("metric")}
    if "idx_metric_deleted_updated" not in indexes:
        op.create_index(
            "idx_metric_deleted_updated",
            "metric",
            ["deleted_at", "updated_at"],
            unique=False,
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    indexes = {ix["name"] for ix in inspector.get_indexes("metric")}
    if "idx_metric_deleted_updated" in indexes:
        op.drop_index("idx_metric_deleted_updated", table_name="metric")
