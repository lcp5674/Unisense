"""create degradation_event table (TD §4.13 降级矩阵审计表).

记录可选依赖降级开始/恢复事件，供运营看板与审计查询。
up/down 均可执行且数据无损（对齐 DEV_GUIDE §9 迁移可逆）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_degradation_event"
down_revision: str = "0023_tracking_softdelete"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "degradation_event",
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
                name="degradation_dep_type",
            ),
            nullable=False,
            comment="依赖类型",
        ),
        sa.Column("dependency_id", sa.String(length=128), nullable=False, comment="依赖实例标识"),
        sa.Column(
            "state",
            sa.Enum("DEGRADED", "HEALTHY", name="degradation_state"),
            nullable=False,
            comment="DEGRADED=降级开始 / HEALTHY=恢复",
        ),
        sa.Column("reason", sa.String(length=255), nullable=False, comment="降级/恢复原因"),
        sa.Column(
            "actor_id",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="触发方（0=系统自动）",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(
        "idx_degradation_dep_time", "degradation_event", ["dependency_type", "created_at"]
    )
    op.create_index("idx_degradation_state_time", "degradation_event", ["state", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_degradation_state_time", table_name="degradation_event")
    op.drop_index("idx_degradation_dep_time", table_name="degradation_event")
    op.drop_table("degradation_event")
    # MySQL 中 ENUM 随列删除而消失；以下 DROP TYPE 对 MySQL 为 no-op（checkfirst 防 PG 告警）。
    bind = op.get_bind()
    sa.Enum(name="degradation_state").drop(bind, checkfirst=True)
    sa.Enum(name="degradation_dep_type").drop(bind, checkfirst=True)
