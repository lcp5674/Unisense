"""create dependency_health table (TD §4.13 依赖实时健康态).

实时熔断态 + 连续失败数 + 最近探测时间，供运营看板实时查询（与 degradation_event
形成「明细 + 快照」双表）。up/down 均可执行且数据无损（对齐 DEV_GUIDE §9 迁移可逆）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_dependency_health"
down_revision: str = "0024_degradation_event"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dependency_health",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键 ID"),
        sa.Column(
            "dependency_type",
            sa.Enum(
                "LLM",
                "OLAP",
                "GRAPH",
                "ES",
                "DATASOURCE",
                "NOTIFICATION",
                name="dependency_health_dep_type",
            ),
            nullable=False,
            comment="依赖类型",
        ),
        sa.Column("dependency_id", sa.String(length=128), nullable=False, comment="依赖实例标识"),
        sa.Column(
            "status",
            sa.Enum("HEALTHY", "DEGRADED", "UNAVAILABLE", name="dependency_health_status"),
            nullable=False,
            server_default="HEALTHY",
            comment="HEALTHY/DEGRADED/UNAVAILABLE",
        ),
        sa.Column("last_check_at", sa.DateTime(), nullable=True, comment="最近一次探测时间"),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="连续失败次数",
        ),
        sa.Column("latency_p95_ms", sa.Integer(), nullable=True, comment="近5分钟 P95 延迟(ms)"),
        sa.Column(
            "error_rate_pct",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
            comment="近5分钟错误率%",
        ),
        sa.Column(
            "circuit_state",
            sa.Enum("CLOSED", "OPEN", "HALF_OPEN", name="dependency_health_circuit"),
            nullable=False,
            server_default="CLOSED",
            comment="熔断器状态",
        ),
        sa.Column("circuit_opened_at", sa.DateTime(), nullable=True, comment="熔断开启时间"),
        sa.Column("meta", sa.JSON(), nullable=True, comment="扩展信息"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_unique_constraint(
        "uk_dependency_health_dep", "dependency_health", ["dependency_type", "dependency_id"]
    )
    op.create_index("idx_dep_type", "dependency_health", ["dependency_type"])
    op.create_index("idx_dep_id", "dependency_health", ["dependency_id"])


def downgrade() -> None:
    op.drop_index("idx_dep_id", table_name="dependency_health")
    op.drop_index("idx_dep_type", table_name="dependency_health")
    op.drop_constraint("uk_dependency_health_dep", "dependency_health", type_="unique")
    op.drop_table("dependency_health")
    # MySQL 中 ENUM 随列删除而消失；以下 DROP TYPE 对 MySQL 为 no-op（checkfirst 防 PG 告警）。
    bind = op.get_bind()
    sa.Enum(name="dependency_health_circuit").drop(bind, checkfirst=True)
    sa.Enum(name="dependency_health_status").drop(bind, checkfirst=True)
    sa.Enum(name="dependency_health_dep_type").drop(bind, checkfirst=True)
