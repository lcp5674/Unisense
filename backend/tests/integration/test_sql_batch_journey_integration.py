"""SQL 批量注册完整旅程 —— 真实 MySQL 集成测试。

目标：用真实数据库（MySQL 8.0 + aiomysql + Alembic 迁移）验证「批量旅程」串接
（第六轮 P2-5：此前 parse→register 与 submit/approve 各自独立单测，无端到端完整用例）：

1. ``infer_sql_batch`` 解析多度量 SQL 产出候选（含 CASE 口径 + 派生比率列）；
2. ``batch_register_from_sql`` 创建 DRAFT（batch_id + raw_sql 落库，可整批回溯）；
3. ``submit_metric`` 提交评审（DRAFT → REVIEW）；
4. ``approve_metric`` 标准发布（REVIEW → PUBLISHED）；
5. 全程状态机/乐观锁/冲突预检在真实 MySQL 上闭环。

数据源与 test_semantic_integration 一致：外部已启动 MySQL（``UNISENSE_INTEGRATION_DB_URL``
或 ``UNISENSE_DB_URL`` 指向 localhost 时启用），否则 testcontainers 容器；均不可用跳过。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _app_db_guard import assert_not_app_db

from app.core.exceptions import BusinessError
from app.models.measure_catalog import MeasureCatalog
from app.models.user import Organization, User
from app.services.semantic.schemas import (
    MetricApproveRequest,
    MetricSqlBatchRegisterRequest,
    MetricSubmitRequest,
)
from app.services.semantic.service import MetricService
from app.services.semantic.sql_split import infer_sql_batch

EXT_DB_URL = os.getenv("UNISENSE_INTEGRATION_DB_URL") or os.getenv("UNISENSE_DB_URL")
_USE_EXT = bool(EXT_DB_URL) and "localhost" in EXT_DB_URL

_BACKEND_DIR = Path(__file__).resolve().parents[2]

# 多度量 SQL：原子聚合 + CASE 口径 + 派生比率列（覆盖 A-1/2 口径保留 + P0-3d 派生候选）
_JOURNEY_SQL = """
INSERT INTO dws.trade_daily (dt, gmv, paid_orders, avg_price, refund_rate)
SELECT dt,
       SUM(amount) AS gmv,
       COUNT(DISTINCT order_id) AS paid_orders,
       ROUND(SUM(amount)/NULLIF(COUNT(DISTINCT order_id),0),2) AS avg_price,
       SUM(CASE WHEN is_refund=1 THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0) AS refund_rate
