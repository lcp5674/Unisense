"""dimension / term 增加 REVIEW 审核态与审核字段（统一主数据审核）。

背景：维度/术语与逻辑度量同为主数据基础层（发布即生效、静默影响下游指标），
此前 DRAFT → PUBLISHED 直接发布、无审核，形成"基础层比上层更松"的治理倒挂。
本迁移与 0082_measure_catalog_review 对齐（统一复用 ``ReviewFieldsMixin``）：
- dimension.status / term.status 枚举新增 REVIEW（DRAFT → REVIEW → PUBLISHED → DEPRECATED）；
- 新增 9 个审核字段（submitted_by/approver_id/reviewer_id/reviewer_type/
  reviewer_domain/reject_reason/reject_reviewer_id/rejected_at/reviewed_at），
  与 measure_catalog 完全一致（模型共享 ReviewFieldsMixin）。

存量数据全部保持 DRAFT/PUBLISHED/DEPRECATED 不变，无数据迁移。

revision 挂 0083_feedback_clarification（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0084_master_data_review"
down_revision = "0083_feedback_clarification"
branch_labels = None
depends_on = None

#: 9 个审核字段（与 ReviewFieldsMixin / 0082 measure_catalog 完全一致）
_REVIEW_COLUMNS: list[tuple[str, sa.Column]] = [
    (
        "submitted_by",
        sa.Column("submitted_by", sa.BigInteger(), nullable=True,
                  comment="提交评审人 ID（approve/reject 时禁止自审）"),
    ),
    (
        "approver_id",
        sa.Column("approver_id", sa.BigInteger(), nullable=True,
                  comment="审核通过人 ID"),
    ),
    (
        "reviewer_id",
        sa.Column("reviewer_id", sa.BigInteger(), nullable=True,
                  comment="指定评审用户 ID（reviewer_type=user 时生效）"),
    ),
    (
        "reviewer_type",
        sa.Column("reviewer_type", sa.String(length=16), nullable=True,
                  comment="评审指派类型: user(指定用户)/domain(域评审组)"),
    ),
    (
        "reviewer_domain",
        sa.Column("reviewer_domain", sa.String(length=64), nullable=True,
                  comment="评审团队所在域（reviewer_type=domain 时生效）"),
    ),
    (
        "reject_reason",
        sa.Column("reject_reason", sa.String(length=500), nullable=True,
                  comment="最近一次审核驳回原因（REVIEW→DRAFT 时写入）"),
    ),
    (
        "reject_reviewer_id",
        sa.Column("reject_reviewer_id", sa.BigInteger(), nullable=True,
                  comment="驳回审核人 ID（reject 时写入）"),
    ),
    (
        "rejected_at",
        sa.Column("rejected_at", sa.DateTime(), nullable=True,
                  comment="驳回时间（REVIEW→DRAFT 时写入）"),
    ),
    (
        "reviewed_at",
        sa.Column("reviewed_at", sa.DateTime(), nullable=True,
                  comment="最近审核时间（approve/reject 时写入）"),
    ),
]


def _upgrade_table(table: str, status_comment: str) -> None:
    # MySQL 原生 ENUM 加值需 MODIFY COLUMN（sa.Enum 变更不生成 ALTER）
    op.execute(
        f"ALTER TABLE {table} "
        "MODIFY COLUMN status ENUM('DRAFT','REVIEW','PUBLISHED','DEPRECATED') "
        f"NOT NULL DEFAULT 'DRAFT' COMMENT '{status_comment}'"
    )
    for _, col in _REVIEW_COLUMNS:
        op.add_column(table, col)


def _downgrade_table(table: str) -> None:
    for name, _ in reversed(_REVIEW_COLUMNS):
        op.drop_column(table, name)
    op.execute(
        f"ALTER TABLE {table} "
        "MODIFY COLUMN status ENUM('DRAFT','PUBLISHED','DEPRECATED') "
        "NOT NULL DEFAULT 'DRAFT' COMMENT '状态（DRAFT/PUBLISHED/DEPRECATED）'"
    )


def upgrade() -> None:
    _upgrade_table("dimension", "状态（DRAFT/REVIEW/PUBLISHED/DEPRECATED）")
    _upgrade_table("term", "术语状态")


def downgrade() -> None:
    _downgrade_table("term")
    _downgrade_table("dimension")
