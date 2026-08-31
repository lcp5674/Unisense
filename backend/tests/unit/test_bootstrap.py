"""部署自举（scripts/bootstrap.py）单元测试。

覆盖点（对应生产部署「零人工介入」的保证）：
    - 阻塞步骤（admin / 主题域+字典）幂等，二次运行 skipped；
    - 阻塞步骤失败 → 退出码 1（compose 自动重试）；尽力步骤失败 → 仍 0（不阻断启动）；
    - ES 索引：不可用则跳过；重建/空索引则必灌；已填充则跳过；超阈值保护；
    - 分布式锁：Redis 不可用**照常执行**（不可误判为「他人持有」而漏灌）；
      确被他人持有则跳过；
    - 回归防护：复用路径不得触发 ``engine.dispose()``；自动化路径不做
      ``migrate_existing_domains`` 批量 UPDATE。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import bootstrap


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
def _result(value: Any) -> MagicMock:
    """构造 db.execute() 的返回替身（含 scalar_one_or_none）。"""
    return MagicMock(scalar_one_or_none=MagicMock(return_value=value))


def _make_session(
    org: Any = None,
    admin: Any = None,
    pending: int = 0,
    *,
    preexisting: bool = False,
) -> MagicMock:
    """按 SQL 文本路由查询结果的会话替身（避免依赖真实 MySQL）。

    Args:
        preexisting: True 时所有查询均返回「已存在」对象，用于模拟二次启动
            （首次自举完成后的状态）。
    """
    session = MagicMock()
    counter = {"next_id": 100}

    def _add(obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            counter["next_id"] += 1
            obj.id = counter["next_id"]

    session.add = MagicMock(side_effect=_add)

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
        if preexisting:
            return _result(MagicMock())
        text = str(stmt).lower()
        if "organization" in text:
            return _result(org)
        if "user" in text:
            return _result(admin)
        return _result(None)  # subject_domain / system_dict：不存在 → 走创建分支

    session.execute = AsyncMock(side_effect=_execute)
    session.scalar = AsyncMock(return_value=pending)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _session_factory(session: MagicMock) -> MagicMock:
    """构造 async_session_factory 替身（支持 async with）。"""
    factory = MagicMock()

    def _call() -> MagicMock:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    factory.side_effect = _call
    return factory


def _redis_pool(set_result: Any = None, raise_on_set: bool = False) -> MagicMock:
    pool = MagicMock()

    async def _set(*args: Any, **kwargs: Any) -> Any:
        if raise_on_set:
            raise ConnectionError("redis unavailable")
        return set_result

    pool.set = AsyncMock(side_effect=_set)
    pool.get = AsyncMock(return_value=None)
    pool.delete = AsyncMock(return_value=1)
    return pool


# --------------------------------------------------------------------------- #
# 回归防护：engine.dispose 不得泄漏进复用路径
# --------------------------------------------------------------------------- #
async def test_seed_admin_does_not_dispose_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """``seed_admin(db)`` 只 flush 不提交、更不得释放全局引擎。

    CLI 入口 ``seed()`` 才会 dispose；自举复用同一函数，若触发 dispose 会回收
    uvicorn 后续要用的连接池（历史缺陷防护）。
    """
    disposed: list[bool] = []
    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock(side_effect=lambda: disposed.append(True))

    monkeypatch.setattr("scripts.seed_admin.engine", fake_engine, raising=False)
    monkeypatch.setattr(
        "scripts.seed_admin.hash_password", AsyncMock(return_value="hashed"), raising=False
    )

    session = _make_session()
    summary = await bootstrap_seed_admin(session)

    assert disposed == []
    assert summary["admin_created"] is True
    assert summary["org_created"] is True


async def bootstrap_seed_admin(session: MagicMock) -> dict[str, Any]:
    """调用被测函数（延迟 import，确保 monkeypatch 生效）。"""
    from scripts.seed_admin import seed_admin

    return await seed_admin(session)


# --------------------------------------------------------------------------- #
# 幂等性
# --------------------------------------------------------------------------- #
async def test_seed_core_idempotent_second_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """数据已存在时二次自举全部 skipped（不重复创建、不报错）。"""
    session = _make_session(preexisting=True)
    monkeypatch.setattr(
        "scripts.bootstrap.async_session_factory", _session_factory(session), raising=False
    )
    monkeypatch.setattr(
        "scripts.seed_admin.hash_password", AsyncMock(return_value="hashed"), raising=False
    )

    admin_result, domains_result = await bootstrap._seed_core()

    assert admin_result.status == "skipped"
    assert admin_result.detail["org_created"] is False
    assert domains_result.status == "skipped"
    assert domains_result.detail["domains_created"] == 0


async def test_seed_core_uses_real_admin_id_as_domain_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主题域责任人取实际 admin id（非硬编码 1），避免自增 id 漂移时落错人。"""
    session = _make_session()
    monkeypatch.setattr(
        "scripts.bootstrap.async_session_factory", _session_factory(session), raising=False
    )
    monkeypatch.setattr(
        "scripts.seed_admin.hash_password", AsyncMock(return_value="hashed"), raising=False
    )

    created_owner_ids: list[int] = []
    original_add = session.add

    def _capture_add(obj: Any) -> None:
        if type(obj).__name__ == "SubjectDomain":
            created_owner_ids.append(obj.owner_id)
        original_add(obj)

    session.add = MagicMock(side_effect=_capture_add)

    admin_result, domains_result = await bootstrap._seed_core()
    admin_id = admin_result.detail["admin_id"]

    assert created_owner_ids, "应创建主题域"
    assert set(created_owner_ids) == {admin_id}
    assert domains_result.detail["owner_id"] == admin_id


