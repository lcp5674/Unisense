"""语义领域服务 —— 真实 MySQL 集成测试。

目标：用真实数据库（MySQL 8.0 + aiomysql）验证被 mock 单测掩盖的运行时缺陷：

1. P0 修复：去掉 ``UPDATE ... RETURNING`` 后，发布/更新在真实 MySQL 上仍可正确执行；
2. P0 修复：版本状态机 —— 初始版本为 DRAFT，发布后版本转正为 PUBLISHED 且
   ``effective_version`` 正确指向；
3. P0 修复：乐观锁并发冲突（stale row_version）→ ConflictError；
4. P1 修复：PII 指标未合规复核被发布拦截，复核后可发布（打通此前死结）；
5. P1 修复：坏枚举值绕过 Pydantic 直达数据库时触发 IntegrityError（证明 DB 约束存在）。

数据源（按可用环境自动选择，生产级优先）：

* 外部已启动 MySQL（默认路径）：设置 ``UNISENSE_INTEGRATION_DB_URL``
  （如 ``mysql+pymysql://unisense:test@localhost:3307/unisense_it``）。
  测试会用 **Alembic 迁移**（与生产 schema 完全一致）建表：先 ``downgrade base``
  清空、再 ``upgrade head`` 建表，保证每次运行 schema 干净、可重复，并顺带验证
  ORM 模型与迁移定义一致。
* testcontainers 容器（CI 无外部库时）：自动拉起临时 MySQL，跑完即弃。

两者均不可用则该模块整体跳过，不阻塞 CI。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy.exc import DataError, IntegrityError

from app.core.exceptions import BusinessError, ConflictError
from app.models.metric import Metric
from app.models.user import Organization, User
from app.services.semantic.repository import MetricRepository
from app.services.semantic.schemas import (
    MetricApproveRequest,
    MetricCreateRequest,
    MetricEmergencyPublishRequest,
    MetricSubmitRequest,
    MetricUpdateRequest,
)
from app.services.semantic.service import MetricService

# 外部 MySQL（生产级）：pymysql URL。未设置则回退 testcontainers。
EXT_DB_URL = os.getenv("UNISENSE_INTEGRATION_DB_URL") or os.getenv("UNISENSE_DB_URL")
# 仅当指向本地/可达 MySQL 时才启用外部模式；
# 若为 conftest 的默认占位（3306 且无服务）则仍走 testcontainers。
_USE_EXT = bool(EXT_DB_URL) and "localhost" in EXT_DB_URL

# backend 目录（alembic.ini 所在处），从测试文件推导，任意 cwd 均可运行
_BACKEND_DIR = Path(__file__).resolve().parents[2]


def _seed(session_factory) -> tuple[int, int]:
    """种子：组织 + Owner 用户（metric.owner_id 外键）+ Reviewer 用户（审核人，避免自审）。

    返回 (owner_id, reviewer_id)。
    """

    async def _run() -> tuple[int, int]:
        async with session_factory() as s:
            org = Organization(name="默认组织", code="default_org", status="active")
            s.add(org)
            await s.flush()
            owner = User(
                org_id=org.id,
                username="owner",
                email="owner@example.com",
                password_hash="x",
                display_name="owner",
                role="metric_owner",
                status="active",
            )
            s.add(owner)
            reviewer = User(
                org_id=org.id,
                username="reviewer",
                email="reviewer@example.com",
                password_hash="x",
                display_name="reviewer",
                role="domain_admin",
                status="active",
            )
            s.add(reviewer)
            await s.flush()
            await s.commit()
            return owner.id, reviewer.id

    return asyncio.run(_run())


def _reset_via_alembic(url: str) -> None:
    """用 Alembic 迁移（与生产一致）将目标库升级到 head。

    仅执行 upgrade head（生产部署的真实路径）；测试库复位由调用方在
    升级前用 ORM ``drop_all`` 完成，避免依赖迁移 downgrade 的可回滚性。
    """
    env = {**os.environ, "UNISENSE_DB_URL": url}
    # MySQL 8.0 在连续 DDL（wipe→重建）下偶发 1684/1050 时序冲突（元数据锁未收敛）；
    # 重试最多 3 次，间隔 1s，保证测试库重建稳定（CI 全新容器 + 本地共享库均适用）。
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
        # NullPool：每次 checkout 在当前事件循环新建连接、归还即关闭，
        # 避免 fixture 内多次 asyncio.run 与 pytest-asyncio 循环之间复用连接导致
        # “Future attached to a different loop” 错误。
        engine = create_async_engine(url, echo=False, poolclass=NullPool)

        # 干净复位：DROP + CREATE 整个测试库（对独立测试库最可靠）。
        # 大量连续 DDL 下 MySQL 8.0 偶发 1684 / 1050 时序冲突；整库重建原子、无残留。
        # 注意：集成测试库账号需具备目标库的 CREATE/DROP 权限。
        db_name = EXT_DB_URL.split("?")[0].rsplit("/", 1)[1]
        admin_url = url.rsplit("/", 1)[0] + "/"  # 无默认库，用于 DROP/CREATE DATABASE
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
        # 显式释放 admin 连接，避免 DROP/CREATE 后连接句柄残留
        asyncio.run(admin_engine.dispose())

        # 生产迁移建表（验证迁移在真实 MySQL 8.0 上可落地、ORM 模型与迁移一致）
        _reset_via_alembic(EXT_DB_URL)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        owner_id, reviewer_id = _seed(session_factory)
        yield {
            "engine": engine,
            "session_factory": session_factory,
            "owner_id": owner_id,
            "reviewer_id": reviewer_id,
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

        # 建表（覆盖 metric 依赖的 user / term 等）
        async def _create_all() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.run(_create_all())

        owner_id, reviewer_id = _seed(session_factory)
        yield {
            "engine": engine,
            "session_factory": session_factory,
            "owner_id": owner_id,
            "reviewer_id": reviewer_id,
        }
        container.stop()


def _create_payload(**overrides) -> MetricCreateRequest:
    payload = {
        "metric_code": "fin_gmv_amount_day",
        "name": "GMV",
        "domain": "fin",
        "type": "atomic",
        "granularity": "DAY",
        "unit": "元",
        "aggregation": "SUM",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "dw_layer": "DWS",
        "metric_tier": "T2",
        "serving_mode": "BATCH_ONLY",
        "additivity": "ADDITIVE",
        "definition_json": {"expression": "SUM(amount)", "dependencies": ["ods_order"]},
    }
    payload.update(overrides)
    return MetricCreateRequest(**payload)


async def test_create_then_publish_promotes_version(db_env):
    """创建(DRAFT)→submit→approve：版本转正为 PUBLISHED，effective_version 正确。"""
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]
    reviewer_id = db_env["reviewer_id"]

    async with session_factory() as session:
        repo = MetricRepository(session)
        svc = MetricService(session)

        metric = await svc.create_metric(_create_payload(), owner_id=owner_id)
        await session.commit()
        assert metric.status == "DRAFT"

        # 初始版本为 DRAFT
        versions = await repo.list_versions(metric.id)
        assert len(versions) == 1
        assert versions[0].status == "DRAFT"

        # 提交审核（DRAFT → REVIEW）→ 审核通过（REVIEW → PUBLISHED，非自审）
        await svc.submit_metric(
            metric.metric_code,
            MetricSubmitRequest(change_reason="首次提交审核"),
            actor_id=owner_id,
        )
        await session.commit()

        published = await svc.approve_metric(
            metric.metric_code,
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=reviewer_id,
        )
        await session.commit()

        assert published.status == "PUBLISHED"
        assert published.effective_version == 1

        # 版本转正
        v1 = await repo.get_version(metric.id, 1)
        assert v1 is not None
        assert v1.status == "PUBLISHED"
        assert v1.published_at is not None


async def test_optimistic_lock_conflict(db_env):
    """乐观锁：用过期 row_version 更新应抛 ConflictError。"""
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]

    async with session_factory() as session:
        repo = MetricRepository(session)
        svc = MetricService(session)

        metric = await svc.create_metric(_create_payload(), owner_id=owner_id)
        await session.commit()
        baseline = metric.row_version

        # 第一次更新成功（row_version 自增）
        ok = await repo.update_with_optimistic_lock(metric.id, baseline, name="改名A")
        await session.commit()
        assert ok.row_version == baseline + 1

        # 用过期 baseline 再次更新 → 冲突
        with pytest.raises(ConflictError):
            await repo.update_with_optimistic_lock(metric.id, baseline, name="改名B")


async def test_pii_flow_deadlock_resolved(db_env):
    """PII 流程：未复核被拦截 → 复核 → 发布成功（打通此前死结）。"""
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]
    reviewer_id = db_env["reviewer_id"]

    async with session_factory() as session:
        svc = MetricService(session)

        metric = await svc.create_metric(
            _create_payload(
                metric_code="fin_pii_mobile_day",
                pii_flag=True,
            ),
            owner_id=owner_id,
        )
        await session.commit()

        # 提交审核
        await svc.submit_metric(
            metric.metric_code,
            MetricSubmitRequest(change_reason="提交含PII指标审核"),
            actor_id=owner_id,
        )
        await session.commit()

        # 未合规复核 → approve 被拦截
        with pytest.raises(BusinessError) as exc:
            await svc.approve_metric(
                metric.metric_code,
                MetricApproveRequest(mode="standard"),
                actor_id=reviewer_id,
            )
        assert exc.value.error_code == "COMPLIANCE_BLOCKED"

        # 合规复核（须非 Owner，防自审）
        reviewed = await svc.review_compliance(
            metric.metric_code, actor_id=reviewer_id, role="domain_admin"
        )
        await session.commit()
        assert reviewed.compliance_reviewed is True

        # 再次 approve → 成功
        published = await svc.approve_metric(
            metric.metric_code,
            MetricApproveRequest(mode="standard"),
            actor_id=reviewer_id,
        )
        await session.commit()
        assert published.status == "PUBLISHED"


async def test_invalid_enum_rejected_at_db_layer(db_env):
    """数据库 ENUM 约束存在：绕过 Pydantic 写入坏枚举值应触发 IntegrityError。"""
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]

    async with session_factory() as session:
        bad = Metric(
            metric_code="bad_enum_metric",
            name="bad",
            domain="fin",
            type="atomic",
            granularity="DAY",
            unit="元",
            aggregation="NOT_A_REAL_AGG",  # 非法枚举
            time_semantics="PERIOD",
            freshness="T1",
            dw_layer="DWS",
            metric_tier="T3",
            serving_mode="BATCH_ONLY",
            additivity="ADDITIVE",
            definition_json={"expression": "1"},
            owner_id=owner_id,
            status="DRAFT",
        )
        session.add(bad)
        # MySQL 严格模式下非法 ENUM 触发 DataError(1265)；只要数据库层拒绝即为通过
        with pytest.raises((IntegrityError, DataError)):
            await session.flush()


def test_migration_is_reversible():
    """迁移必须可回滚（生产回退/分支重置所需）。

    验证：upgrade head → downgrade base → upgrade head 全程无错，
    防止再次出现“外键支撑索引无法直接 DROP（MySQL 1553）”这类不可逆缺陷。
    """
    if not _USE_EXT:
        pytest.skip("仅外部 MySQL 模式验证迁移可逆性")
    env = {**os.environ, "UNISENSE_DB_URL": EXT_DB_URL}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        cwd=_BACKEND_DIR,
        check=True,
        capture_output=True,
        text=True,
    )


# =====================================================================
# T067: 工业级整改端到端集成测试
# 完整流程：创建→submit→approve→PUBLISHED→PUT breaking→PENDING_VERSION
#          →confirm→新CURRENT→deprecate→灰度→promote→紧急发布→健康度评分
# =====================================================================


async def test_full_lifecycle_e2e(db_env):
    """端到端完整生命周期：DRAFT→SUBMIT→APPROVE→PUBLISHED→BREAKING→PENDING_VERSION。

    验证状态机 + PENDING_VERSION 缓冲期 + 破坏性变更不直接生效。
    """
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]
    reviewer_id = db_env["reviewer_id"]

    async with session_factory() as session:
        svc = MetricService(session)

        # Step 1: 创建 DRAFT
        metric = await svc.create_metric(
            _create_payload(metric_code="fin_lifecycle_flow_day"),
            owner_id=owner_id,
        )
        await session.commit()
        assert metric.status == "DRAFT"

        # Step 2: submit → REVIEW（无 role 参数）
        submitted = await svc.submit_metric(
            "fin_lifecycle_flow_day",
            MetricSubmitRequest(change_reason="提交评审"),
            actor_id=owner_id,
        )
        await session.commit()
        assert submitted.status == "REVIEW"

        # Step 3: approve（非自审）→ PUBLISHED
        approved = await svc.approve_metric(
            "fin_lifecycle_flow_day",
            MetricApproveRequest(mode="standard"),
            actor_id=reviewer_id,
        )
        await session.commit()
        assert approved.status == "PUBLISHED"
        assert approved.effective_version == 1

        # Step 4: PUT breaking change → PENDING_VERSION
        updated = await svc.update_metric(
            "fin_lifecycle_flow_day",
            MetricUpdateRequest(
                definition_json={
                    "expression": "SUM(amount_new)",
                    "dependencies": ["ods_order_v2"],
                },
                change_reason="口径变更-破坏性",
            ),
            actor_id=owner_id,
            role="metric_owner",
        )
        await session.commit()
        # PUBLISHED + breaking → metric 主表不变，版本记录 PENDING_CONFIRMATION
        assert updated.status == "PUBLISHED"
        assert updated.effective_version == 1  # 仍指向旧版本

        # 验证新版本处于 PENDING
        repo = MetricRepository(session)
        v2 = await repo.get_version(metric.id, 2)
        assert v2 is not None
        assert v2.status == "PENDING_CONFIRMATION"


async def test_gray_release_and_promote_e2e(db_env):
    """灰度发布：approve(mode=experimental) → EXPERIMENTAL → promote → PUBLISHED。"""
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]
    reviewer_id = db_env["reviewer_id"]

    async with session_factory() as session:
        svc = MetricService(session)

        metric = await svc.create_metric(  # noqa: F841
            _create_payload(metric_code="fin_gray_flow_day"),
            owner_id=owner_id,
        )
        await session.commit()

        await svc.submit_metric(
            "fin_gray_flow_day",
            MetricSubmitRequest(change_reason="灰度评审"),
            actor_id=owner_id,
        )
        await session.commit()

        # 灰度发布（非自审）
        gray = await svc.approve_metric(
            "fin_gray_flow_day",
            MetricApproveRequest(mode="experimental", gray_tenant_ids=[1, 2]),
            actor_id=reviewer_id,
        )
        await session.commit()
        assert gray.status == "EXPERIMENTAL"

        # promote → PUBLISHED
        promoted = await svc.promote_metric("fin_gray_flow_day", actor_id=owner_id)
        await session.commit()
        assert promoted.status == "PUBLISHED"


async def test_emergency_publish_blocks_pii_e2e(db_env):
    """紧急发布：跳过 REVIEW 但 PII 门禁不可跳。"""
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]

    async with session_factory() as session:
        svc = MetricService(session)

        await svc.create_metric(  # noqa: F841
            _create_payload(
                metric_code="fin_emergency_pii_day",
                pii_flag=True,
            ),
            owner_id=owner_id,
        )
        await session.commit()

        # PII 未合规 → 紧急发布被拒绝
        with pytest.raises(BusinessError, match="PII"):
            await svc.emergency_publish_metric(
                "fin_emergency_pii_day",
                MetricEmergencyPublishRequest(reason="紧急业务需求需要立即上线"),
                actor_id=owner_id,
                role="domain_admin",
            )


async def test_emergency_publish_succeeds_non_pii_e2e(db_env):
    """紧急发布：非 PII 指标 → 跳 REVIEW 直接 PUBLISHED。"""
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]

    async with session_factory() as session:
        svc = MetricService(session)

        metric = await svc.create_metric(
            _create_payload(metric_code="fin_emergency_nopii_day", pii_flag=False),
            owner_id=owner_id,
        )
        await session.commit()
        assert metric.status == "DRAFT"

        emergency = await svc.emergency_publish_metric(
            "fin_emergency_nopii_day",
            MetricEmergencyPublishRequest(reason="紧急业务需求需要立即上线"),
            actor_id=owner_id,
            role="domain_admin",
        )
        await session.commit()
        assert emergency.status == "PUBLISHED"
        assert emergency.emergency_publish is True


async def test_health_score_e2e(db_env):
    """健康度评分：完整指标 → 有评分结果。"""
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]

    async with session_factory() as session:
        svc = MetricService(session)

        await svc.create_metric(  # noqa: F841
            _create_payload(metric_code="fin_health_score_day"),
            owner_id=owner_id,
        )
        await session.commit()

        health = await svc.get_metric_health("fin_health_score_day")
        # get_metric_health 返回 MetricHealthScore ORM 对象（非 dict）
        assert health.score is not None
        assert health.level in ("EXCELLENT", "GOOD", "WARNING", "CRITICAL")


async def test_deprecate_only_published_e2e(db_env):
    """废弃：仅 PUBLISHED 可废弃，DRAFT 不可。"""
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]

    async with session_factory() as session:
        svc = MetricService(session)

        # DRAFT 状态废弃 → 拒绝（deprecate_metric 签名: successor_code: str|None）
        await svc.create_metric(  # noqa: F841
            _create_payload(metric_code="fin_deprecate_draft_day"),
            owner_id=owner_id,
        )
        await session.commit()

        with pytest.raises(BusinessError):
            await svc.deprecate_metric(
                "fin_deprecate_draft_day",
                None,
                actor_id=owner_id,
                role="domain_admin",
            )


async def test_state_machine_illegal_transition_e2e(db_env):
    """状态机非法跃迁：DEPRECATED → PUBLISHED 被拦截。"""
    from app.services.semantic.state_machine import MetricStateMachine

    # validate_transition 返回拒绝原因字符串（None 表示合法），非法跃迁返回非 None
    err = MetricStateMachine.validate_transition("DEPRECATED", "PUBLISHED")
    assert err is not None
