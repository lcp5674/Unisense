"""subject_domain + system_dict 表

新增主题域管理模块和系统字典管理模块，支撑指标注册全字段自动化。

Revision ID: 0026
Revises: 0025_dependency_health
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0026"
down_revision = "0025_dependency_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subject_domain",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(64), nullable=False, comment="域编码（唯一）"),
        sa.Column("name", sa.String(128), nullable=False, comment="域显示名"),
        sa.Column("parent_id", sa.BigInteger(), nullable=True, comment="父域ID（根域为null）"),
        sa.Column("level", sa.Integer(), nullable=False, comment="层级（1=根/2=子/3=孙）"),
        sa.Column("path", sa.String(512), nullable=True, comment="物化路径（如 1.5.12）"),
        sa.Column("sort_order", sa.Integer(), nullable=False, comment="同级排序"),
        sa.Column(
            "status",
            sa.Enum("active", "inactive", name="domain_status"),
            nullable=False,
            comment="状态",
        ),
        sa.Column("defaults_json", mysql.JSON(), nullable=False, comment="域级默认值预设"),
        sa.Column("description", sa.Text(), nullable=True, comment="描述"),
        sa.Column("owner_id", sa.BigInteger(), nullable=False, comment="域管理员ID"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间（UTC）"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间（UTC）"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_subject_domain_code"),
        sa.ForeignKeyConstraint(["parent_id"], ["subject_domain.id"], name="fk_domain_parent"),
    )
    op.create_index("idx_domain_code", "subject_domain", ["code"])
    op.create_index("idx_domain_parent", "subject_domain", ["parent_id"])
    op.create_index("idx_domain_path", "subject_domain", ["path"])
    op.create_index("idx_domain_status", "subject_domain", ["status"])

    op.create_table(
        "system_dict",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dict_type", sa.String(64), nullable=False, comment="字典类型"),
        sa.Column("code", sa.String(64), nullable=False, comment="字典项编码"),
        sa.Column("label", sa.String(128), nullable=False, comment="显示名"),
        sa.Column("sort_order", sa.Integer(), nullable=False, comment="排序序号"),
        sa.Column(
            "status",
            sa.Enum("active", "inactive", name="dict_status"),
            nullable=False,
            comment="状态",
        ),
        sa.Column("description", sa.String(256), nullable=True, comment="描述"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间（UTC）"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间（UTC）"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dict_type", "code", name="uk_dict_type_code"),
    )
    op.create_index("idx_dict_type", "system_dict", ["dict_type"])
    op.create_index("idx_dict_status", "system_dict", ["status"])


def downgrade() -> None:
    op.drop_table("system_dict")
    op.drop_table("subject_domain")
