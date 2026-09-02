"""参照数据「只初始化一次」标记（seed_medical_reference）单元测试。

覆盖点（对应「首次部署零手工、迭代重建不重灌」的保证）：
    - is_reference_seeded：seed_marker 存在即 True（DB 持久，容器重建后仍可判）；
    - run(force=False)：已初始化（marker 存在）→ skipped，不触碰参照数据；
    - run(force=False)：未初始化 → 执行清除/灌入并写 marker；
    - run(force=True)：无视 marker 强制重灌（运维手动通道）；
    - _resolve_owner_id：优先解析实际 admin 用户 id（干净库自增 id 非硬编码 3）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import seed_medical_reference as smr


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class _Row:
    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


def _make_session(*, marker: Any = None, admin: Any = None) -> MagicMock:
    """会话替身：get(SeedMarker) 按 marker 返回；execute(User 查询) 按 admin 返回。"""
    session = MagicMock()

    async def _get(model: Any, pk: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(model, "__name__", "") == "SeedMarker":
            return marker
        return None

    session.get = AsyncMock(side_effect=_get)

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
        result = MagicMock()
        if "user" in str(stmt).lower():
            result.scalar_one_or_none = MagicMock(return_value=admin)
        else:
            result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    session.execute = AsyncMock(side_effect=_execute)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _session_factory(session: MagicMock) -> MagicMock:
    factory = MagicMock()

    def _call() -> MagicMock:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    factory.side_effect = _call
    return factory


def _patch_seed_fns(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """把 run() 内部的清除/灌入函数替换为替身（防真实 DB 写入）。"""
    fns = {
        name: AsyncMock(return_value=0)
        for name in (
            "clear_mock_dimensions",
            "clear_mock_terms",
            "seed_domains",
            "seed_terms",
            "seed_dimensions",
        )
    }
    fns["seed_domains"].return_value = 3
    fns["seed_terms"].return_value = 5
    fns["seed_dimensions"].return_value = 2
    for name, mock in fns.items():
        monkeypatch.setattr(smr, name, mock, raising=False)
    return fns


# --------------------------------------------------------------------------- #
# is_reference_seeded
# --------------------------------------------------------------------------- #
async def test_is_reference_seeded_false_when_marker_missing() -> None:
    """无 marker 行 → 未初始化（首次部署）。"""
    session = _make_session(marker=None)
    assert await smr.is_reference_seeded(session) is False


async def test_is_reference_seeded_true_when_marker_exists() -> None:
    """marker 行存在（含迭代重建后）→ 已初始化。"""
    session = _make_session(marker=_Row(name=smr.MARKER_NAME))
    assert await smr.is_reference_seeded(session) is True


# --------------------------------------------------------------------------- #
# run() 的 marker 语义
# --------------------------------------------------------------------------- #
async def test_run_skipped_when_seeded_without_force(monkeypatch: pytest.MonkeyPatch) -> None:
    """已初始化且未 force → skipped，不执行任何 seed、不写 marker。"""
    session = _make_session(marker=_Row(name=smr.MARKER_NAME))
    monkeypatch.setattr(smr, "async_session_factory", _session_factory(session), raising=False)
    fns = _patch_seed_fns(monkeypatch)

    result = await smr.run(force=False)

    assert result["status"] == "skipped"
    assert result["reason"] == "already_seeded"
    for mock in fns.values():
        mock.assert_not_awaited()
    session.commit.assert_not_awaited()


async def test_run_executes_and_marks_when_not_seeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """未初始化 → 执行清除/灌入，commit 并写 marker。"""
    session = _make_session(marker=None)
    monkeypatch.setattr(smr, "async_session_factory", _session_factory(session), raising=False)
    fns = _patch_seed_fns(monkeypatch)
    monkeypatch.setattr(smr, "_resolve_owner_id", AsyncMock(return_value=7), raising=False)

    result = await smr.run(force=False)

    assert result["status"] == "ok"
    assert result["owner_id"] == 7
    for mock in fns.values():
        mock.assert_awaited_once()
    session.commit.assert_awaited_once()
    # _mark_reference_seeded 在无行时 db.add(SeedMarker)
    added = list(session.add.call_args_list)
    assert added, "应写入 seed_marker 行"
    assert added[0].args[0].name == smr.MARKER_NAME


async def test_run_force_ignores_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """force=True → 无视已有 marker 强制重灌（运维手动通道）。"""
    session = _make_session(marker=_Row(name=smr.MARKER_NAME))
    monkeypatch.setattr(smr, "async_session_factory", _session_factory(session), raising=False)
    fns = _patch_seed_fns(monkeypatch)
    monkeypatch.setattr(smr, "_resolve_owner_id", AsyncMock(return_value=3), raising=False)

    result = await smr.run(force=True)

    assert result["status"] == "ok"
    for mock in fns.values():
        mock.assert_awaited_once()
    session.commit.assert_awaited_once()


async def test_run_rolls_back_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """执行异常 → 回滚并上抛（不写 marker、不静默成功）。"""
    session = _make_session(marker=None)
    monkeypatch.setattr(smr, "async_session_factory", _session_factory(session), raising=False)
    fns = _patch_seed_fns(monkeypatch)
    fns["seed_dimensions"].side_effect = RuntimeError("db boom")

    with pytest.raises(RuntimeError, match="db boom"):
        await smr.run(force=False)

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


# --------------------------------------------------------------------------- #
# owner 动态解析
# --------------------------------------------------------------------------- #
async def test_resolve_owner_prefers_admin_user() -> None:
    """优先取 username=admin 的实际 id（干净库自增 id 非硬编码 3）。"""
    session = _make_session(admin=_Row(id=7, username="admin"))
    assert await smr._resolve_owner_id(session) == 7


async def test_resolve_owner_falls_back_to_first_user() -> None:
    """无 admin 用户 → 回退任一用户（不悬挂）。"""
    session = _make_session(admin=None)
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )
    assert await smr._resolve_owner_id(session) == smr.TERM_OWNER_ID
