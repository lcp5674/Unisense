"""measure_catalog 增加 REVIEW 审核态与审核字段。

背景：度量是原子指标的权威继承源（单位/格式/小数位/口径直接传播到下游指标），
此前 DRAFT → PUBLISHED 直接发布、无审核，形成"基础层比上层更松"的治理倒挂。
本迁移对齐指标审核流（TD §13）：
- status 枚举新增 REVIEW（DRAFT → REVIEW → PUBLISHED → DEPRECATED）；
- 新增审核字段：submitted_by（提交评审人）、approver_id（审核通过人）、
  reviewer_id/reviewer_type/reviewer_domain（评审指派）、
  reject_reason/reject_reviewer_id/rejected_at（驳回可追溯）、reviewed_at（最近审核时间）。

存量数据全部保持 DRAFT/PUBLISHED/DEPRECATED 不变，无数据迁移。

revision 挂 0081_subscription_asset（当前线性链后继）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0082_measure_catalog_review"
down_revision = "0081_subscription_asset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MySQL 原生 ENUM 加值需 MODIFY COLUMN（sa.Enum 变更不生成 ALTER）
    op.execute(
        "ALTER TABLE measure_catalog "
        "MODIFY COLUMN status ENUM('DRAFT','REVIEW','PUBLISHED','DEPRECATED') "
        "NOT NULL DEFAULT 'DRAFT' COMMENT '状态（DRAFT/REVIEW/PUBLISHED/DEPRECATED）'"
    )
    op.add_column(
        "measure_catalog",
        sa.Column(
            "submitted_by",
            sa.BigInteger(),
            nullable=True,
            comment="提交评审人 ID（approve/reject 时禁止自审）",
        ),
    )
    op.add_column(
        "measure_catalog",
        sa.Column(
            "approver_id",
            sa.BigInteger(),
            nullable=True,
            comment="审核通过人 ID",
        ),
    )
    op.add_column(
        "measure_catalog",
        sa.Column(
            "reviewer_id",
            sa.BigInteger(),
            nullable=True,
            comment="指定评审用户 ID（reviewer_type=user 时生效）",
        ),
    )
    op.add_column(
        "measure_catalog",
        sa.Column(
            "reviewer_type",
            sa.String(length=16),
            nullable=True,
            comment="评审指派类型: user(指定用户)/domain(域评审组)",
        ),
    )
    op.add_column(
        "measure_catalog",
        sa.Column(
            "reviewer_domain",
            sa.String(length=64),
            nullable=True,
            comment="评审团队所在域（reviewer_type=domain 时生效）",
        ),
    )
    op.add_column(
        "measure_catalog",
        sa.Column(
            "reject_reason",
            sa.String(length=500),
            nullable=True,
            comment="最近一次审核驳回原因（REVIEW→DRAFT 时写入）",
        ),
    )
    op.add_column(
        "measure_catalog",
        sa.Column(
            "reject_reviewer_id",
            sa.BigInteger(),
            nullable=True,
            comment="驳回审核人 ID（reject 时写入）",
        ),
    )
    op.add_column(
        "measure_catalog",
        sa.Column(
            "rejected_at",
            sa.DateTime(),
            nullable=True,
            comment="驳回时间（REVIEW→DRAFT 时写入）",
        ),
    )
    op.add_column(
        "measure_catalog",
        sa.Column(
            "reviewed_at",
            sa.DateTime(),
            nullable=True,
            comment="最近审核时间（approve/reject 时写入）",
        ),
    )


def downgrade() -> None:
    for col in (
        "reviewed_at",
        "rejected_at",
        "reject_reviewer_id",
        "reject_reason",
        "reviewer_domain",
        "reviewer_type",
        "reviewer_id",
        "approver_id",
        "submitted_by",
    ):
        op.drop_column("measure_catalog", col)
    op.execute(
        "ALTER TABLE measure_catalog "
        "MODIFY COLUMN status ENUM('DRAFT','PUBLISHED','DEPRECATED') "
        "NOT NULL DEFAULT 'DRAFT' COMMENT '状态（DRAFT/PUBLISHED/DEPRECATED）'"
    )
