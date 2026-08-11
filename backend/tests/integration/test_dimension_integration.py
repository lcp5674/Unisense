"""维度管理真实 MySQL 集成测试（TD §12.15 / FR-05,09）。

用真实数据库验证：维度注册、状态机(DRAFT→DEPRECATED)闭环、成员层级、
维度映射、指标-维度绑定、口径对账(PENDING→APPROVED)闭环。
schema 由 Alembic 迁移建表。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.mysql import Base
from app.models.dimension import (  # noqa: F401
    Dimension,
    DimensionMapping,
    DimensionMember,
    MetricDimension,
    Reconciliation,
)
from app.models.user import Organization, User
from app.services.dimension.schemas import (
    DimensionCreate,
    DimensionMappingCreate,
    DimensionMemberCreate,
    MetricDimensionBind,
    ReconciliationReview,
    ReconciliationSubmit,
)
from app.services.dimension.service import DimensionService

EXT_DB_URL = os.getenv("UNISENSE_INTEGRATION_DB_URL") or os.getenv("UNISENSE_DB_URL")
_USE_EXT = bool(EXT_DB_URL) and "localhost" in EXT_DB_URL
_BACKEND_ROOT = str(Path(__file__).resolve().parents[2])


def _seed(session_factory) -> int:
    async def _run() -> int:
        async with session_factory() as s:
            org = Organization(name="默认组织", code="default_org", status="active")
            s.add(org)
            await s.flush()
            user = User(
                org_id=org.id,
                username="downer",
                email="downer@example.com",
                password_hash="x",
                display_name="downer",
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
        cwd=_BACKEND_ROOT,
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
        except Exception as exc:  # noqa: BLE001
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


async def test_dimension_lifecycle_and_members(db_env) -> None:
    """维度状态机 + 成员层级维护。"""
    async with db_env["session_factory"]() as session:
        svc = DimensionService(session)
        dim = await svc.create_dimension(
            DimensionCreate(
                dim_code="region", name="地区", domain="geo", owner_id=db_env["owner_id"]
            )
        )
        assert dim.status == "DRAFT"

        member = await svc.create_member(
            DimensionMemberCreate(
                dim_code="region", member_code="east", member_name="华东", status="PUBLISHED"
            )
        )
        assert member.id is not None
        members = await svc.list_members("region")
        assert len(members) == 1

        deprecated = await svc.deprecate_dimension("region")
        assert deprecated.status == "DEPRECATED"


async def test_mapping_and_metric_binding(db_env) -> None:
    """维度映射 + 指标-维度绑定。"""
    async with db_env["session_factory"]() as session:
        svc = DimensionService(session)
        await svc.create_dimension(
            DimensionCreate(
                dim_code="region", name="地区", domain="geo", owner_id=db_env["owner_id"]
            )
        )
        await svc.create_dimension(
            DimensionCreate(dim_code="d2", name="d2", domain="x", owner_id=db_env["owner_id"])
        )
        mapping = await svc.create_mapping(
            DimensionMappingCreate(
                source_dim_code="d2",
                target_dim_code="region",
                mapping_type="EQUIVALENT",
                created_by=db_env["owner_id"],
            )
        )
        assert mapping.id is not None
        bind = await svc.bind_metric_dimension(
            MetricDimensionBind(metric_id=1, dim_code="d2", role="PARTITION")
        )
        assert bind.id is not None
        mappings = await svc.list_mappings("d2")
        assert len(mappings) == 1
        metric_dims = await svc.list_metric_dimensions(1)
        assert len(metric_dims) == 1


async def test_reconciliation_review_closure(db_env) -> None:
    """口径对账 PENDING→APPROVED 闭环。"""
    async with db_env["session_factory"]() as session:
        svc = DimensionService(session)
        await svc.create_dimension(
            DimensionCreate(dim_code="d3", name="d3", domain="x", owner_id=db_env["owner_id"])
        )
        rec = await svc.submit_reconciliation(
            ReconciliationSubmit(
                metric_id=1,
                dim_code="d3",
                expected_expr="SUM(x)",
                actual_expr="SUM(x)",
                diff_summary="无差异",
            )
        )
        assert rec.status == "PENDING"
        reviewed = await svc.review_reconciliation(
            rec.id, ReconciliationReview(decision="APPROVED", reviewer_id=db_env["owner_id"])
        )
        assert reviewed.status == "APPROVED"
