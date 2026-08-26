"""sql_infer_eval_sample 评测样本表。

背景：评测集此前是硬编码 ``dataset.GOLDEN``（开发期基线，pytest 回归门禁），
业务用户无法自助扩充样本。新增自定义样本表（软删、enabled 停用、is_builtin 只读
标记），运行时与内置基线合并——「解析成功率」可随样本持续扩充而度量。挂
0095_sql_infer_eval_run（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0096_sql_infer_eval_sample"
down_revision = "0095_sql_infer_eval_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sql_infer_eval_sample",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "case_id", sa.String(128), nullable=False, unique=True, comment="样本编码（唯一）"
        ),
        sa.Column("dialect", sa.String(32), nullable=False, comment="方言/场景标注"),
        sa.Column("sql", sa.Text(), nullable=False, comment="待解析 SQL 脚本"),
        sa.Column(
            "expected_measures",
            sa.JSON(),
            nullable=False,
            comment="期望度量 [{column, agg, alias?, table?}]",
        ),
        sa.Column("expected_tables", sa.JSON(), nullable=False, comment="期望源表集合"),
        sa.Column("expected_period", sa.String(16), nullable=False, comment="期望统计周期"),
        sa.Column("note", sa.String(512), nullable=False, comment="样本说明"),
        sa.Column("enabled", sa.Boolean(), nullable=False, comment="是否参与评测"),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, comment="内置基线标记（只读）"),
        sa.Column("created_by", sa.Integer(), nullable=True, comment="创建人 ID"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_sql_infer_eval_sample_case_id",
        "sql_infer_eval_sample",
        ["case_id"],
        unique=True,
    )
    op.create_index(
        "idx_sql_infer_eval_sample_enabled",
        "sql_infer_eval_sample",
        ["enabled"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_sql_infer_eval_sample_enabled", table_name="sql_infer_eval_sample")
    op.drop_index("idx_sql_infer_eval_sample_case_id", table_name="sql_infer_eval_sample")
    op.drop_table("sql_infer_eval_sample")
