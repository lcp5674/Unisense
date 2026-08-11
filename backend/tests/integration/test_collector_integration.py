"""采集领域真实 MySQL 集成测试（对齐 gateways integration）。

用真实数据库验证：数据源加密落库、元数据注册敏感分级（PII）、批量废弃部分失败、
覆盖率重算。schema 由 Alembic 迁移（与生产一致）建表；外部 MySQL 不可用时回退
testcontainers，二者皆不可用则跳过。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import NotFoundError
from app.db.mysql import Base
from app.models.user import Organization, User
from app.services.collector.schemas import (
    BulkDeprecateItem,
    BulkDeprecateRequest,
    DataSourceCreateRequest,
    DBCatalogCreateRequest,
    DBCatalogListParams,
)
from app.services.collector.service import CollectorService

EXT_DB_URL = os.getenv("UNISENSE_INTEGRATION_DB_URL") or os.getenv("UNISENSE_DB_URL")
_USE_EXT = bool(EXT_DB_URL) and "localhost" in EXT_DB_URL


def _seed(session_factory) -> int:
    """种子：组织 + Owner 用户（data_source.created_by 外键）。"""

    async def _run() -> int:
        async with session_factory() as s:
            org = Organization(name="默认组织", code="default_org", status="active")
            s.add(org)
            await s.flush()
            user = User(
                org_id=org.id,
                username="owner",
                email="owner@example.com",
                password_hash="x",
                display_name="owner",
                role="metric_owner",
                status="active",
            )
            s.add(user)
            await s.flush()
            await s.commit()
            return user.id

    return asyncio.run(_run())


def _reset_via_alembic(url: str) -> None:
    env = {**os.environ, "UNISENSE_DB_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        cwd="backend",
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="function")
def db_env():
    if _USE_EXT:
        url = EXT_DB_URL.replace("mysql+pymysql", "mysql+aiomysql")
        engine = create_async_engine(url, echo=False, poolclass=NullPool)

        async def _wipe() -> None:
            async with engine.begin() as conn:
                await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
                await conn.run_sync(Base.metadata.drop_all)
                await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
                await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))

        asyncio.run(_wipe())
        _reset_via_alembic(EXT_DB_URL)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        owner_id = _seed(session_factory)
        yield {"engine": engine, "session_factory": session_factory, "owner_id": owner_id}
        asyncio.run(engine.dispose())
    else:
        pytest.importorskip("testcontainers")
        from testcontainers.mysql import MySqlContainer

        container = MySqlContainer("mysql:8.0")
        try:
            container.start()
        except Exception as exc:
            pytest.skip(f"MySQL 容器不可用，跳过集成测试: {exc}")

        url = container.get_connection_url().replace("mysql+pymysql", "mysql+aiomysql")
        engine = create_async_engine(url, echo=False)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async def _create_all() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.run(_create_all())
        owner_id = _seed(session_factory)
        yield {"engine": engine, "session_factory": session_factory, "owner_id": owner_id}
        container.stop()


async def test_create_source_encrypts_then_register_classifies_pii(db_env):
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]
    async with session_factory() as session:
        svc = CollectorService(session)
        src = await svc.create_source(
            DataSourceCreateRequest(
                source_id="src1",
                name="主库",
                source_type="mysql",
                connection_config={"host": "127.0.0.1", "password": "secret"},
                domain="db1",
            ),
            actor_id=owner_id,
        )
        await session.commit()
        assert src.connection_config_present is True

        # 注册含 user_name 的实体 -> PII
        cat = await svc.register_catalog(
            DBCatalogCreateRequest(
                source_id="src1",
                entity_name="users",
                schema_def={"columns": ["user_name", "email"]},
            ),
            actor_id=owner_id,
        )
        await session.commit()
        assert cat.sensitivity_level == "PII"

        # 列表可查
        listing = await svc.list_catalogs(
            DBCatalogListParams(source_id="src1", page=1, page_size=10)
        )
        assert listing.total == 1


async def test_bulk_deprecate_partial_on_real_db(db_env):
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]
    async with session_factory() as session:
        svc = CollectorService(session)
        await svc.create_source(
            DataSourceCreateRequest(
                source_id="src2",
                name="S2",
                source_type="mysql",
                connection_config={"host": "h"},
                domain="db2",
            ),
            actor_id=owner_id,
        )
        await svc.register_catalog(
            DBCatalogCreateRequest(
                source_id="src2", entity_name="orders", schema_def={"columns": ["order_id"]}
            ),
            actor_id=owner_id,
        )
        await session.commit()

        result = await svc.bulk_deprecate(
            BulkDeprecateRequest(
                items=[
                    BulkDeprecateItem(source_id="src2", entity_name="orders"),
                    BulkDeprecateItem(source_id="src2", entity_name="nonexistent"),
                ]
            ),
            actor_id=owner_id,
        )
        await session.commit()
        assert len(result.succeeded) == 1
        assert len(result.failed) == 1


async def test_coverage_recomputed_after_register(db_env):
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]
    async with session_factory() as session:
        svc = CollectorService(session)
        await svc.create_source(
            DataSourceCreateRequest(
                source_id="src3",
                name="S3",
                source_type="mysql",
                connection_config={"host": "h"},
                domain="db3",
            ),
            actor_id=owner_id,
        )
        await svc.register_catalog(
            DBCatalogCreateRequest(
                source_id="src3", entity_name="a", schema_def={"columns": ["x"]}
            ),
            actor_id=owner_id,
        )
        await svc.register_catalog(
            DBCatalogCreateRequest(
                source_id="src3", entity_name="b", schema_def={"columns": ["y"]}
            ),
            actor_id=owner_id,
        )
        await session.commit()
        src = await svc.get_source("src3")
        assert src.coverage == 1.0  # 无 quota -> 已采集即 100%


async def test_delete_missing_source_raises(db_env):
    session_factory = db_env["session_factory"]
    async with session_factory() as session:
        svc = CollectorService(session)
        with pytest.raises(NotFoundError):
            await svc.delete_source("ghost")
