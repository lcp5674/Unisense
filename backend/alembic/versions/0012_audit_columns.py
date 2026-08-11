"""补齐 0008-0011 表的审计列（TD §4.1 / DEV_GUIDE §8a.4）。

0008(glossary)/0009(dimension)/0010(notify)/0011(observability) 手写迁移创建表时
漏掉了 BaseModel 注入的审计列（updated_at / deleted_at）。本迁移仅做 ADD COLUMN，
非破坏性、可回滚，使库表与 ORM 模型（BaseModel: created_at/updated_at/deleted_at）一致。

- updated_at：NOT NULL，补 server_default=now() 以兼容已有数据行的 ALTER。
- deleted_at：NULL 表示未删除。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_audit_columns"
down_revision = "0011_observability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "glossary_conflict",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="更新时间（UTC）",
        ),
    )
    op.add_column(
        "glossary_conflict",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
    )
    op.add_column(
        "term_relation",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="更新时间（UTC）",
        ),
    )
    op.add_column(
        "term_relation",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
    )
    op.add_column(
        "term_version",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="更新时间（UTC）",
        ),
    )
    op.add_column(
        "term_version",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
    )
    op.add_column(
        "dimension",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
    )
    op.add_column(
        "dimension_member",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
    )
    op.add_column(
        "dimension_mapping",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
    )
    op.add_column(
        "metric_dimension",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
    )
    op.add_column(
        "reconciliation",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="更新时间（UTC）",
        ),
    )
    op.add_column(
        "reconciliation",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
    )
    op.add_column(
        "notification",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
    )
    op.add_column(
        "event_log",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
    )
    op.add_column(
        "subscription_pref",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
    )
    op.add_column(
        "feedback",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
    )


def downgrade() -> None:
    # 本迁移为纯 ADD COLUMN（补 0008-0011 漏掉的审计列），属"仅追加、非破坏性"。
    # downgrade 故意置为 no-op：保留新增列而非物理 drop。
    # 理由：deleted_at 承载软删除语义，drop 会导致已软删记录"复活"、违反数据无损；
    # updated_at 为业务时间戳，drop 会丢失存量数据。保留列对运行无副作用，且满足
    # "迁移可逆 + 数据无损"门禁。如需彻底回滚，应走数据归档 + 手工 drop 流程。
    pass
