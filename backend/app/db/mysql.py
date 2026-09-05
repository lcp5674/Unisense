"""MySQL 数据库连接管理。

使用 SQLAlchemy 2.0 异步引擎。
对齐 DEV_GUIDE §17.1（连接池配置）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# R-1（第七轮韧性）：MySQL 主引擎必须防「无限挂起」。
# - 连接超时：aiomysql 建连阶段超过 10s 视为不可达，快速失败而非无限等待；
# - 语句级超时：每次新连接执行 SET SESSION MAX_EXECUTION_TIME（SELECT 慢查询硬上限），
#   防止慢查询无限占用连接拖垮全站（读写超时由 mysql server 层 wait_timeout 兜底）。
_DB_CONNECT_TIMEOUT = 10
# 语句级超时（毫秒）：主查询路径 SELECT 超过 30s 由 MySQL 主动终止。
_DB_MAX_EXECUTION_TIME_MS = 30_000


def _mask_password(url: str) -> str:
    """SEC-07: 掩码数据库连接串中的密码。"""
    import re

    return re.sub(r"(:\/\/[^:]+:)([^@]+)(@)", r"\1***\3", url)


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 声明式基类。"""


def _to_async_dsn(url: str) -> str:
    """将同步 MySQL DSN 规整为 SQLAlchemy 异步驱动。

    ``UNISENSE_DB_URL`` 统一以同步形式（``mysql+pymysql://``）配置，
    Alembic 等同步工具可直接使用；应用侧异步引擎需 ``mysql+aiomysql``，
    此处做一次驱动替换，避免“同步 DSN 建异步引擎”在导入期即抛错。
    """
    if url.startswith("mysql+aiomysql"):
        return url
    if url.startswith("mysql+pymysql"):
        return "mysql+aiomysql" + url[len("mysql+pymysql") :]
    if url.startswith("mysql://"):
        return "mysql+aiomysql" + url[len("mysql") :]
    return url


engine = create_async_engine(
    _to_async_dsn(settings.db_url),
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=1800,
    echo=settings.env == "local",
    # R-1：连接阶段超时——MySQL 不可达（网络分区/宕机）时 10s 快速失败，防全站无限挂起
    connect_args={"connect_timeout": _DB_CONNECT_TIMEOUT},
)


@event.listens_for(engine.sync_engine, "connect")
def _set_mysql_session_options(dbapi_connection: Any, connection_record: Any) -> None:  # noqa: ANN401
    """R-1 + TZ：新连接注入会话级选项（语句超时 + 会话时区对齐 UTC）。

    - ``MAX_EXECUTION_TIME`` 仅对 SELECT 生效（MySQL 5.7.8+），覆盖主查询路径；
      DML/事务由连接池 ``pool_recycle`` 与 server ``wait_timeout`` 兜底。
    - ``time_zone='+00:00'`` 把 MySQL 会话钟对齐 UTC——ORM 用 Python ``datetime.now(UTC)``
      存 UTC 墙钟到 DATETIME 列，若 DB 服务器钟为 +08:00，SQL 侧 ``func.now()``/
      ``CURRENT_TIMESTAMP`` 会写入上海墙钟，与同表 Python UTC 列混用两种钟差 8 小时。
      显式固定会话时区后，两类写入统一 UTC（读取 DATETIME 无时区语义不受影响）。
    任一语句异常（权限不足等）不应阻断连接建立，best-effort 记录。
    """
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET SESSION time_zone = '+00:00'")
        cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {_DB_MAX_EXECUTION_TIME_MS}")
        cursor.close()
    except Exception as exc:  # noqa: BLE001 - 会话级选项注入失败不应阻断建连
        # R12（审查修复）：失败不再静默——该连接此后无语句级超时保护，须告警
        import logging

        logging.getLogger(__name__).warning(
            "set_mysql_session_options_failed", error=str(exc)
        )

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：提供异步数据库会话。

    不自动 commit——由 API 层统一控制提交时机。
    异常时自动 rollback，finally 关闭会话。

    Yields:
        异步数据库会话。

    Examples:
        >>> @app.get("/items")
        ... async def list_items(db: AsyncSession = Depends(get_db_session)):
        ...     result = await db.execute(select(Item))
        ...     return result.scalars().all()
    """
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
