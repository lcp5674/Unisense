"""metric 表口径三方责任字段（产品需求方/技术方/数仓开发）。

背景：指标口径从需求到落地涉及三个责任主体——产品需求方（业务口径提出人）、
技术方（口径 ETL/SQL 实现人）、数仓开发（数仓建模/血缘维护人）。此前 metric 表
仅有 owner_id/backup_owner_id（主/副 Owner），缺口径相关的三方责任字段，无法
按"谁提的需求/谁做的开发/谁维护数仓"定位到人、做通知与审计。

设计：三个可空 user FK（product_owner_id / tech_owner_id / dw_developer_id），
与 owner_id 同模式——可通知/指派/审计到具体用户。均可空（主 Owner 已必填，
三方责任为补充，避免抬升注册门槛）。对齐 TD §4.1 metric 表。

revision 挂 0073_db_catalog_row_version（当前线性链后继）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0074_metric_responsible_owners"
down_revision = "0073_db_catalog_row_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "metric",
        sa.Column(
            "product_owner_id",
            sa.BigInteger(),
            nullable=True,
            comment="产品需求方用户 ID（口径业务需求提出人）",
        ),
    )
    op.add_column(
        "metric",
        sa.Column(
            "tech_owner_id",
            sa.BigInteger(),
            nullable=True,
            comment="技术方用户 ID（口径 ETL/SQL 实现人）",
        ),
    )
    op.add_column(
        "metric",
        sa.Column(
            "dw_developer_id",
            sa.BigInteger(),
            nullable=True,
            comment="数仓开发用户 ID（数仓建模/血缘维护人）",
        ),
    )
    op.create_foreign_key(
        "fk_metric_product_owner", "metric", "user", ["product_owner_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_metric_tech_owner", "metric", "user", ["tech_owner_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_metric_dw_developer", "metric", "user", ["dw_developer_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_metric_dw_developer", "metric", type_="foreignkey")
    op.drop_constraint("fk_metric_tech_owner", "metric", type_="foreignkey")
    op.drop_constraint("fk_metric_product_owner", "metric", type_="foreignkey")
    op.drop_column("metric", "dw_developer_id")
    op.drop_column("metric", "tech_owner_id")
    op.drop_column("metric", "product_owner_id")
