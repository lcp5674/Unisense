"""血缘领域真实 MySQL 集成测试（对齐 gateways integration）。

用真实数据库验证：解析落库（表级+字段级）、影响分析 BFS 查询、
源表 DDL 变更 → 受影响指标 Owner 定向通知（治理闭环）。
schema 由 Alembic 迁移（与生产一致）建表。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.mysql import Base
from app.models.metric import Metric
from app.models.notify import Notification
from app.models.user import Organization, User
from app.services.lineage.repository import LineageRepository
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


# backend 目录（alembic.ini 所在处），从测试文件推导，任意 cwd 均可运行
_BACKEND_DIR = Path(__file__).resolve().parents[2]


def _reset_via_alembic(url: str) -> None:
    # 预建加宽版本表：迁移文件名最长 34 字符（0066_data_source_multi_db_schedule），
    # alembic 默认 alembic_version.version_num 为 VARCHAR(32) 会 Data too long。
    # 主库应用库已手扩 VARCHAR(64)，这里对齐，避免新迁移库 upgrade head 炸。
    from sqlalchemy import create_engine

    eng = create_engine(url, poolclass=NullPool)
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(64) NOT NULL PRIMARY KEY) ENGINE=InnoDB"
            )
        )
    eng.dispose()

    env = {**os.environ, "UNISENSE_DB_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        cwd=_BACKEND_DIR,
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


async def _seed_metric_with_upstream_edge(session_factory, owner_id: int) -> tuple[str, str]:
    """预置发布态指标 + 其源表血缘边（table:ods_orders → metric:revenue_amount）。

    返回 (metric_code, source_table)；供 DDL 变更端到端用例复用。
    """
    metric_code = "revenue_amount"
    source_table = "ods_orders"
    async with session_factory() as s:
        s.add(
            Metric(
                metric_code=metric_code,
                name="营收金额",
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
                    "dependencies": [source_table],
                },
                status="PUBLISHED",
                owner_id=owner_id,
            )
        )
        await s.flush()
        # 指标源表方向：table:{tbl} → metric:{code}（粒度 L3）
        await LineageRepository(s).upsert_metric_table_edge(
            metric_code=metric_code, table_node=f"table:{source_table}", direction="upstream"
        )
        await s.commit()
    return metric_code, source_table


async def _count_ddl_notifications(session_factory, owner_id: int) -> int:
    async with session_factory() as s:
        return len(
            (
                await s.execute(
                    select(Notification).where(
                        Notification.subscriber_id == owner_id,
                        Notification.template_code == "lineage.ddl_changed",
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_ddl_rename_notifies_affected_metric_owner(db_env):
    """端到端：源表 RENAME → 受影响指标 Owner 收到 lineage.ddl_changed 定向通知。

    覆盖治理闭环整链：DDL 解析（rename_table）→ 血缘落库 → 提交后副作用
    ``_notify_ddl_change`` → ``affected_asset_owners`` 下游收集指标 Owner →
    ``NotifyService.notify_user`` 落库 Notification（template_code=lineage.ddl_changed）。
    """
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]
    _, source_table = await _seed_metric_with_upstream_edge(session_factory, owner_id)
    assert await _count_ddl_notifications(session_factory, owner_id) == 0

    async with session_factory() as s:
        svc = LineageService(s)
        res = await svc.parse_and_store(
            LineageParseRequest(sql=f"ALTER TABLE {source_table} RENAME TO {source_table}_v2"),
            actor_id=owner_id,
        )
        await s.commit()
        await svc.run_post_commit()
        assert res.table_edges >= 1

    notifications = await _count_ddl_notifications(session_factory, owner_id)
    assert notifications >= 1, "源表重命名后受影响指标 Owner 应收到 DDL 变更定向通知"
    async with session_factory() as s:
        rows = (
            await s.execute(
                select(Notification)
                .where(
                    Notification.subscriber_id == owner_id,
                    Notification.template_code == "lineage.ddl_changed",
                )
                .order_by(Notification.id.desc())
            )
        )
        n = rows.scalars().first()
    assert n is not None and "血缘变更" in n.title
    assert n.channel == "IN_APP"
