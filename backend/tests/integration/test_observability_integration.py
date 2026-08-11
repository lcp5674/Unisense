"""可观测性真实 MySQL 集成测试（TD §12.10 / FR-16）。

用真实数据库验证：反馈提交与查询、评分越界校验、运营大盘聚合统计。
schema 由 Alembic 迁移建表（feedback 表）。
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

from app.core.exceptions import UnisenseError
from app.db.mysql import Base
from app.models.feedback import Feedback  # noqa: F401
from app.models.user import Organization, User
from app.services.observability.schemas import FeedbackCreate
from app.services.observability.service import ObservabilityService

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
                username="oowner",
                email="oowner@example.com",
                password_hash="x",
                display_name="oowner",
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


async def test_submit_feedback_and_stats(db_env) -> None:
    """反馈提交 + 查询 + 大盘聚合统计。"""
    async with db_env["session_factory"]() as session:
        svc = ObservabilityService(session)
        fb = await svc.submit_feedback(
            FeedbackCreate(
                user_id=db_env["owner_id"],
                target_type="metric",
                target_id="gmv",
                rating=5,
                comment="good",
            )
        )
        assert fb.id is not None
        feedbacks = await svc.list_feedback(None, 10)
        assert len(feedbacks) >= 1
        assert isinstance(await svc.api_stats(), dict)
        assert isinstance(await svc.notification_stats(), dict)
        assert isinstance(await svc.lineage_stats(), dict)


async def test_rating_out_of_range_rejected(db_env) -> None:
    """评分越界(>5)被拒绝。"""
    async with db_env["session_factory"]() as session:
        svc = ObservabilityService(session)
        with pytest.raises(UnisenseError):
            await svc.submit_feedback(
                FeedbackCreate(user_id=db_env["owner_id"], target_type="metric", rating=9)
            )
