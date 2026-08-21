"""metric 表口径三方责任：外部人员名称兜底字段。

背景：口径三方责任（product_owner_id / tech_owner_id / dw_developer_id）此前仅支持
平台用户引用（user FK）。但生产场景中责任方可能是平台外人员（业务侧供应商、
协作方等尚未开通平台账号），无法落到 user.id。本迁移为三方可空名称字段——
当责任方不是平台用户时直接落名称（id 为空），展示时「id 可解析 → 平台用户；
name 非空 → 外部人员」。

设计：三个可空 String(128) 列（product_owner_name / tech_owner_name /
dw_developer_name），与 id 字段独立、互不约束（id 与 name 可共存——
id 为权威引用，name 可作展示兜底快照）。对齐 PRD 4.5 口径责任闭环。

revision 挂 0077_metric_onedata（当前线性链后继）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0078_metric_responsible_owner_names"
down_revision = "0077_metric_onedata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "metric",
        sa.Column(
            "product_owner_name",
            sa.String(length=128),
            nullable=True,
            comment="产品需求方名称（非平台用户直接填写）",
        ),
    )
    op.add_column(
        "metric",
        sa.Column(
            "tech_owner_name",
            sa.String(length=128),
            nullable=True,
            comment="技术方名称（非平台用户直接填写）",
        ),
    )
    op.add_column(
        "metric",
        sa.Column(
            "dw_developer_name",
            sa.String(length=128),
            nullable=True,
            comment="数仓开发名称（非平台用户直接填写）",
        ),
    )


def downgrade() -> None:
    op.drop_column("metric", "dw_developer_name")
    op.drop_column("metric", "tech_owner_name")
    op.drop_column("metric", "product_owner_name")
