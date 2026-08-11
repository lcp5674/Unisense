"""创建 external_benchmark 与 reconciliation_record 表（D11 外部基准对账，TD §4.15.7）。

非破坏性、可回滚：仅新建两张表与索引，不改动既有表。
external_benchmark 幂等键 (source_id, metric_code, bench_date, dims) 中 dims 为 JSON，
MySQL 无法对 JSON 列建唯一约束，故 DB 层唯一约束仅覆盖前三者，dims 一致性在
应用层 find_benchmark 中按 JSON 规范化比对保证。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_benchmark_reconciliation"
down_revision = "0013_erasure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_benchmark",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("metric_code", sa.String(length=64), nullable=False),
        sa.Column("bench_date", sa.Date(), nullable=False),
        sa.Column("dims", sa.JSON(), nullable=True),
        sa.Column("bench_value", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("tolerance_pct", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("imported_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id",
            "metric_code",
            "bench_date",
            name="uk_benchmark_source_metric_date",
        ),
        comment="外部权威基准值（银行对账单/审计数），用于与平台指标值自动比对",
    )
    op.create_index("idx_benchmark_metric", "external_benchmark", ["metric_code"])
    op.create_index("idx_benchmark_source", "external_benchmark", ["source_id"])

    op.create_table(
        "reconciliation_record",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("benchmark_id", sa.BigInteger(), nullable=False),
        sa.Column("metric_code", sa.String(length=64), nullable=False),
        sa.Column("metric_value", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("bench_value", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("diff_pct", sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column("window", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum("OK", "WARN", "ALERT", "CONFIRMED"),
            nullable=False,
        ),
        sa.Column("owner_note", sa.String(length=512), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("confirmed_by", sa.BigInteger(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_recon_benchmark", "reconciliation_record", ["benchmark_id"])
    op.create_index("idx_recon_metric", "reconciliation_record", ["metric_code"])
    op.create_index("idx_recon_status", "reconciliation_record", ["status"])


def downgrade() -> None:
    op.drop_index("idx_recon_status", "reconciliation_record")
    op.drop_index("idx_recon_metric", "reconciliation_record")
    op.drop_index("idx_recon_benchmark", "reconciliation_record")
    op.drop_table("reconciliation_record")

    op.drop_index("idx_benchmark_source", "external_benchmark")
    op.drop_index("idx_benchmark_metric", "external_benchmark")
    op.drop_table("external_benchmark")
