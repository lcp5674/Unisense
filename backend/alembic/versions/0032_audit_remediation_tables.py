"""Audit remediation: KeyRotation/JwtBlacklist/DegradationEntry/FeatureFlag/DeadLetterEvent tables.

SEC-01/02: Key rotation records for Fernet key management.
SEC-06: JWT blacklist for token revocation.
OPS-05: Degradation registry entries.
OPS-09: Feature flag framework.
TECH-04: Dead letter queue for event bus.

Reversible: downgrade drops all tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_audit_remediation_tables"
down_revision = "0031_degradation_event_td413"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # KeyRotation: 密钥轮换记录
    op.create_table(
        "key_rotation",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("key_id", sa.String(64), nullable=False, unique=True),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_by", sa.Integer(), nullable=True),
    )
    op.create_index("ix_key_rotation_purpose", "key_rotation", ["purpose"])
    op.create_index("ix_key_rotation_status", "key_rotation", ["status"])

    # JwtBlacklistEntry: JWT 黑名单
    op.create_table(
        "jwt_blacklist_entry",
        sa.Column("jti", sa.String(36), primary_key=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False, server_default="LOGOUT"),
    )

    # DegradationEntry: 降级注册中心条目
    op.create_table(
        "degradation_entry",
        sa.Column("component", sa.String(64), primary_key=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="HEALTHY",
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check", sa.DateTime(timezone=True), nullable=True),
    )

    # FeatureFlag: 特性开关
    op.create_table(
        "feature_flag",
        sa.Column("name", sa.String(128), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("target_domains", sa.JSON(), nullable=True),
        sa.Column("target_users", sa.JSON(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # DeadLetterEvent: 事件总线死信
    op.create_table(
        "dead_letter_event",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="PENDING",
        ),
    )
    op.create_index("ix_dle_event_type", "dead_letter_event", ["event_type"])
    op.create_index("ix_dle_status", "dead_letter_event", ["status"])


def downgrade() -> None:
    op.drop_index("ix_dle_status", table_name="dead_letter_event")
    op.drop_index("ix_dle_event_type", table_name="dead_letter_event")
    op.drop_table("dead_letter_event")
    op.drop_table("feature_flag")
    op.drop_table("degradation_entry")
    op.drop_table("jwt_blacklist_entry")
    op.drop_index("ix_key_rotation_status", table_name="key_rotation")
    op.drop_index("ix_key_rotation_purpose", table_name="key_rotation")
    op.drop_table("key_rotation")
