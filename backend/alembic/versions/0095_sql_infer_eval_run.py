"""sql_infer_eval_run 评测运行记录表。

背景：SQL 智能推断「解析成功率」需要可度量、可追踪——每次评测集运行落一行，
聚合指标（度量/表精确率召回率、完全匹配率）+ 逐用例明细 JSON，前端评测页面
据此可视化成功率与趋势。挂 0094_measure_category_dict_seed（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0095_sql_infer_eval_run"
down_revision = "0094_measure_category_dict_seed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sql_infer_eval_run",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("total", sa.Integer(), nullable=False, comment="评测集用例总数"),
        sa.Column("exact_count", sa.Integer(), nullable=False, comment="完全匹配用例数"),
        sa.Column("exact_rate", sa.Float(), nullable=False, comment="完全匹配率（0~1）"),
        sa.Column(
            "measure_precision", sa.Float(), nullable=True, comment="度量级精确率（宏平均）"
        ),
        sa.Column(
            "measure_recall", sa.Float(), nullable=True, comment="度量级召回率（宏平均）"
        ),
        sa.Column("table_precision", sa.Float(), nullable=True, comment="表级精确率（宏平均）"),
        sa.Column("table_recall", sa.Float(), nullable=True, comment="表级召回率（宏平均）"),
        sa.Column("period_match_rate", sa.Float(), nullable=True, comment="周期匹配率（0~1）"),
        sa.Column("cases_json", sa.JSON(), nullable=False, comment="逐用例明细"),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False, comment="本次评测耗时（毫秒）"),
        sa.Column("actor_id", sa.Integer(), nullable=True, comment="触发人 ID（CLI/CI 为空）"),
        sa.Column("ran_at", sa.DateTime(timezone=True), nullable=False, comment="评测运行时间（UTC）"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_sql_infer_eval_run_ran_at",
        "sql_infer_eval_run",
        ["ran_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_sql_infer_eval_run_ran_at", table_name="sql_infer_eval_run")
    op.drop_table("sql_infer_eval_run")
