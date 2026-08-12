"""消费层真实 MySQL 集成测试（TD §12.6 / FR-12,13）。

用真实数据库验证：
- 接入方鉴权（X-Api-Key：client_id:secret 解析、吊销、密钥校验、限流闸门）
- dry-run 口径校验（PUBLISHED ok / DEPRECATED 拦截 / scope 越权拦截）
- 语义查询降级（OLAP 未配置 → 503 DEPENDENCY_DEGRADED_ENGINE）
- 快照 WORM（保存 + 列举）
- 收藏（新增 / 列举 / 移除）
- 版本消费方确认回调（PENDING_REVIEW → PUBLISHED / ARCHIVED）

schema 由 Alembic 迁移建表（外部 localhost 库）或 Base.metadata.create_all（testcontainers）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.deps import get_db_session as _dep_get_db_session
from app.core.exceptions import BusinessError
from app.core.security import create_access_token, hash_password
from app.db.mysql import Base
from app.main import app
from app.models.consume import ApiClient, ApiClientStatus
from app.models.metric import Metric, MetricVersion
from app.models.user import Organization, User
from app.services.consume.schemas import QueryRequest
from app.services.consume.service import ConsumeService

EXT_DB_URL = os.getenv("UNISENSE_INTEGRATION_DB_URL") or os.getenv("UNISENSE_DB_URL")
_USE_EXT = bool(EXT_DB_URL) and "localhost" in EXT_DB_URL
_BACKEND_ROOT = str(Path(__file__).resolve().parents[2])


def _seed(session_factory) -> dict[str, int]:
    async def _run() -> dict[str, int]:
        async with session_factory() as s:
            org = Organization(name="消费测试组织", code="consume_org", status="active")
            s.add(org)
            await s.flush()
            admin = User(
                org_id=org.id,
                username="cadmin",
                email="cadmin@example.com",
                password_hash="x",
                display_name="cadmin",
                role="platform_admin",
                status="active",
            )
            owner = User(
                org_id=org.id,
                username="cowner",
                email="cowner@example.com",
                password_hash="x",
                display_name="cowner",
                role="metric_owner",
                status="active",
            )
            s.add_all([admin, owner])
            await s.flush()
            m1 = Metric(
                metric_code="M1",
                name="营收",
                domain="finance",
                type="atomic",
                granularity="day",
                unit="元",
                aggregation="SUM",
                time_semantics="PERIOD",
                freshness="T1",
                dw_layer="ADS",
                metric_tier="T1",
                serving_mode="BATCH_ONLY",
                additivity="ADDITIVE",
                definition_json={
                    "expression": "sum(revenue)",
                    "grain": "day",
                    "unit": "元",
                    "pii": False,
                    "dependencies": ["fct_order"],
                },
                status="PUBLISHED",
                owner_id=owner.id,
            )
            m2 = Metric(
                metric_code="M2",
                name="毛利",
                domain="finance",
                type="atomic",
                granularity="day",
                unit="元",
                aggregation="SUM",
                time_semantics="PERIOD",
                freshness="T1",
                dw_layer="ADS",
                metric_tier="T2",
                serving_mode="BATCH_ONLY",
                additivity="ADDITIVE",
                definition_json={"expression": "sum(profit)", "grain": "day"},
                status="PUBLISHED",
                owner_id=owner.id,
            )
            m3 = Metric(
                metric_code="M3",
                name="废弃指标",
                domain="finance",
                type="atomic",
                granularity="day",
                unit="元",
                aggregation="SUM",
                time_semantics="PERIOD",
                freshness="T1",
                dw_layer="ADS",
                metric_tier="T3",
                serving_mode="BATCH_ONLY",
                additivity="ADDITIVE",
                definition_json={"expression": "sum(x)", "grain": "day"},
                status="DEPRECATED",
                owner_id=owner.id,
                successor_code="M1",
            )
            s.add_all([m1, m2, m3])
            await s.flush()
            mv = MetricVersion(
                metric_id=m1.id,
                version=2,
                change_type="BREAKING",
                definition_json=m1.definition_json,
                status="PENDING_REVIEW",
                change_reason="口径调整",
                created_by=owner.id,
            )
            s.add(mv)
            await s.commit()
            return {"org_id": org.id, "admin_id": admin.id, "owner_id": owner.id, "mv_id": mv.id}

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


async def _make_client(factory, client_id: str, secret: str, **kw) -> None:
    async with factory() as s:
        client = ApiClient(
            client_id=client_id,
            client_secret_ref=hash_password(secret),
            scope_domain=kw.get("scope_domain", "finance"),
            metric_whitelist=kw.get("metric_whitelist"),
            qps=kw.get("qps", 1000),
            daily_quota=kw.get("daily_quota", 100_000),
            status=ApiClientStatus.ACTIVE,
            created_by=kw.get("created_by", 1),
        )
        s.add(client)
        await s.commit()


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
        ids = _seed(session_factory)
        yield {"engine": engine, "session_factory": session_factory, **ids}
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
        ids = _seed(session_factory)
        yield {"engine": engine, "session_factory": session_factory, **ids}
        container.stop()


async def test_authenticate_and_rate_limit(db_env) -> None:
    factory = db_env["session_factory"]
    await _make_client(factory, "cli_a", "secret_a", created_by=db_env["admin_id"], qps=1)
    async with factory() as session:
        svc = ConsumeService(session)
        client = await svc.authenticate_client("cli_a:secret_a")
        assert client.client_id == "cli_a"
        await svc.check_rate_limit(client)
        with pytest.raises(BusinessError):
            await svc.check_rate_limit(client)
        client.status = ApiClientStatus.REVOKED
        await session.flush()
        await session.commit()
    async with factory() as session:
        svc = ConsumeService(session)
        with pytest.raises(BusinessError):
            await svc.authenticate_client("cli_a:secret_a")
    await _make_client(factory, "cli_b", "secret_b", created_by=db_env["admin_id"])
    async with factory() as session:
        svc = ConsumeService(session)
        with pytest.raises(BusinessError):
            await svc.authenticate_client("cli_b:wrong")


async def test_dry_run_and_degraded(db_env) -> None:
    factory = db_env["session_factory"]
    await _make_client(
        factory, "cli_m", "secret_m", created_by=db_env["admin_id"], metric_whitelist=["M1"]
    )
    async with factory() as session:
        svc = ConsumeService(session)
        client = await svc.authenticate_client("cli_m:secret_m")
        res = await svc.dry_run_query(QueryRequest(metric_code="M1", date_range="2024"), client)
        assert res.status == "ok"
        assert res.meta["status"] == "PUBLISHED"
        with pytest.raises(BusinessError):
            await svc.dry_run_query(QueryRequest(metric_code="M3", date_range="2024"), client)
        with pytest.raises(BusinessError):
            await svc.dry_run_query(QueryRequest(metric_code="M2", date_range="2024"), client)
        with pytest.raises(BusinessError):
            await svc.execute_query(QueryRequest(metric_code="M1", date_range="2024"), client)


async def test_snapshot_and_favorite(db_env) -> None:
    factory = db_env["session_factory"]
    await _make_client(factory, "cli_s", "secret_s", created_by=db_env["admin_id"])
    async with factory() as session:
        svc = ConsumeService(session)
        await svc.authenticate_client("cli_s:secret_s")
        snap = await svc.save_snapshot(
            metric_code="M1",
            version=2,
            dims={},
            date_range="2024",
            value_json={"rows": [{"k": "v"}]},
            quality_flag=None,
            generated_at=datetime.now(timezone.utc),  # noqa: UP017
        )
        await session.commit()
        assert snap.id is not None
        snaps = await svc.list_snapshots("M1", 10, 0)
        assert any(s.id == snap.id for s in snaps)
        # 收藏
        fav = await svc.add_favorite(db_env["owner_id"], "M1")
        await session.commit()
        assert fav.pinned is True
        favs = await svc.list_favorites(db_env["owner_id"])
        assert "M1" in favs
        rem = await svc.remove_favorite(db_env["owner_id"], "M1")
        await session.commit()
        assert rem.pinned is False


async def test_version_confirm_reject(db_env) -> None:
    factory = db_env["session_factory"]
    mv_id = db_env["mv_id"]
    async with factory() as session:
        svc = ConsumeService(session)
        await svc.confirm_version(mv_id, db_env["owner_id"])
        await session.commit()
    async with factory() as session:
        from sqlalchemy import select

        mv = (
            await session.execute(select(MetricVersion).where(MetricVersion.id == mv_id))
        ).scalar_one()
        assert mv.status == "PUBLISHED"
    # 重建一个待确认版本测试 reject
    async with factory() as session:
        from sqlalchemy import select

        m1 = (await session.execute(select(Metric).where(Metric.metric_code == "M1"))).scalar_one()
        mv2 = MetricVersion(
            metric_id=m1.id,
            version=3,
            change_type="UPDATE",
            definition_json=m1.definition_json,
            status="PENDING_REVIEW",
            change_reason="小改",
            created_by=db_env["owner_id"],
        )
        session.add(mv2)
        await session.commit()
        new_id = mv2.id
    async with factory() as session:
        svc = ConsumeService(session)
        await svc.reject_version(new_id, db_env["owner_id"], "不同意")
        await session.commit()
    async with factory() as session:
        from sqlalchemy import select

        mv2 = (
            await session.execute(select(MetricVersion).where(MetricVersion.id == new_id))
        ).scalar_one()
        assert mv2.status == "ARCHIVED"


@pytest.fixture
def app_client(db_env):
    """覆盖 get_db_session 依赖，使整应用路由使用测试库会话。"""
    factory = db_env["session_factory"]

    async def _override():
        async with factory() as s:
            yield s

    _dep_get_db_session.__name__ = "get_db_session"
    app.dependency_overrides[_dep_get_db_session] = _override
    yield app
    app.dependency_overrides.clear()


async def test_api_security_401_and_scope_403(app_client, db_env) -> None:
    transport = ASGITransport(app=app_client)
    await _make_client(
        db_env["session_factory"],
        "cli_m",
        "secret_m",
        created_by=db_env["admin_id"],
        metric_whitelist=["M1"],
    )
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 缺少 X-Api-Key → 401
        r = await ac.post(
            "/api/v1/consume/query/dry-run",
            json={"metric_code": "M1", "date_range": "2024"},
        )
        assert r.status_code == 401
        # 错误密钥 → 401
        r = await ac.post(
            "/api/v1/consume/query/dry-run",
            json={"metric_code": "M1", "date_range": "2024"},
            headers={"X-Api-Key": "cli_m:wrong"},
        )
        assert r.status_code == 401
        # 合法密钥 + scope 越权（白名单仅 M1，请求 M2）→ 403
        r = await ac.post(
            "/api/v1/consume/query/dry-run",
            json={"metric_code": "M2", "date_range": "2024"},
            headers={"X-Api-Key": "cli_m:secret_m"},
        )
        assert r.status_code == 403


async def test_api_admin_rbac_and_injection_guard(app_client, db_env) -> None:
    transport = ASGITransport(app=app_client)
    org_id = db_env["org_id"]
    admin_token = create_access_token(sub=db_env["admin_id"], role="platform_admin", org_id=org_id)
    owner_token = create_access_token(sub=db_env["owner_id"], role="metric_owner", org_id=org_id)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 普通 metric_owner 调用管理端点 → 403
        r = await ac.post(
            "/api/v1/consume/api-clients",
            json={"client_id": "x", "secret": "y"},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert r.status_code == 403
        # 管理员创建接入方 → 200，且明文 secret 返回
        r = await ac.post(
            "/api/v1/consume/api-clients",
            json={"client_id": "cli_api", "secret": "secret_api", "scope_domain": "finance"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        assert r.json()["data"]["secret"] == "secret_api"
        # 注入守卫：date_range 含 SQL 注入片段，API 层依赖项应在执行前拦截（fail-closed）
        r = await ac.post(
            "/api/v1/consume/query/dry-run",
            json={"metric_code": "M1", "date_range": "2024'; DROP TABLE metric; --"},
            headers={"X-Api-Key": "cli_api:secret_api"},
        )
        assert r.status_code == 400
        assert r.json()["code"] == "INJECTION_DETECTED"
