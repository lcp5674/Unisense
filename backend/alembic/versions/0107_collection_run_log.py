"""采集运行日志表 collection_run_log：采集记录详情页「实时日志」明细

Revision ID: 0107_collection_run_log
Revises: 0106_metric_mount_variant_owner
Create Date: 2026-08-27

背景：采集记录详情页需展示采集「实时日志」。数据流为 Redis List 实时缓冲
（``collect:run_log:{run_id}``，RUNNING 期间前端轮询可见）+ 任务终态一次性
bulk 回写本表（长期可追溯，随 ``collection_run`` 保留 90 天、purge 级联清理）。

- ``run_id`` 外键关联 ``collection_run.id``（明细归属主记录）；
- ``idx_run_log_run_ts`` 覆盖 (run_id, ts) 分页查询；
- 日志表随采集记录物理删除（purge），无需软删（审计留痕由 audit_log 独立承担）。

幂等性（对齐 0105/0106 经验：MySQL DDL 隐式提交致半应用态自愈）：
- 建表前检查 information_schema.TABLES 是否已存在；
- 索引/外键随建表语句一次创建，不重复执行。
"""

from alembic import op
import sqlalchemy as sa

revision = "0107_collection_run_log"
down_revision = "0106_metric_mount_variant_owner"
branch_labels = None
depends_on = None


def _table_exists(bind: sa.Connection, table: str) -> bool:
    """幂等：判断表是否已存在（MySQL DDL 隐式提交致半应用态自愈）。"""
    rows = bind.exec_driver_sql(
        "SELECT 1 FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    ).fetchall()
    return bool(rows)


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "collection_run_log"):
        return
    op.create_table(
        "collection_run_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False, comment="关联采集运行记录 ID"),
        sa.Column("ts", sa.DateTime(), nullable=False, comment="日志时间"),
        sa.Column("level", sa.String(length=8), nullable=False, server_default="INFO", comment="日志级别 INFO/WARN/ERROR"),
        sa.Column("phase", sa.String(length=32), nullable=True, comment="采集阶段 start/scanning/registering/complete/fail"),
        sa.Column("entity_name", sa.String(length=256), nullable=True, comment="关联实体名"),
        sa.Column("message", sa.String(length=512), nullable=False, comment="日志内容（截断）"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_collection_run_log"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["collection_run.id"],
            name="fk_run_log_run",
        ),
    )
    op.create_index(
        "idx_run_log_run_ts", "collection_run_log", ["run_id", "ts"], unique=False
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "collection_run_log"):
        return
    op.drop_index("idx_run_log_run_ts", table_name="collection_run_log")
    op.drop_table("collection_run_log")
