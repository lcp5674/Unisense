"""质量领域真实 MySQL 集成测试（TD §12.8 / FR-10）。

用真实数据库验证：规则注册、检测引擎命中落事件(OPEN)、异常事件状态机闭环
(OPEN→ACK→RESOLVED→CLOSED)、阈值内不触发。schema 由 Alembic 迁移建表。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.mysql import Base
from app.models.quality import QualityEvent, QualityRule  # noqa: F401 - 注册模型供 drop_all
from app.models.user import Organization, User
from app.services.quality.schemas import QualityRuleCreate
from app.services.quality.service import QualityService

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
                username="qowner",
                email="qowner@example.com",
                password_hash="x",
                display_name="qowner",
                role="metric_owner",
                status="active",
            )
            s.add(user)
            await s.flush()
            await s.commit()
            return user.id

    return asyncio.run(_run())


def _reset_via_alembic(url: str) -> None:
    """用 Alembic 迁移将目标库升级到 head；MySQL 8.0 连续 DDL 下 1050/1684
    元数据锁时序冲突偶发，重试最多 3 次（对齐 semantic 集成 fixture）。
    """
    env = {**os.environ, "UNISENSE_DB_URL": url}
    last_exc: subprocess.CalledProcessError | None = None
    for attempt in range(3):
        try:
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                env=env,
                cwd=_BACKEND_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            return
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(1.0)
    assert last_exc is not None
    raise last_exc


@pytest.fixture(scope="function")
def db_env():
    if _USE_EXT:
        url = EXT_DB_URL.replace("mysql+pymysql", "mysql+aiomysql")
        engine = create_async_engine(url, echo=False, poolclass=NullPool)

        async def _wipe() -> None:
            # 全表清理：仅 import 本测试涉及模型时 Base.metadata.drop_all 会漏删
            # 其余表，残留表使 alembic 重建报 1050。改为按 information_schema
            # 枚举全部表删除，保证从零重建。
            async with engine.begin() as conn:
                await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
                rows = (
                    await conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = DATABASE()"
                        )
                    )
                ).all()
                for (tname,) in rows:
                    await conn.execute(text(f"DROP TABLE IF EXISTS `{tname}`"))
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


async def test_rule_create_and_event_lifecycle(db_env) -> None:
    async with db_env["session_factory"]() as session:
        svc = QualityService(session)
        rule = await svc.create_rule(
            QualityRuleCreate(
                metric_id=1,
                rule_type="COMPLETENESS",
                threshold={"max": 100},
                severity="P0",
            ),
            user_id=db_env["owner_id"],
        )
        await session.commit()
        assert rule.id is not None

        # 越界 → 命中落事件 OPEN
        event = await svc.detect(1, "COMPLETENESS", Decimal("150"))
        await session.commit()
        assert event is not None
        assert event.level.value == "P0"
        assert event.status.value == "OPEN"

        acked = await svc.ack_event(event.id, "已确认误报，数据已修复", db_env["owner_id"])
        await session.commit()
        assert acked.status.value == "ACK"
        assert acked.ack_note == "已确认误报，数据已修复"
        assert acked.ack_by == db_env["owner_id"]
        assert acked.ack_at is not None

        resolved = await svc.resolve_event(event.id, db_env["owner_id"])
        await session.commit()
        assert resolved.status.value == "RESOLVED"
        assert resolved.resolved_by == db_env["owner_id"]
        assert resolved.resolved_at is not None

        closed = await svc.close_event(event.id, db_env["owner_id"])
        await session.commit()
        assert closed.status.value == "CLOSED"
        assert closed.closed_by == db_env["owner_id"]
        assert closed.closed_at is not None


async def test_detect_no_trigger_within_threshold(db_env) -> None:
    async with db_env["session_factory"]() as session:
        svc = QualityService(session)
        await svc.create_rule(
            QualityRuleCreate(
                metric_id=2,
                rule_type="ACCURACY",
                threshold={"op": "<=", "value": 100},
                severity="P2",
            ),
            user_id=db_env["owner_id"],
        )
        await session.commit()
        event = await svc.detect(2, "ACCURACY", Decimal("80"))
        await session.commit()
        assert event is None
