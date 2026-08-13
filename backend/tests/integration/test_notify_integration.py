"""通知服务真实 MySQL 集成测试（TD §12.9 / FR-16,17）。

用真实数据库验证：事件发布(EventLog 留痕) + 按订阅偏好扇出 Notification、
无订阅时不扇出、通知状态回写(SENT/FAILED)。
schema 由 Alembic 迁移建表。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.mysql import Base
from app.models.notify import EventLog, Notification, SubscriptionPref  # noqa: F401
from app.models.user import Organization, User
from app.services.notify.schemas import EventPublish, SubscriptionUpsert
from app.services.notify.service import NotifyService

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
                username="nowner",
                email="nowner@example.com",
                password_hash="x",
                display_name="nowner",
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


async def test_publish_event_fans_out_notifications(db_env, monkeypatch) -> None:
    """有订阅时，事件发布扇出 Notification 并回写 SENT。"""
    from unittest.mock import AsyncMock

    from app.core.config import settings

    # SMTP 为外部依赖：集成测试不真实发邮件，mock 投递成功 + 配置 SMTP 端点
    monkeypatch.setattr(settings, "notify_smtp_host", "smtp.test.local")
    monkeypatch.setattr(settings, "notify_smtp_port", 587)
    monkeypatch.setattr(settings, "notify_smtp_user", "no-reply@unisense.local")
    monkeypatch.setattr(settings, "notify_smtp_password", "pw")
    monkeypatch.setattr("aiosmtplib.send", AsyncMock(return_value=(250, b"ok")))

    async with db_env["session_factory"]() as session:
        svc = NotifyService(session)
        await svc.upsert_subscription(
            SubscriptionUpsert(
                user_id=db_env["owner_id"],
                channel="email",
                event_type="quality.anomaly",
                enabled=True,
            )
        )
        result = await svc.publish_event(
            EventPublish(event_type="quality.anomaly", source="quality", payload={"x": 1})
        )
        assert result["notifications"] >= 1
        # P-1 修复后：事件发布即完成投递，通知状态为 SENT
        assert result["delivered"] >= 1

        notifs = await svc.list_notifications(db_env["owner_id"], "SENT")
        assert len(notifs) >= 1


async def test_publish_without_subscription_no_fanout(db_env) -> None:
    """无订阅时，事件发布不扇出任何通知。"""
    async with db_env["session_factory"]() as session:
        svc = NotifyService(session)
        result = await svc.publish_event(
            EventPublish(event_type="orphan.event", source="quality", payload={})
        )
        assert result["notifications"] == 0
