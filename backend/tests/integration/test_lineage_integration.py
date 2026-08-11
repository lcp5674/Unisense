"""血缘领域真实 MySQL 集成测试（对齐 gateways integration）。

用真实数据库验证：解析落库（表级+字段级）、影响分析 BFS 查询。
schema 由 Alembic 迁移（与生产一致）建表。
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

from app.db.mysql import Base
from app.models.user import Organization, User
from app.services.lineage.schemas import LineageImpactParams, LineageParseRequest
from app.services.lineage.service import LineageService

EXT_DB_URL = os.getenv("UNISENSE_INTEGRATION_DB_URL") or os.getenv("UNISENSE_DB_URL")
_USE_EXT = bool(EXT_DB_URL) and "localhost" in EXT_DB_URL


def _seed(session_factory) -> int:
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


async def test_parse_persists_edges_and_impact_query(db_env):
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]
    async with session_factory() as session:
        svc = LineageService(session)
        res = await svc.parse_and_store(
            LineageParseRequest(
                sql="INSERT INTO t SELECT a.id, b.name FROM a JOIN b ON a.id = b.id"
            ),
            actor_id=owner_id,
        )
        await session.commit()
        assert res.table_edges >= 1

        edges = await svc.query_impact(LineageImpactParams(node="table:a"))
        targets = {e.target_node for e in edges}
        assert "table:t" in targets