# --------------------------------------------------------------------------- #
# 自动化路径不做批量 UPDATE
# --------------------------------------------------------------------------- #
async def test_bootstrap_does_not_run_migrate_existing_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自举不执行 ``migrate_existing_domains``（会把存量指标 domain 批量改写）。

    该动作属不可逆数据迁移，保留给运维显式执行 CLI；若自动化路径调用它，
    存量升级时可能误改业务数据。
    """

    async def _boom(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("自举不得执行 migrate_existing_domains")

    monkeypatch.setattr("scripts.seed_domains_dicts.migrate_existing_domains", _boom, raising=False)

    session = _make_session()
    monkeypatch.setattr(
        "scripts.bootstrap.async_session_factory", _session_factory(session), raising=False
    )
    monkeypatch.setattr(
        "scripts.seed_admin.hash_password", AsyncMock(return_value="hashed"), raising=False
    )

    _admin, domains = await bootstrap._seed_core()
    assert domains.status in {"ok", "skipped"}


# --------------------------------------------------------------------------- #
# ES 步骤
# --------------------------------------------------------------------------- #
def _fake_es(enabled: bool = True, healthy: bool = True) -> MagicMock:
    es = MagicMock()
    es.enabled = enabled
    es.health = AsyncMock(return_value=healthy)
    es.search = AsyncMock(return_value={"hits": {"total": {"value": 0}}})
    es.close = AsyncMock()
    return es


class _FakeIndexer:
    def __init__(self, created: dict[str, bool], synced: dict[str, int] | None = None) -> None:
        self._created = created
        self._synced = synced or {"metric_idx": 0, "term_idx": 0}
        self.sync_called = False

    async def ensure_indexes(self, **_: Any) -> dict[str, bool]:
        return self._created

    async def sync_all(self) -> dict[str, int]:
        self.sync_called = True
        return self._synced


def _patch_es(
    monkeypatch: pytest.MonkeyPatch, es: MagicMock, indexer: _FakeIndexer, pending: int = 0
) -> MagicMock:
    session = _make_session(pending=pending)
    monkeypatch.setattr(
        "scripts.bootstrap.async_session_factory", _session_factory(session), raising=False
    )
    monkeypatch.setattr("app.core.es_client.get_es_client", lambda: es, raising=False)
    monkeypatch.setattr(
        "app.services.search.es_indexer.EsIndexer", lambda _db: indexer, raising=False
    )
    return session


async def test_es_step_skipped_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """ES 未启用/不可达 → skipped（不算失败，检索走 MySQL 降级）。"""
    indexer = _FakeIndexer(created={"metric_idx": False, "term_idx": False})
    _patch_es(monkeypatch, _fake_es(enabled=False, healthy=False), indexer)

    result = await bootstrap._step_es(pool=None)
    assert result.status == "skipped"
    assert result.detail["reason"] == "es_unavailable"


async def test_es_step_syncs_when_index_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """索引为空 → 必须灌数据（空索引会让全局检索无结果）。"""
    indexer = _FakeIndexer(created={"metric_idx": False, "term_idx": False}, synced={"a": 1})
    _patch_es(monkeypatch, _fake_es(), indexer)

    result = await bootstrap._step_es(pool=None)
    assert indexer.sync_called is True
    assert result.status == "ok"
    assert "synced" in result.detail


async def test_es_step_skips_sync_when_populated(monkeypatch: pytest.MonkeyPatch) -> None:
    """索引已填充且未重建 → 跳过全量重灌（避免每次启动都重灌）。"""
    indexer = _FakeIndexer(created={"metric_idx": False, "term_idx": False})
    es = _fake_es()
    es.search = AsyncMock(return_value={"hits": {"total": {"value": 42}}})
    _patch_es(monkeypatch, es, indexer)

    result = await bootstrap._step_es(pool=None)
    assert indexer.sync_called is False
    assert result.detail["sync"] == "skipped_indexes_populated"


async def test_es_step_skips_sync_over_max_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    """超过阈值时不自灌（逐条写放大会拖垮启动），交由离线任务处理。"""
    monkeypatch.setenv("UNISENSE_BOOTSTRAP_ES_SYNC_MAX_DOCS", "5")
    indexer = _FakeIndexer(created={"metric_idx": False, "term_idx": False})
    _patch_es(monkeypatch, _fake_es(), indexer, pending=100)

    result = await bootstrap._step_es(pool=None)
    assert indexer.sync_called is False
    assert result.detail["sync"] == "skipped_too_large"


# --------------------------------------------------------------------------- #
# 分布式锁语义
# --------------------------------------------------------------------------- #
async def test_guard_runs_when_redis_unavailable() -> None:
    """Redis 不可用 → 照常执行（不可误判为他人持有而漏灌必做步骤）。"""
    async with bootstrap._guard_once(_redis_pool(raise_on_set=True), "es-sync", 60) as run:
        assert run is True


async def test_guard_skips_when_held_by_other_replica() -> None:
    """锁被他人持有 → 本副本跳过（对正在灌，写入幂等）。"""
    async with bootstrap._guard_once(_redis_pool(set_result=None), "es-sync", 60) as run:
        assert run is False


async def test_guard_runs_when_acquired() -> None:
    """抢到锁 → 执行，并在退出时释放自己持有的锁（不误删他人锁）。"""
    pool = _redis_pool(set_result=True)
    pool.get = AsyncMock(return_value=None)  # owner 不匹配 → 不删
    async with bootstrap._guard_once(pool, "es-sync", 60) as run:
        assert run is True
    pool.delete.assert_not_awaited()


# --------------------------------------------------------------------------- #
# 退出码矩阵
# --------------------------------------------------------------------------- #
async def test_exit_code_zero_when_best_effort_step_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """尽力步骤（neo4j）失败不阻断启动，退出码仍为 0。"""

    async def _boom() -> bootstrap.StepResult:
        raise RuntimeError("neo4j down")

    monkeypatch.setattr("scripts.bootstrap._seed_core", _ok_core, raising=False)
    monkeypatch.setattr("scripts.bootstrap._step_es", lambda _p: _ok_step("es"), raising=False)
    monkeypatch.setattr("scripts.bootstrap._step_neo4j", _boom, raising=False)
    monkeypatch.setattr("scripts.bootstrap._release_resources", _noop, raising=False)
    monkeypatch.setattr(bootstrap, "_bootstrap_lock", _no_lock, raising=False)
    monkeypatch.setenv("UNISENSE_BOOTSTRAP_ENABLED", "true")
    monkeypatch.delenv("UNISENSE_BOOTSTRAP_STEPS", raising=False)

    assert await bootstrap.main([]) == 0


async def test_exit_code_one_when_blocking_step_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """阻塞步骤（admin）失败 → 退出码 1，容器重启重试。"""

    async def _boom() -> tuple[bootstrap.StepResult, bootstrap.StepResult]:
        raise RuntimeError("db down")

    monkeypatch.setattr("scripts.bootstrap._seed_core", _boom, raising=False)
    monkeypatch.setattr("scripts.bootstrap._step_es", lambda _p: _ok_step("es"), raising=False)
    monkeypatch.setattr("scripts.bootstrap._step_neo4j", lambda: _ok_step("neo4j"), raising=False)
    monkeypatch.setattr("scripts.bootstrap._release_resources", _noop, raising=False)
    monkeypatch.setattr(bootstrap, "_bootstrap_lock", _no_lock, raising=False)
    monkeypatch.delenv("UNISENSE_BOOTSTRAP_STEPS", raising=False)

    assert await bootstrap.main([]) == 1


async def test_dry_run_executes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """--dry-run 只打印计划。"""

    async def _boom() -> tuple[bootstrap.StepResult, bootstrap.StepResult]:
        raise AssertionError("dry-run 不应执行任何步骤")

    monkeypatch.setattr("scripts.bootstrap._seed_core", _boom, raising=False)
    assert await bootstrap.main(["--dry-run"]) == 0


# --------------------------------------------------------------------------- #
# 公共替身
# --------------------------------------------------------------------------- #
async def _noop() -> None:
    return None


def _ok_step(name: str) -> Any:
    async def _inner(*_args: Any, **_kwargs: Any) -> bootstrap.StepResult:
        return bootstrap.StepResult(name, "ok")

    return _inner()


async def _ok_core() -> tuple[bootstrap.StepResult, bootstrap.StepResult]:
    return bootstrap.StepResult("admin", "ok"), bootstrap.StepResult("domains", "ok")


@bootstrap.asynccontextmanager
async def _no_lock() -> Any:
    yield None


def test_enabled_steps_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """步骤开关解析：未知名忽略、可裁剪、可整体关闭。"""
    monkeypatch.delenv("UNISENSE_BOOTSTRAP_STEPS", raising=False)
    assert bootstrap._enabled_steps() == bootstrap.DEFAULT_STEPS

    monkeypatch.setenv("UNISENSE_BOOTSTRAP_STEPS", "admin,bogus,es")
    assert bootstrap._enabled_steps() == ("admin", "es")

    monkeypatch.setenv("UNISENSE_BOOTSTRAP_ENABLED", "false")
    assert bootstrap._enabled_steps() == ()


def test_execute_step_converts_exception_to_failed() -> None:
    """步骤异常归一为 failed（由编排层按阻塞性决定退出码，不冒泡中断后续步骤）。"""

    async def _boom() -> bootstrap.StepResult:
        raise RuntimeError("boom")

    result = asyncio.run(bootstrap._execute_step("x", _boom, timeout=5))
    assert result.status == "failed"
    assert "boom" in result.detail["error"]
