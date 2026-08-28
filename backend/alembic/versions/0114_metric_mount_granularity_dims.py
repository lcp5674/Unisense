"""metric_mount 加 granularity_dims（组合粒度粒度维度列，方案 B）

Revision ID: 0114_metric_mount_granularity_dims
Revises: 0113_db_catalog_fulltext
Create Date: 2026-08-28

背景（用户审查「按月+医院统计订单总金额算什么粒度」）：
- 单值 granularity 无法表达组合粒度（唯一性维度集合）——「月+医院」被压成
  主时间粒度 + 普通维度，粒度维度（参与唯一性的业务实体）与可下钻普通维度混淆。
- 方案 B：granularity 保留单值（主粒度=时间频率语义），新增 granularity_dims
  （粒度维度=参与唯一性的业务实体，如 ["hospital"]）；推断/注册/消费三侧
  同步区分「粒度维度」与「普通维度」（普通维度可下钻，粒度维度固定进 GROUP BY）。

仅 MySQL 的 JSON 列（SQLite 单测无真实迁移需求，ORM 自动建表）；可逆。
"""

from __future__ import annotations

from alembic import op

revision = "0114_metric_mount_granularity_dims"
down_revision = "0113_db_catalog_fulltext"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(
            "ALTER TABLE metric_mount ADD COLUMN granularity_dims JSON NULL "
            "COMMENT '粒度维度（组合粒度唯一性实体列表，如 [\"hospital\"]）' "
            "AFTER granularity"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("ALTER TABLE metric_mount DROP COLUMN granularity_dims")
