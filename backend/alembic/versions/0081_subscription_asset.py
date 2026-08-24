"""subscription_pref 增加资产维度订阅（按指标/源表 watch）。

背景：P2「变更订阅闭环」——此前订阅粒度仅事件类型（metric.breaking_change_pending 等），
无法"按资产 watch"（关注某指标/某源表的变更）。新增可空 asset_type/asset_id：
- 事件订阅行：event_type 非空，asset 为 NULL（存量不变）；
- 资产订阅行：event_type 置 NULL，asset_type+asset_id 非空。
新唯一键 uk_sub_asset (user_id, channel, asset_type, asset_id)；MySQL 唯一索引对
NULL 不比较，事件订阅行不受其约束。

revision 挂 0080_query_log（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0081_subscription_asset"
down_revision = "0080_query_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "subscription_pref",
        "event_type",
        existing_type=sa.String(length=64),
        nullable=True,
        existing_comment="事件类型",
    )
    op.add_column(
        "subscription_pref",
        sa.Column(
            "asset_type",
            sa.String(length=32),
            nullable=True,
            comment="资产类型（METRIC/TABLE；NULL=按事件订阅）",
        ),
    )
    op.add_column(
        "subscription_pref",
        sa.Column(
            "asset_id",
            sa.String(length=64),
            nullable=True,
            comment="资产业务编码（metric_code/entity_name；asset_type 非空时必填）",
        ),
    )
    op.create_unique_constraint(
        "uk_sub_asset",
        "subscription_pref",
        ["user_id", "channel", "asset_type", "asset_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uk_sub_asset", "subscription_pref", type_="unique")
    op.drop_column("subscription_pref", "asset_id")
    op.drop_column("subscription_pref", "asset_type")
    op.alter_column(
        "subscription_pref",
        "event_type",
        existing_type=sa.String(length=64),
        nullable=False,
    )
