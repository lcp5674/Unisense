"""metric_mount 变体级责任方：product/tech/dw_developer 三列（id + 外部人员名称）

Revision ID: 0106_metric_mount_variant_owner
Revises: 0105_metric_mount_multi_mount
Create Date: 2026-08-27

背景：多变体（一指标多挂载，0105 放开）下不同变体可能归属不同需求方/开发角色
（如「医院粒度费用」归张三、「药品粒度费用」归李四）。给 metric_mount 加与
metric 表同构的变体级口径三方责任（均可空，缺省继承指标级）：

- ``product_owner_id`` / ``tech_owner_id`` / ``dw_developer_id``（BigInteger 可空）；
- ``product_owner_name`` / ``tech_owner_name`` / ``dw_developer_name``
  （String(128) 可空，外部人员名称兜底）。

仅新增可空列（治理属性），不涉及约束/索引变更；存量行空 = 继承指标级，
零数据迁移。全部操作幂等（对齐 0105 经验：MySQL DDL 隐式提交致半应用态自愈）。
"""

from alembic import op
import sqlalchemy as sa

revision = "0106_metric_mount_variant_owner"
down_revision = "0105_metric_mount_multi_mount"
branch_labels = None
depends_on = None


def _column_exists(bind: sa.Connection, table: str, column: str) -> bool:
    """幂等：判断列是否已存在（MySQL DDL 隐式提交致半应用态自愈）。"""
    rows = bind.exec_driver_sql(
        "SELECT 1 FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table, column),
    ).fetchall()
    return bool(rows)


_COLUMNS: list[tuple[str, sa.Column]] = [
    (
        "product_owner_id",
        sa.Column("product_owner_id", sa.BigInteger(), nullable=True, comment="变体级产品需求方用户 ID（缺省继承指标级）"),
    ),
    (
        "tech_owner_id",
        sa.Column("tech_owner_id", sa.BigInteger(), nullable=True, comment="变体级技术方用户 ID（缺省继承指标级）"),
    ),
    (
        "dw_developer_id",
        sa.Column("dw_developer_id", sa.BigInteger(), nullable=True, comment="变体级数仓开发用户 ID（缺省继承指标级）"),
    ),
    (
        "product_owner_name",
        sa.Column("product_owner_name", sa.String(length=128), nullable=True, comment="变体级产品需求方名称（非平台用户直接填写）"),
    ),
    (
        "tech_owner_name",
        sa.Column("tech_owner_name", sa.String(length=128), nullable=True, comment="变体级技术方名称（非平台用户直接填写）"),
    ),
    (
        "dw_developer_name",
        sa.Column("dw_developer_name", sa.String(length=128), nullable=True, comment="变体级数仓开发名称（非平台用户直接填写）"),
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    for name, column in _COLUMNS:
        if not _column_exists(bind, "metric_mount", name):
            op.add_column("metric_mount", column)


def downgrade() -> None:
    bind = op.get_bind()
    for name, _column in reversed(_COLUMNS):
        if _column_exists(bind, "metric_mount", name):
            op.drop_column("metric_mount", name)
