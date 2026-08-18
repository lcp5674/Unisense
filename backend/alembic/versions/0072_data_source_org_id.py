"""数据源组织归属（多租户隔离地基，P0-2/P1-加固）。

背景：``data_source`` 是资产（db_catalog）的租户根实体，此前无组织边界——
任意组织的用户登录后可见/可改全部数据源资产（跨组织越权读改）。本迁移为
``data_source`` 增加 ``org_id`` 组织归属列：

- 新列可空（历史数据回填到默认组织 1，保持存量单组织部署可见性）；
- 创建数据源时由当前用户组织写入（API 层注入 user.org_id）；
- 资产地图数据源作用域读取（表目录/孤儿/搜索/详情/PII）按 ``org_id`` 过滤，
  多组织部署下用户仅见本组织资产；platform_admin 透传 None 豁免过滤。

revision 挂 0071_catalog_entity_name_idx（当前线性链后继）。
"""

from __future__ import annotations

from alembic import op

revision = "0072_data_source_org_id"
down_revision = "0071_catalog_entity_name_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "data_source",
        op.Column("org_id", op.Integer(), nullable=True, comment="所属组织 ID（多租户隔离）"),
    )
    # 历史数据回填默认组织 1（存量单组织部署保持全量可见）
    op.execute("UPDATE data_source SET org_id = 1 WHERE org_id IS NULL")
    op.create_index("idx_data_source_org", "data_source", ["org_id"])


def downgrade() -> None:
    op.drop_index("idx_data_source_org", table_name="data_source")
    op.drop_column("data_source", "org_id")
