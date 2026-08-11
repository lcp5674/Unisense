"""glossary 服务迁移：术语冲突 / 版本快照 / 关系（TD §12.14 / FR-08）。

手写迁移（对齐 DEV_GUIDE §9，up + down 均可执行、数据无损），
表结构严格对齐 `app.models.glossary` 与 TD §4.1。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0008_glossary"
down_revision = "0007"
branch_labels = None
depends_on = None

_TABLE_OPTS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "glossary_conflict",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True, comment="主键 ID"),
        sa.Column("term_id", sa.BigInteger(), nullable=False, comment="术语 ID"),
        sa.Column(
            "conflict_type",
            sa.Enum(
                "alias_overlap",
                "name_overlap",
                "definition_overlap",
                name="glossary_conflict_type",
            ),
            nullable=False,
            comment="冲突类型",
        ),
        sa.Column("ref_term_id", sa.BigInteger(), nullable=True, comment="参照术语 ID"),
        sa.Column("ref_metric_id", sa.BigInteger(), nullable=True, comment="参照指标 ID"),
        sa.Column(
            "status",
            sa.Enum("OPEN", "RESOLVED", "IGNORED", name="glossary_conflict_status"),
            nullable=False,
            server_default="OPEN",
            comment="裁决状态",
        ),
        sa.Column("resolver", sa.BigInteger(), nullable=True, comment="裁决人 ID"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_glossary_conflict_term_id", "glossary_conflict", ["term_id"])
    op.create_index("ix_glossary_conflict_status", "glossary_conflict", ["status"])

    op.create_table(
        "term_version",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True, comment="主键 ID"),
        sa.Column("term_id", sa.BigInteger(), nullable=False, comment="术语 ID"),
        sa.Column("version", sa.BigInteger(), nullable=False, comment="版本号"),
        sa.Column("snapshot", mysql.JSON(), nullable=False, comment="术语快照"),
        sa.Column("changed_by", sa.BigInteger(), nullable=False, comment="变更人 ID"),
        sa.Column("change_note", sa.String(255), nullable=True, comment="变更说明"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_term_version_term_id", "term_version", ["term_id"])

    op.create_table(
        "term_relation",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True, comment="主键 ID"),
        sa.Column("source_term_id", sa.BigInteger(), nullable=False, comment="源术语 ID"),
        sa.Column("target_term_id", sa.BigInteger(), nullable=False, comment="目标术语 ID"),
        sa.Column(
            "relation_type",
            sa.Enum(
                "SYNONYM_OF",
                "BROADER_THAN",
                "NARROWER_THAN",
                "RELATED_TO",
                name="term_relation_type",
            ),
            nullable=False,
            comment="关系类型",
        ),
        sa.Column("declared_by", sa.BigInteger(), nullable=True, comment="声明人 ID"),
        sa.Column(
            "source_type",
            sa.Enum("MANUAL", "LLM_SUGGESTED", name="term_source_type"),
            nullable=False,
            server_default="MANUAL",
            comment="来源类型",
        ),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True, comment="确认时间"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_term_id", "target_term_id", "relation_type", name="uk_term_pair"
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_term_relation_source", "term_relation", ["source_term_id"])
    op.create_index("ix_term_relation_target", "term_relation", ["target_term_id"])


def downgrade() -> None:
    op.drop_table("term_relation")
    op.drop_table("term_version")
    op.drop_table("glossary_conflict")
