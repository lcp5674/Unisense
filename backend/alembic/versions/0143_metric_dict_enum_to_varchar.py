"""指标字典消费列 ENUM → VARCHAR(32)：值域单一事实源收敛到 system_dict（TD §3.1 / 治理）。

背景：metric 表的 dw_layer/aggregation/time_semantics/freshness/metric_tier/serving_mode/
additivity 七列是**纯字典消费列**——前端表单选项从 system_dict 加载、保存侧
validate_dict_value 校验、改码侧注册表同步引用。但列类型仍是 MySQL ENUM 锁死值域，
导致字典补录新值（如 dw_layer 扩 DIM/MID/ST、freshness 加 DAILY）无法落库：
- 保存指标：字典校验通过 → 写入仍抛 ``Data truncated for column``（MySQL 1265）→ 500；
- 字典改码：service 层 ENUM 校验拒绝（历史 0035 只能靠 ALTER ENUM 痛苦扩值）。

本迁移把 7 列放开为 VARCHAR(32)（值域上限 32 覆盖最长的
BATCH_REALTIME_DUAL / COUNT_DISTINCT），值域校验不再由列类型承载；
``metric.type`` 是业务逻辑字段（atomic/derived/composite 三分支被语义服务强依赖）
**保留 ENUM**，不让字典扩出第 4 态。measure_catalog.measure_format 建表即 varchar，
本迁移不含该列（仅 ORM model 对齐）。

可逆：downgrade 恢复 ENUM——若期间已写入扩值（如 dw_layer='DIM'）将失败，
需先人工清理扩值数据方可执行。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0143_metric_dict_enum_to_varchar"
down_revision = "0142_dp_run_log_field_stats"
branch_labels = None
depends_on = None

#: metric 七列：原 ENUM 值域 + nullable + DB 注释（与 ORM model 对齐，MODIFY 须显式携带
#: 避免 MySQL 重置 nullable/comment）。type 列保留 ENUM 不进本清单。
_DICT_COLUMNS: list[tuple[str, tuple[str, ...], bool, str | None]] = [
    ("aggregation", ("SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", "MAX", "MIN", "MEDIAN", "PERCENTILE"), True, "聚合方式（派生/复合无聚合语义时为空）"),
    ("time_semantics", ("PERIOD", "YTD", "TTM", "AVG", "MOM", "YOY"), False, "时间语义"),
    ("freshness", ("REALTIME", "T0", "T1", "HOURLY"), False, "数据新鲜度"),
    ("dw_layer", ("ODS", "DWD", "DWS", "ADS", "DM"), False, "数仓分层"),
    ("metric_tier", ("T1", "T2", "T3"), False, "指标分级"),
    ("serving_mode", ("BATCH_ONLY", "REALTIME_ONLY", "BATCH_REALTIME_DUAL"), False, "服务模式"),
    ("additivity", ("ADDITIVE", "SEMI_ADDITIVE", "NON_ADDITIVE"), False, "可加性"),
]

#: ENUM 类型名（与 model/建表一致，恢复时用）
_ENUM_NAMES = {
    "aggregation": "agg_type",
    "time_semantics": "time_sem",
    "freshness": "freshness_type",
    "dw_layer": "dw_layer_type",
    "metric_tier": "metric_tier_type",
    "serving_mode": "serving_mode_type",
    "additivity": "additivity_type",
}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    for column, values, nullable, comment in _DICT_COLUMNS:
        op.alter_column(
            "metric",
            column,
            existing_type=sa.Enum(*values, name=_ENUM_NAMES[column]),
            type_=sa.String(32),
            existing_nullable=nullable,
            nullable=nullable,
            comment=comment,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    for column, values, nullable, comment in _DICT_COLUMNS:
        op.alter_column(
            "metric",
            column,
            existing_type=sa.String(32),
            type_=sa.Enum(*values, name=_ENUM_NAMES[column]),
            existing_nullable=nullable,
            nullable=nullable,
            comment=comment,
        )
