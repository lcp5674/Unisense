"""Alembic 迁移环境。

从 app.core.config.settings 读取数据库 URL，支持 offline + online 模式。
对齐 DEV_GUIDE §9（迁移工具 Alembic，up+down 均可执行且数据无损）。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool, text

# 确保能导入 app 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.db.mysql import Base  # noqa: E402
from app.models import *  # noqa: E402, F401, F403 — 注册所有模型到 Base.metadata

config = context.config

# 从 settings 注入数据库 URL（将 async URL 转为 sync 供 Alembic 使用）
sync_url = settings.db_url.replace("mysql+aiomysql://", "mysql+pymysql://")
config.set_main_option("sqlalchemy.url", sync_url)

# 调试开关：ALEMBIC_SQL_ECHO=1 时打印迁移执行的原始 SQL
if os.environ.get("ALEMBIC_SQL_ECHO"):
    config.set_main_option("sqlalchemy.echo", "true")

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本不连接数据库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _ensure_version_table_width(connection) -> None:
    """保障 alembic_version.version_num 列宽度，容纳超长 revision。

    Alembic 自动创建版本表时用 VARCHAR(32)，而本项目部分迁移 revision 超长
    （0066_data_source_multi_db_schedule=34 / 0078=35 / 0086=36 字符），导致
    干净库从零 upgrade 到 0066 时报 1406 Data too long（unisense_it 即因此
    停在 0065）。在跑迁移前把列扩到 VARCHAR(128)：表不存在先建够宽的，
    存在但偏窄则 ALTER（幂等）。不动迁移历史，对所有库（含 CI 干净库）生效。
    """
    if connection.dialect.name != "mysql":
        return
    if not connection.dialect.has_table(connection, "alembic_version"):
        connection.execute(
            text(
                "CREATE TABLE alembic_version (version_num VARCHAR(128) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            )
        )
        return
    row = connection.execute(
        text(
            "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'alembic_version' "
            "AND column_name = 'version_num'"
        )
    ).first()
    if row and row[0] and row[0] < 128:
        connection.execute(
            text("ALTER TABLE alembic_version MODIFY version_num VARCHAR(128) NOT NULL")
        )


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        _ensure_version_table_width(connection)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()

        # MySQL non-transactional DDL：每个迁移的 DDL 会隐式提交上一个迁移的
        # 版本写入，但「最后一个迁移」之后没有后续 DDL，其版本 UPDATE 悬在
        # 未提交事务中，连接关闭即回滚 → 表已建而版本号未写（0087 曾因此
        # 半应用）。显式 commit 兜底（事务型库上为空操作，无害）。
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
