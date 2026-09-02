"""术语库真实 MySQL 集成测试（TD §12.14 / FR-08）。

用真实数据库验证：术语注册、状态机(DRAFT→PUBLISHED→DEPRECATED)闭环、
同义词别名重合率 >80% 触发 GlossaryConflict(OPEN)、术语关系维护。
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
from app.models.glossary import GlossaryConflict, TermRelation, TermVersion  # noqa: F401
from app.models.term import Term  # noqa: F401
from app.models.user import Organization, User
from app.services.glossary.schemas import TermCreate, TermRelationCreate
from app.services.glossary.service import GlossaryService

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
                username="gowner",
                email="gowner@example.com",
                password_hash="x",
                display_name="gowner",
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


async def test_term_lifecycle_draft_published_deprecated(db_env) -> None:
    """术语状态机 DRAFT→REVIEW→PUBLISHED→DEPRECATED 闭环（审核流）。"""
    from app.services.master_data_review.schemas import ReviewApproveRequest, ReviewSubmitRequest

    async with db_env["session_factory"]() as session:
        svc = GlossaryService(session)
        created = await svc.create_term(
            TermCreate(
                term_code="gmv",
                name="成交额",
                definition="一定周期内的成交金额",
                domain="sales",
                synonyms=["GMV", "交易额"],
                owner_id=db_env["owner_id"],
            ),
            actor_id=db_env["owner_id"],
        )
        assert created.status.value == "DRAFT"

        # 提交审核 → REVIEW
        submitted = await svc.submit_term(
            "gmv",
            ReviewSubmitRequest(change_reason="完善定义后提审"),
            actor_id=db_env["owner_id"],
            role="platform_admin",
            user_domains=["sales"],
        )
        assert submitted.status.value == "REVIEW"

        # 审核通过 → PUBLISHED
        published = await svc.approve_term(
            "gmv",
            ReviewApproveRequest(),
            actor_id=db_env["owner_id"],
            role="platform_admin",
            user_domains=["sales"],
        )
        assert published.status.value == "PUBLISHED"

        deprecated = await svc.deprecate_term("gmv", db_env["owner_id"])
        assert deprecated.status.value == "DEPRECATED"

        terms, _ = await svc.list_terms(None, None, None, 10, 0)
        assert terms[0].status.value == "DEPRECATED"


async def test_alias_overlap_triggers_conflict(db_env) -> None:
    """同义词别名重合率 >80% 自动生成 OPEN 冲突。"""
    async with db_env["session_factory"]() as session:
        svc = GlossaryService(session)
        await svc.create_term(
            TermCreate(
                term_code="metric_a",
                name="指标甲",
                definition="d-a",
                domain="x",
                synonyms=["a", "b", "c", "d", "e"],
                owner_id=db_env["owner_id"],
            ),
            actor_id=db_env["owner_id"],
        )
        await svc.create_term(
            TermCreate(
                term_code="metric_b",
                name="指标乙",
                definition="d-b",
                domain="x",
                synonyms=["a", "b", "c", "d", "e"],
                owner_id=db_env["owner_id"],
            ),
            actor_id=db_env["owner_id"],
        )
        conflicts = await svc.list_conflicts(None)
        assert len(conflicts) >= 1
        assert any(c.status == "OPEN" for c in conflicts)


async def test_term_relation_create(db_env) -> None:
    """术语关系维护（SYNONYM_OF）。"""
    async with db_env["session_factory"]() as session:
        svc = GlossaryService(session)
        a = await svc.create_term(
            TermCreate(
                term_code="rel_a",
                name="A",
                definition="d",
                domain="x",
                owner_id=db_env["owner_id"],
            ),
            actor_id=db_env["owner_id"],
        )
        b = await svc.create_term(
            TermCreate(
                term_code="rel_b",
                name="B",
                definition="d",
                domain="x",
                owner_id=db_env["owner_id"],
            ),
            actor_id=db_env["owner_id"],
        )
        rel = await svc.create_term_relation(
            "rel_a",
            TermRelationCreate(
                target_term_id=b.id,
                relation_type="SYNONYM_OF",
                declared_by=db_env["owner_id"],
            ),
        )
        assert rel.id is not None
        assert rel.source_term_id == a.id