FROM ods.trade_order
WHERE dt >= '2026-01-01'
GROUP BY dt
"""


def _seed(session_factory) -> tuple[int, int, int]:
    """种子：组织 + Owner（metric_owner）+ Reviewer（domain_admin）+ 已发布逻辑度量。"""

    async def _run() -> tuple[int, int, int]:
        async with session_factory() as s:
            org = Organization(name="批量旅程组织", code="bt_journey_org", status="active")
            s.add(org)
            await s.flush()
            owner = User(
                org_id=org.id,
                username="bt_owner",
                email="bt_owner@example.com",
                password_hash="x",
                display_name="bt_owner",
                role="metric_owner",
                domain="fin",
                status="active",
            )
            s.add(owner)
            reviewer = User(
                org_id=org.id,
                username="bt_reviewer",
                email="bt_reviewer@example.com",
                password_hash="x",
                display_name="bt_reviewer",
                role="domain_admin",
                domain="fin",
                status="active",
            )
            s.add(reviewer)
            # 先 flush 让 owner/reviewer 拿到自增 id（measure.owner_id 外键非空）
            await s.flush()
            measure = MeasureCatalog(
                measure_code="pay_amount",
                name="支付金额",
                measure_format="AMOUNT",
                default_unit="元",
                default_decimal_places=2,
                domain="fin",
                owner_id=owner.id,
                status="PUBLISHED",
            )
            s.add(measure)
            await s.flush()
            await s.commit()
            return owner.id, reviewer.id, org.id

    return asyncio.run(_run())


def _reset_via_alembic(url: str) -> None:
    env = {**os.environ, "UNISENSE_DB_URL": url}
    last_exc: subprocess.CalledProcessError | None = None
    for attempt in range(3):
        try:
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                env=env,
                cwd=_BACKEND_DIR,
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
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        url = EXT_DB_URL.replace("mysql+pymysql", "mysql+aiomysql")
        engine = create_async_engine(url, echo=False, poolclass=NullPool)

        db_name = EXT_DB_URL.split("?")[0].rsplit("/", 1)[1]
        assert_not_app_db(EXT_DB_URL)
        admin_url = url.rsplit("/", 1)[0] + "/"
        admin_engine = create_async_engine(admin_url, echo=False, poolclass=NullPool)

        async def _wipe() -> None:
            async with admin_engine.begin() as conn:
                await conn.execute(text(f"DROP DATABASE IF EXISTS `{db_name}`"))
                await conn.execute(
                    text(
                        f"CREATE DATABASE `{db_name}` "
                        "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                    )
                )

        asyncio.run(_wipe())
        asyncio.run(admin_engine.dispose())

        _reset_via_alembic(EXT_DB_URL)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        owner_id, reviewer_id, org_id = _seed(session_factory)
        yield {
            "engine": engine,
            "session_factory": session_factory,
            "owner_id": owner_id,
            "reviewer_id": reviewer_id,
            "org_id": org_id,
        }

        asyncio.run(engine.dispose())
    else:
        pytest.importorskip("testcontainers")
        from testcontainers.mysql import MySqlContainer

        container = MySqlContainer("mysql:8.0")
        try:
            container.start()
        except Exception as exc:  # Docker 不可用 → 跳过
            pytest.skip(f"MySQL 容器不可用，跳过集成测试: {exc}")

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db.mysql import Base

        url = container.get_connection_url().replace("mysql+pymysql", "mysql+aiomysql")
        engine = create_async_engine(url, echo=False)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async def _create_all() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.run(_create_all())

        owner_id, reviewer_id, org_id = _seed(session_factory)
        yield {
            "engine": engine,
            "session_factory": session_factory,
            "owner_id": owner_id,
            "reviewer_id": reviewer_id,
            "org_id": org_id,
        }

        asyncio.run(engine.dispose())


def test_sql_batch_full_journey(db_env) -> None:
    """SQL 批量旅程端到端：解析 → 注册 DRAFT → 提交评审 → 标准发布，真实 MySQL 闭环。"""

    async def _run() -> None:
        sf = db_env["session_factory"]
        owner_id = db_env["owner_id"]
        reviewer_id = db_env["reviewer_id"]

        # 1) 批量解析：多度量 SQL → 候选（含派生比率列）
        async with sf() as s:
            parsed = await infer_sql_batch(s, sql=_JOURNEY_SQL, domain_code="fin")
        assert parsed["candidates"], "Doris/多度量 SQL 应产出候选"
        codes = {c["metric_code"] for c in parsed["candidates"] if c["metric_code"]}
        assert len(codes) >= 2, "应产出至少 2 个可注册候选"
        # 派生比率列（avg_price/refund_rate）应产出且带 derived 标记
        derived = [c for c in parsed["candidates"] if c.get("derived")]
        assert len(derived) >= 1, "P0-3d：派生比率列应产出候选"
        for c in parsed["candidates"]:
            assert c["metric_code"], "指定域后候选编码应已生成（非空 4 段）"

        # 2) 批量注册：DRAFT + batch_id/raw_sql 落库
        request = MetricSqlBatchRegisterRequest(
            domain="fin", candidates=parsed["candidates"]
        )
        async with sf() as s:
            svc = MetricService(s)
            result = await svc.batch_register_from_sql(
                request,
                actor_id=owner_id,
                role="metric_owner",
                user_domain="fin",
            )
            batch_id = result["batch_id"]
            assert result["candidates"], "应返回候选结果"
            failed = [
                f"{c['metric_code']}({c['validation_errors']})"
                for c in result["candidates"]
                if c["status"] == "VALIDATION_ERROR"
            ]
            assert not failed, f"批量注册不应失败: {failed}"
            ok_codes = [
                c["metric_code"] for c in result["candidates"] if c["status"] == "DRAFT"
            ]
            assert len(ok_codes) >= 2, "应至少创建 2 个 DRAFT"
            # service 不自动 commit（API 端点层才 commit）——真实旅程中后续读依赖提交
            await s.commit()

        # 3) 逐条提交评审（DRAFT → REVIEW）
        async with sf() as s:
            svc = MetricService(s)
            submitted: list[str] = []
            for code in ok_codes:
                m = await svc.submit_metric(
                    code,
                    MetricSubmitRequest(change_reason="批量旅程集成测试提交评审"),
                    actor_id=owner_id,
                    role="metric_owner",
                    user_domain="fin",
                )
                assert m.status == "REVIEW", f"{code} 应处于 REVIEW，实际 {m.status}"
                # 批次标识与原始口径 SQL 已落库（口径溯源闭合）
                assert m.batch_id == batch_id, f"{code} batch_id 应等于 {batch_id}"
                assert m.raw_sql, f"{code} raw_sql 应落库"
                assert "trade_order" in (m.raw_sql or ""), "raw_sql 应含整句口径原文"
                submitted.append(code)
            await s.commit()

        # 4) 标准发布（REVIEW → PUBLISHED），评审人角色 domain_admin
        async with sf() as s:
            svc = MetricService(s)
            for code in submitted:
                m = await svc.approve_metric(
                    code,
                    MetricApproveRequest(mode="standard"),
                    actor_id=reviewer_id,
                    role="domain_admin",
                    user_domain="fin",
                )
                assert m.status == "PUBLISHED", f"{code} 应发布为 PUBLISHED，实际 {m.status}"
                assert m.effective_version is not None, "发布后应有生效版本"
            await s.commit()

        # 5) 二次批量解析不应报错（幂等性冒烟：重复解析同一 SQL 仍可产出候选）
        async with sf() as s:
            parsed2 = await infer_sql_batch(s, sql=_JOURNEY_SQL, domain_code="fin")
        assert parsed2["candidates"], "重复解析同一 SQL 仍应产出候选"

    asyncio.run(_run())


def test_sql_batch_journey_owner_domain_gate(db_env) -> None:
    """批量旅程域门禁：metric_owner 跨域提交 → 整批 FORBIDDEN（真实 DB 校验）。"""

    async def _run() -> None:
        sf = db_env["session_factory"]
        owner_id = db_env["owner_id"]

        async with sf() as s:
            parsed = await infer_sql_batch(s, sql=_JOURNEY_SQL, domain_code="fin")
        request = MetricSqlBatchRegisterRequest(
            domain="sales", candidates=parsed["candidates"]  # 跨域（owner 属 fin）
        )
        async with sf() as s:
            svc = MetricService(s)
            with pytest.raises(BusinessError) as exc_info:
                await svc.batch_register_from_sql(
                    request,
                    actor_id=owner_id,
                    role="metric_owner",
                    user_domain="fin",
                )
        assert exc_info.value.error_code == "FORBIDDEN"

    asyncio.run(_run())
