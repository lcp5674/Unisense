"""conflict 表补治理字段（severity/source/reason/block_publish）。

背景：指标目录「口径冲突」标记与仲裁模块数据不一致——创建时自动预检
（precheck）只挂 Metric.pending_conflict 标记、不落 conflict 表，导致
「目录显示冲突、仲裁台为空」的孤儿标记（既不可仲裁、又无法自动清除）。

本迁移为 conflict 表补充检测元数据列：
- severity      硬冲突（hard，阻断发布须仲裁）/ 软冲突（soft，建议复核）
- source        来源（auto=创建自动预检 / manual=人工预检 / backfill=存量回填）
- reason        检测原因
- block_publish 是否阻断发布

配合 create_metric 自动落库（硬+软均落）与仲裁台软硬区分展示，
使「指标标记 ⇔ conflict 表未决记录」严格一致。

revision 挂 0089_user_role（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0090_conflict_governance_fields"
down_revision = "0089_user_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conflict",
        sa.Column(
            "severity",
            sa.String(length=16),
            nullable=True,
            comment="冲突严重级别：hard 硬冲突（阻断发布须仲裁）/ soft 软冲突（建议复核）",
        ),
    )
    op.add_column(
        "conflict",
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=True,
            comment="来源：auto 创建自动预检 / manual 人工预检 / backfill 存量回填",
        ),
    )
    op.add_column(
        "conflict",
        sa.Column(
            "reason",
            sa.String(length=1024),
            nullable=True,
            comment="检测原因",
        ),
    )
    op.add_column(
        "conflict",
        sa.Column(
            "block_publish",
            sa.Boolean(),
            nullable=True,
            server_default=sa.text("0"),
            comment="是否阻断发布",
        ),
    )
    op.create_index("ix_conflict_severity", "conflict", ["severity"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_conflict_severity", table_name="conflict")
    op.drop_column("conflict", "block_publish")
    op.drop_column("conflict", "reason")
    op.drop_column("conflict", "source")
    op.drop_column("conflict", "severity")
