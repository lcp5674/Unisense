"""新增 llm_config 表：平台 LLM 配置（单例行，OpenAI 协议兼容）。

背景：此前 LLM 只能通过环境变量（UNISENSE_LLM_*）配置，无前端入口；
本迁移新增 llm_config 表，允许在「AI 助手」页配置 base_url/model/api_key
（API Key 经 SecretManager Fernet 加密落库），DB 配置优先于环境变量。

可逆：downgrade DROP TABLE llm_config。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_llm_config"
down_revision = "0036_column_descriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_config",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column(
            "provider",
            sa.String(32),
            nullable=False,
            server_default="custom",
            comment="提供商标识",
        ),
        sa.Column(
            "base_url",
            sa.String(256),
            nullable=False,
            server_default="",
            comment="OpenAI 兼容接口基础 URL",
        ),
        sa.Column("model", sa.String(128), nullable=False, server_default="", comment="模型名称"),
        sa.Column(
            "api_key_enc",
            sa.Text(),
            nullable=False,
            comment="API Key（Fernet 加密令牌）",
        ),
        sa.Column(
            "timeout",
            sa.Integer(),
            nullable=False,
            server_default="30",
            comment="请求超时秒数",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="是否启用该配置",
        ),
        sa.Column("updated_by", sa.BigInteger(), nullable=True, comment="最后编辑者用户 ID"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            comment="更新时间（UTC）",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC）",
        ),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        comment="LLM 平台配置（单例行）",
    )


def downgrade() -> None:
    op.drop_table("llm_config")
