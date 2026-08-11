"""冲突领域真实 MySQL 集成测试（对齐 gateways integration，TD §12.4 / FR-09）。

用真实数据库验证：check 落库（硬冲突/软冲突）、arbitrate 流转(RULED)、
ruling_record 沉淀。schema 由 Alembic 迁移（与生产一致）建表。
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
from app.models.conflict import Conflict, RulingRecord  # noqa: F401 - 注册模型供 drop_all
from app.models.user import Organization, User
from app.services.conflict.schemas import (
    ArbitrateRequest,
    ConflictListParams,
    MetricInput,
)
from app.services.conflict.service import ConflictService

EXT_DB_URL = os.getenv("UNISENSE_INTEGRATION_DB_URL") or os.getenv("UNISENSE_DB_URL")
_USE_EXT = bool(EXT_DB_URL) and "localhost" in EXT_DB_URL

# backend 根目录（无论 pytest 从何处调用，均定位到 backend/）
_BACKEND_ROOT = str(Path(__file__).resolve().parents[2])


def _seed(session_factory) -> int:
    async def _run() -> int:
        async with session_factory() as s:
            org = Organization(name="默认组织", code="default_org", status="active")
            s.add(org)
            await s.flush()
            user = User(
                org_id=org.id,
                username="cowner",
                email="cowner@example.com",
                password_hash="x",
                display_name="cowner",
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


async def test_hard_conflict_persisted_and_blocks(db_env):
    async with db_env["session_factory"]() as session:
        svc = ConflictService(session)
        result = await svc.check(
            MetricInput(metric_code="gmv_total", domain="sales", definition="sum(amount)"),
            [MetricInput(metric_code="gmv_total", domain="finance", definition="sum(price)")],
        )
        await session.commit()
        assert result.blocked is True
        assert len(result.detections) >= 1
        # 硬冲突已落库 OPEN
        rows, _ = await svc.list_conflicts(
            ConflictListParams(ctype=None, domain=None, page=1, page_size=50)
        )
        assert any(c.type.value == "same_name_diff_def" for c in rows)


async def test_soft_conflict_arbitrated_and_ruling_recorded(db_env):
    async with db_env["session_factory"]() as session:
        svc = ConflictService(session)
        await svc.check(
            MetricInput(
                metric_code="sales_amt",
                domain="sales",
                definition="sum(amount) filter where status=1",
            ),
            [
                MetricInput(
                    metric_code="gmv_total",
                    domain="sales",
                    definition="sum(amount) filter where status=1",
                )
            ],
        )
        await session.commit()
        rows, _ = await svc.list_conflicts(
            ConflictListParams(ctype=None, domain=None, page=1, page_size=50)
        )
        assert any(c.type.value == "same_def_diff_name" for c in rows)
        cid = next(c.conflict_id for c in rows if c.type.value == "same_def_diff_name")

        conflict = await svc.arbitrate(
            cid,
            ArbitrateRequest(
                decision="merge",
                canonical_metric_code="gmv_total",
                arbitrator_id=db_env["owner_id"],
                reason="口径一致建议合并",
            ),
        )
        await session.commit()
        assert conflict.status.value == "RULED"
        assert conflict.decision_json["decision"] == "merge"

        rulings = await svc.get_rulings(cid)
        assert len(rulings) >= 1
        assert rulings[0].decision == "merge"
