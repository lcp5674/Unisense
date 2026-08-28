"""system_dict.granularity 补 hospital（医院粒度）字典项

Revision ID: 0115_granularity_hospital_dict
Revises: 0114_metric_mount_granularity_dims
Create Date: 2026-08-28

背景（2026-08-28 组合粒度方案 B）：业务实体粒度种子缺「医院」——hospital 是医疗
最基础实体粒度（用户示例「按月+医院统计订单总金额」），推断关键词已在内置默认
（infer_dict.DEFAULT_GRAIN_KEYWORDS.hospital），但字典目录（granularity）无此项
导致前端粒度维度下拉无法点选、字典驱动推断无法经 system_dict 覆盖 hospital 关键词。

本迁移幂等补插字典项（仅 MySQL 执行；SQLite 单测无字典种子需求）。可逆（删除该项）。
"""

from __future__ import annotations

from alembic import op

revision = "0115_granularity_hospital_dict"
down_revision = "0114_metric_mount_granularity_dims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(
            "INSERT INTO system_dict (dict_type, code, label, sort_order, status, created_at, updated_at) "
            "SELECT 'granularity', 'hospital', '医院粒度', 29, 'active', NOW(), NOW() "
            "FROM DUAL WHERE NOT EXISTS ("
            "  SELECT 1 FROM system_dict WHERE dict_type='granularity' AND code='hospital'"
            ")"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(
            "DELETE FROM system_dict WHERE dict_type='granularity' AND code='hospital'"
        )
