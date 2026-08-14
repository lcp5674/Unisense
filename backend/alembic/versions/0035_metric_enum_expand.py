"""指标枚举扩展：aggregation/time_semantics/freshness 对齐生产字典种子（TD §3.1 / FR-005）。

背景：指标字典（system_dict）已按生产场景扩展为
``aggregation``(9)/``time_semantics``(6)/``freshness``(4)，但
``metric`` 表的 MySQL ENUM 列与 ``MetricCreateRequest`` 的 Literal 校验仍停留在旧窄集，
前端下拉可选项包含 MAX/MIN/MEDIAN/PERCENTILE/MOM/YOY/T0，选中后：
- 后端 Literal 校验拒绝 → 422（注册指标失败）
- 即使放宽校验，写入仍抛 ``Data truncated for column``（MySQL 1265）→ 500

本迁移将三列 ENUM 扩展为与字典种子、ORM 模型、Pydantic Literal 完全一致的值集。
可逆：downgrade 收回新增的枚举值（需确保无存量数据使用这些值时方可执行）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_metric_enum_expand"
down_revision = "0034_feedback_status"
branch_labels = None
depends_on = None

#: 与字典种子 / app.models.metric / schemas.MetricCreateRequest 完全一致。
_AGG = ("SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", "MAX", "MIN", "MEDIAN", "PERCENTILE")
_TIME_SEM = ("PERIOD", "YTD", "TTM", "AVG", "MOM", "YOY")
_FRESHNESS = ("REALTIME", "T0", "T1", "HOURLY")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "metric",
        "aggregation",
        existing_type=sa.Enum(
            "SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", name="agg_type"
        ),
        type_=sa.Enum(*_AGG, name="agg_type"),
        existing_nullable=False,
    )
    op.alter_column(
        "metric",
        "time_semantics",
        existing_type=sa.Enum("PERIOD", "YTD", "TTM", "AVG", name="time_sem"),
        type_=sa.Enum(*_TIME_SEM, name="time_sem"),
        existing_nullable=False,
    )
    op.alter_column(
        "metric",
        "freshness",
        existing_type=sa.Enum("REALTIME", "T1", "HOURLY", name="freshness_type"),
        type_=sa.Enum(*_FRESHNESS, name="freshness_type"),
        existing_nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "metric",
        "aggregation",
        existing_type=sa.Enum(*_AGG, name="agg_type"),
        type_=sa.Enum(
            "SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", name="agg_type"
        ),
        existing_nullable=False,
    )
    op.alter_column(
        "metric",
        "time_semantics",
        existing_type=sa.Enum(*_TIME_SEM, name="time_sem"),
        type_=sa.Enum("PERIOD", "YTD", "TTM", "AVG", name="time_sem"),
        existing_nullable=False,
    )
    op.alter_column(
        "metric",
        "freshness",
        existing_type=sa.Enum(*_FRESHNESS, name="freshness_type"),
        type_=sa.Enum("REALTIME", "T1", "HOURLY", name="freshness_type"),
        existing_nullable=False,
    )
