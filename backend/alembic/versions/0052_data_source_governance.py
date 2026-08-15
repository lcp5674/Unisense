"""data_source 治理字段：owner_id / description / include_patterns /
exclude_patterns / health_metrics / degraded_since。

三期功能数据地基（表级采集过滤 + 健康降级判定）：
- owner_id（FK→user.id）：数据源负责人；
- description：用途描述；
- include_patterns / exclude_patterns（JSON list[str]）：采集表级白黑名单（fnmatch）；
- health_metrics（JSON dict）：DEGRADED 判定所需的健康指标；
- degraded_since（DateTime tz）：进入降级态起始时间。

注意：当前 alembic 链头为 0051_metric_template_owner，故本迁移
down_revision 指向 0051 以保持线性（任务原稿的 0050 已被 0051 接管为前驱）。

可逆：downgrade 删除上述列与 FK。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_data_source_governance"
down_revision = "0051_metric_template_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "data_source",
        sa.Column("owner_id", sa.Integer(), nullable=True, comment="数据源负责人（用户 ID）"),
    )
    op.create_foreign_key(
        "fk_data_source_owner", "data_source", "user", ["owner_id"], ["id"]
    )
    op.add_column(
        "data_source",
        sa.Column("description", sa.Text(), nullable=True, comment="用途描述"),
    )
    op.add_column(
        "data_source",
        sa.Column(
            "include_patterns",
            sa.JSON(),
            nullable=True,
            comment="表级包含白名单（fnmatch 风格，NULL=全部）",
        ),
    )
    op.add_column(
        "data_source",
        sa.Column(
            "exclude_patterns",
            sa.JSON(),
            nullable=True,
            comment="表级排除黑名单（fnmatch 风格）",
        ),
    )
    op.add_column(
        "data_source",
        sa.Column(
            "health_metrics",
            sa.JSON(),
            nullable=True,
            comment="健康指标（p95_ms/success_rate/error_count/sample_count/period_hours）",
        ),
    )
    op.add_column(
        "data_source",
        sa.Column(
            "degraded_since",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="进入降级态起始时间（UTC）",
        ),
    )


def downgrade() -> None:
    op.drop_constraint("fk_data_source_owner", "data_source", type_="foreignkey")
    op.drop_column("data_source", "degraded_since")
    op.drop_column("data_source", "health_metrics")
    op.drop_column("data_source", "exclude_patterns")
    op.drop_column("data_source", "include_patterns")
    op.drop_column("data_source", "description")
    op.drop_column("data_source", "owner_id")
