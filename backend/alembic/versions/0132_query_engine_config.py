"""查询引擎（OLAP/MySQL 降级）DB 配置表（方案 A：前端可配置化）。

背景：``consume`` 查询引擎连接此前仅能经环境变量（``UNISENSE_OLAP_URL`` /
``UNISENSE_MYSQL_FALLBACK_URL``）配置，更换 Doris 集群/改密码需改 .env 并重启。
本迁移新增 ``query_engine_config`` 单行配置表，仿 ``llm_config``（DB 优先、env
兜底、密钥 Fernet 加密落库）的既有范式：

- ``olap_url``：可选 OLAP 基础 URL（含则派生 host/port/database，与 config.py
  ``_derive_doris_from_olap_url`` 同语义）；
- ``doris_host/port/database/user``：显式直连参数（优先于 olap_url 派生）；
- ``doris_password_enc`` / ``mysql_fallback_url_enc``：Fernet 加密存储，避免
  明文密钥/连接串落库；
- ``enabled``：是否启用 DB 配置（停用回落环境变量）。

生效解析在 ``QueryEngineConfigService.get_effective``（DB 行 enabled > env > none），
consume 执行器按配置指纹热重建，配置保存后无需重启即生效（跨 worker 最长 30s）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0132_query_engine_config"
down_revision = "0131_audit_worm_allow_erasure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "query_engine_config",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "olap_url",
            sa.String(length=512),
            nullable=False,
            server_default="",
            comment="OLAP 基础 URL（可选，含则派生 host/port/database）",
        ),
        sa.Column(
            "doris_host",
            sa.String(length=128),
            nullable=False,
            server_default="",
            comment="Doris FE 主机（显式直连，优先于 olap_url 派生）",
        ),
        sa.Column(
            "doris_port",
            sa.Integer(),
            nullable=False,
            server_default="8030",
            comment="Doris FE HTTP 端口（默认 8030）",
        ),
        sa.Column(
            "doris_database",
            sa.String(length=128),
            nullable=False,
            server_default="",
            comment="Doris 默认库（可空，不指定默认库）",
        ),
        sa.Column(
            "doris_user",
            sa.String(length=64),
            nullable=False,
            server_default="",
            comment="Doris HTTP basic auth 用户名（可空=无认证）",
        ),
        sa.Column(
            "doris_password_enc",
            sa.Text(),
            nullable=False,
            comment="Doris 密码（Fernet 加密令牌）",
        ),
        sa.Column(
            "mysql_fallback_url_enc",
            sa.Text(),
            nullable=False,
            comment="MySQL 降级引擎完整 URL（Fernet 加密令牌）",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
            comment="是否启用 DB 配置（停用回落环境变量）",
        ),
        sa.Column(
            "updated_by",
            sa.BigInteger(),
            nullable=True,
            comment="最后编辑者用户 ID",
        ),
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
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
            comment="更新时间（UTC）",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
        comment="查询引擎（OLAP/MySQL 降级）DB 配置（单行，方案 A）",
    )


def downgrade() -> None:
    op.drop_table("query_engine_config")
