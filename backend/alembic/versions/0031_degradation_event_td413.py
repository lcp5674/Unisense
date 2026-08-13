"""degradation_event 补齐 TD §4.13 降级度量字段（Gap #4 / FR-17）。

原表仅有 state/reason/actor_id，无法计算降级持续时长与受影响用户数。
本迁移新增 event_type / severity / affected_capabilities / affected_user_count /
started_at / recovered_at / duration_seconds / trigger_reason / resolution_action，
并对齐 TD §4.13 ENUM（degradation_event_type / degradation_severity）。

up/down 均可执行且数据无损（对齐 DEV_GUIDE §9 迁移可逆）：downgrade 删除新增列并
清理枚举类型（MySQL 枚举随列删除而消失，DROP TYPE 为 no-op checkfirst 防 PG 告警）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_degradation_event_td413"
down_revision = "0030_system_dict_seed"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "degradation_event",
        sa.Column(
            "event_type",
            sa.Enum(
                "DEGRADED",
                "UNAVAILABLE",
                "RECOVERED",
                "CIRCUIT_OPENED",
                "CIRCUIT_HALF_OPEN",
                "CIRCUIT_CLOSED",
                name="degradation_event_type",
            ),
            nullable=False,
            server_default="DEGRADED",
            comment="降级事件类型（TD §4.13）",
        ),
    )
    op.add_column(
        "degradation_event",
        sa.Column(
            "severity",
            sa.Enum("LIGHT", "HEAVY", name="degradation_severity"),
            nullable=False,
            server_default="LIGHT",
            comment="严重程度：LIGHT=轻降级(功能减退) / HEAVY=重降级(能力关停)",
        ),
    )
    op.add_column(
        "degradation_event",
        sa.Column(
            "affected_capabilities",
            sa.JSON(),
            nullable=True,
            comment="受影响能力列表（如 ['ai_prefill','nl2sql']）",
        ),
    )
    op.add_column(
        "degradation_event",
        sa.Column(
            "affected_user_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="预估受影响用户数",
        ),
    )
    op.add_column(
        "degradation_event",
        sa.Column("started_at", sa.DateTime(), nullable=True, comment="降级开始时间（UTC）"),
    )
    op.add_column(
        "degradation_event",
        sa.Column(
            "recovered_at", sa.DateTime(), nullable=True, comment="恢复时间（UTC），NULL=仍在降级中"
        ),
    )
    op.add_column(
        "degradation_event",
        sa.Column(
            "duration_seconds", sa.Integer(), nullable=True, comment="降级持续秒数（恢复后回填）"
        ),
    )
    op.add_column(
        "degradation_event",
        sa.Column(
            "trigger_reason",
            sa.String(512),
            nullable=True,
            comment="触发原因（如 'LLM 连续5次超时 > 30s'）",
        ),
    )
    op.add_column(
        "degradation_event",
        sa.Column(
            "resolution_action",
            sa.String(512),
            nullable=True,
            comment="恢复动作（如 '自动探测恢复' / '人工重启'）",
        ),
    )
    op.create_index("idx_degradation_started", "degradation_event", ["started_at"])


def downgrade() -> None:
    op.drop_index("idx_degradation_started", table_name="degradation_event")
    op.drop_column("degradation_event", "resolution_action")
    op.drop_column("degradation_event", "trigger_reason")
    op.drop_column("degradation_event", "duration_seconds")
    op.drop_column("degradation_event", "recovered_at")
    op.drop_column("degradation_event", "started_at")
    op.drop_column("degradation_event", "affected_user_count")
    op.drop_column("degradation_event", "affected_capabilities")
    op.drop_column("degradation_event", "severity")
    op.drop_column("degradation_event", "event_type")
    # MySQL 中 ENUM 随列删除而消失；以下 DROP TYPE 对 MySQL 为 no-op（checkfirst 防 PG 告警）。
    bind = op.get_bind()
    sa.Enum(name="degradation_event_type").drop(bind, checkfirst=True)
    sa.Enum(name="degradation_severity").drop(bind, checkfirst=True)
