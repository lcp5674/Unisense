"""db_catalog 乐观锁版本列（P1-加固）。

背景：``assign_owner``/``reclassify_sensitivity``/批量认领/批量重分类此前直接
覆盖（last-write-wins），并发用户同时认领/重分类同一资产时后写覆盖先写，无冲突
提示。为 ``db_catalog`` 增加 ``row_version`` 版本列，治理写方法用条件 UPDATE
（``WHERE id=? AND row_version=?``）实现乐观锁：版本不匹配即抛 409 冲突，
提示用户刷新后重试。

注意：不启用 SQLAlchemy ``version_id_col``（那会让采集 worker 对同一行的并发
ORM 更新全部抛 StaleDataError，破坏采集幂等语义）；仅资产地图治理写路径显式
校验版本。

revision 挂 0072_data_source_org_id（当前线性链后继）。
"""

from __future__ import annotations

from alembic import op

revision = "0073_db_catalog_row_version"
down_revision = "0072_data_source_org_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "db_catalog",
        op.Column("row_version", op.Integer(), nullable=False, server_default="1", comment="乐观锁版本号"),
    )


def downgrade() -> None:
    op.drop_column("db_catalog", "row_version")
