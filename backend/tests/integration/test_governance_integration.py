"""governance 真实 MySQL 集成测试（对齐 gateways integration，TD §12.5 / FR-11）。

用真实数据库验证：授权落库与幂等合并、到期自动回收、权限快照、
PII 复核回写 ``metric.compliance_reviewed``、分级重扫落 ``classification`` 并回写
``db_catalog.sensitivity_level``。schema 由 Alembic 迁移（与生产一致）建表。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.mysql import Base
from app.models.data_source import DataSource, DBCatalog
from app.models.governance import (  # noqa: F401 - 注册模型供 drop_all
    Classification,
    Grant,
    GrantStatus,
    GrantType,
    Role,
    SensitivityLevel,
)
from app.models.metric import Metric
from app.models.user import Organization, User
from app.services.governance.schemas import (
    ClassificationRescanRequest,
    GrantCreate,
    PermissionCheckRequest,
    PiiReviewRequest,
)
from app.services.governance.service import GovernanceService

EXT_DB_URL = os.getenv("UNISENSE_INTEGRATION_DB_URL") or os.getenv("UNISENSE_DB_URL")
_USE_EXT = bool(EXT_DB_URL) and "localhost" in EXT_DB_URL

_BACKEND_ROOT = str(Path(__file__).resolve().parents[2])

_FUTURE = datetime.now(UTC) + timedelta(days=30)
_SOON = datetime.now(UTC) + timedelta(days=2)


def _seed(session_factory: Any) -> dict[str, int]:
    async def _run() -> dict[str, int]:
        async with session_factory() as s:
            org = Organization(name="默认组织", code="default_org", status="active")
            s.add(org)
            await s.flush()

            owner = User(
                org_id=org.id,
                username="gowner",
                email="gowner@example.com",
                password_hash="x",
                display_name="gowner",
                role="metric_owner",
                domain="sales",
                status="active",
            )
            officer = User(
                org_id=org.id,
                username="gofficer",
                email="gofficer@example.com",
                password_hash="x",
                display_name="gofficer",
                role="compliance_officer",
                domain="risk",
                status="active",
            )
            s.add_all([owner, officer])
            await s.flush()

            metric = Metric(
                metric_code="user_phone_cnt",
                name="手机号覆盖数",
                domain="sales",
                type="atomic",
                granularity="daily",
                unit="count",
                aggregation="COUNT",
                time_semantics="PERIOD",
                freshness="T1",
                dw_layer="DWD",
                metric_tier="T3",
                serving_mode="BATCH_ONLY",
                additivity="ADDITIVE",
                definition_json={"expression": "COUNT(phone)", "dependencies": ["dwd_user"]},
                version=1,
                row_version=1,
                status="DRAFT",
                owner_id=owner.id,
                pii_flag=False,
                compliance_reviewed=False,
            )
            source = DataSource(
                source_id="mysql-01",
                name="业务库",
                source_type="mysql",
                connection_config='{"host": "127.0.0.1"}',
                domain="sales",
                quota={"max_concurrency": 4, "max_scan_rows": 10000},
                health_status="healthy",
                created_by=owner.id,
            )
            s.add_all([metric, source])
            await s.flush()

            catalog_pii = DBCatalog(
                source_id="mysql-01",
                entity_type="table",
                entity_name="dwd_user",
                upstream_signature=hashlib.sha256(b"mysql-01:dwd_user").hexdigest(),
                schema_json={"columns": [{"name": "id_card"}, {"name": "amount"}]},
                sensitivity_level="INTERNAL",
            )
            catalog_plain = DBCatalog(
                source_id="mysql-01",
                entity_type="table",
                entity_name="dwd_order",
                upstream_signature=hashlib.sha256(b"mysql-01:dwd_order").hexdigest(),
                schema_json={"columns": [{"name": "order_id"}, {"name": "amount"}]},
                sensitivity_level="INTERNAL",
            )
            s.add_all([catalog_pii, catalog_plain])
            await s.flush()
            await s.commit()
            return {
                "owner_id": owner.id,
                "officer_id": officer.id,
                "catalog_pii": catalog_pii.id,
                "catalog_plain": catalog_plain.id,
            }

    result: dict[str, int] = asyncio.run(_run())
    return result


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
        assert EXT_DB_URL is not None
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
        seeded = _seed(session_factory)
        yield {"engine": engine, "session_factory": session_factory, **seeded}
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
        seeded = _seed(session_factory)
        yield {"engine": engine, "session_factory": session_factory, **seeded}
        container.stop()


async def test_grant_persisted_and_merged_idempotently(db_env) -> None:
    async with db_env["session_factory"]() as session:
        svc = GovernanceService(session)
        first = await svc.grant(
            GrantCreate(
                user_id=db_env["officer_id"],
                domain="sales",
                metric_whitelist=["user_phone_cnt"],
                grant_type=GrantType.READ,
                expires_at=_SOON,
            ),
            actor_id=db_env["owner_id"],
        )
        await session.commit()
        assert first.id > 0
        assert first.status is GrantStatus.ACTIVE

        second = await svc.grant(
            GrantCreate(
                user_id=db_env["officer_id"],
                domain="sales",
                metric_whitelist=["gmv_total"],
                grant_type=GrantType.READ,
                expires_at=_FUTURE,
            ),
            actor_id=db_env["owner_id"],
        )
        await session.commit()
        assert second.id == first.id
        assert sorted(second.metric_whitelist or []) == ["gmv_total", "user_phone_cnt"]

        rows = (await session.execute(select(Grant))).scalars().all()
        assert len(rows) == 1


async def test_expired_grant_recycled_and_excluded_from_snapshot(db_env) -> None:
    async with db_env["session_factory"]() as session:
        svc = GovernanceService(session)
        row = await svc.grant(
            GrantCreate(user_id=db_env["officer_id"], domain="sales", expires_at=_SOON),
            actor_id=db_env["owner_id"],
        )
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

        recycled = await svc.expire_due_grants()
        await session.commit()
        assert recycled == 1

        refreshed = (await session.execute(select(Grant).where(Grant.id == row.id))).scalar_one()
        assert refreshed.status is GrantStatus.EXPIRED

        officer = (
            await session.execute(select(User).where(User.id == db_env["officer_id"]))
        ).scalar_one()
        snapshot = await svc.my_permissions(officer)
        assert snapshot.grants == []
        assert snapshot.granted_domains == []


async def test_permission_snapshot_reflects_active_grants(db_env) -> None:
    async with db_env["session_factory"]() as session:
        svc = GovernanceService(session)
        await svc.grant(
            GrantCreate(
                user_id=db_env["officer_id"],
                domain="sales",
                metric_whitelist=["user_phone_cnt"],
                grant_type=GrantType.READ,
                row_level=True,
                expires_at=_SOON,
            ),
            actor_id=db_env["owner_id"],
        )
        await session.commit()

        officer = (
            await session.execute(select(User).where(User.id == db_env["officer_id"]))
        ).scalar_one()
        snapshot = await svc.my_permissions(officer)
        assert snapshot.role == "compliance_officer"
        assert snapshot.granted_domains == ["sales"]
        assert snapshot.metric_whitelist == ["user_phone_cnt"]
        assert snapshot.row_level_restricted is True
        assert len(snapshot.expiring_soon) == 1


async def test_pii_review_updates_metric_compliance_flag(db_env) -> None:
    async with db_env["session_factory"]() as session:
        svc = GovernanceService(session)
        officer = (
            await session.execute(select(User).where(User.id == db_env["officer_id"]))
        ).scalar_one()

        # 复核前：PDP 拒绝访问未复核的 PII 指标
        metric = (
            await session.execute(select(Metric).where(Metric.metric_code == "user_phone_cnt"))
        ).scalar_one()
        metric.pii_flag = True
        await session.commit()

        denied = await svc.check_permission(
            PermissionCheckRequest(
                user_id=db_env["owner_id"], action="read", metric_code="user_phone_cnt"
            )
        )
        assert denied.allow is False
        assert denied.error_code == "FORBIDDEN_PII"

        result = await svc.pii_review(
            PiiReviewRequest(
                metric_code="user_phone_cnt",
                decision="APPROVE",
                sensitivity_level=SensitivityLevel.PII,
                comment="已配置 hash 脱敏，同意发布",
            ),
            reviewer=officer,
        )
        await session.commit()
        assert result.compliance_reviewed is True
        assert result.masking_policy == "hash"

        refreshed = (
            await session.execute(select(Metric).where(Metric.metric_code == "user_phone_cnt"))
        ).scalar_one()
        assert refreshed.compliance_reviewed is True
        assert refreshed.pii_flag is True

        allowed = await svc.check_permission(
            PermissionCheckRequest(
                user_id=db_env["owner_id"], action="read", metric_code="user_phone_cnt"
            )
        )
        assert allowed.allow is True
        assert allowed.masking == "hash"


async def test_classification_rescan_persists_and_updates_catalog(db_env) -> None:
    async with db_env["session_factory"]() as session:
        svc = GovernanceService(session)
        result = await svc.classification_rescan(ClassificationRescanRequest(source_id="mysql-01"))
        await session.commit()

        assert result.scanned == 2
        assert result.pii_found == 1
        assert result.changed == 1
        assert result.model_version == "rules-v1"

        rows = (await session.execute(select(Classification))).scalars().all()
        assert len(rows) == 2
        pii_row = next(r for r in rows if r.catalog_id == db_env["catalog_pii"])
        assert pii_row.sensitivity_level is SensitivityLevel.PII
        assert pii_row.pii_columns and pii_row.pii_columns[0]["rule"] == "id_card"

        catalog = (
            await session.execute(select(DBCatalog).where(DBCatalog.id == db_env["catalog_pii"]))
        ).scalar_one()
        assert catalog.sensitivity_level == "PII"

        # 二次重扫幂等：不新增 classification 行，也不重复变更
        again = await svc.classification_rescan(ClassificationRescanRequest(source_id="mysql-01"))
        await session.commit()
        assert again.changed == 0
        rows2 = (await session.execute(select(Classification))).scalars().all()
        assert len(rows2) == 2
