"""MySQL 数据库连接管理。

使用 SQLAlchemy 2.0 异步引擎。
对齐 DEV_GUIDE §17.1（连接池配置）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


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
    echo=settings.env == "local",
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
