"""metric 表新增 term_ids JSON 列（多术语关联）。

背景（2026-09 用户反馈）：指标详情页「关联术语」要求支持多选——一个指标可关联
多个业务术语（如同时归属「费用」「医保结算」两个术语）。此前仅 ``metric.term_id``
单值外键，无法表达多术语。

方案：新增 ``metric.term_ids`` JSON 数组列（可空）存全部术语 ID；``term_id``
保留为「主术语」（= term_ids 首项），既有单术语绑定/展示/冲突检测链路零破坏。
创建/绑定接口透传 term_ids，服务层写 term_ids + term_id（首项）。

幂等：add_column 天然幂等（列已存在则跳过由 alembic 版本表保证）。
downgrade 删除该列（仅删除新增列，不触碰 term_id 主术语）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0119_metric_term_ids"
down_revision = "0118_seed_builtin_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "metric",
        sa.Column("term_ids", sa.JSON(), nullable=True, comment="关联术语 ID 列表（多选，主术语=首项）"),
    )


def downgrade() -> None:
    op.drop_column("metric", "term_ids")
