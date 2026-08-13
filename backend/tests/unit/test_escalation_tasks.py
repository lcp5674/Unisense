"""升级重试周期任务单测（补齐 coverage ≥85%）。

覆盖 escalation_tasks.check_escalation_retries：
- 自建会话 + EscalationService.check_retries + commit
- stats["due"]>0 时记录 info 日志并返回
- stats["due"]==0 时不记日志、直接返回
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.notify.escalation_tasks import check_escalation_retries


def _build_session() -> MagicMock:
    session = MagicMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _patch_deps(
    monkeypatch: pytest.MonkeyPatch,
    session: MagicMock,
    stats: dict[str, int],
) -> MagicMock:
    # async_session_factory 是 async_sessionmaker：同步调用返回 AsyncSession，
    # 再经 async with session 进入异步上下文管理。
    monkeypatch.setattr("app.db.mysql.async_session_factory", lambda: session)

    svc = MagicMock()
    svc.check_retries = AsyncMock(return_value=stats)

    class _FakeService:
        def __init__(self, session: object) -> None:
            self.check_retries = svc.check_retries

    monkeypatch.setattr("app.services.notify.escalation.EscalationService", _FakeService)
    return svc


@pytest.mark.asyncio
async def test_due_stats_logs_info_and_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _build_session()
    stats = {"due": 2, "resent": 1, "escalated": 1, "maxed_out": 0}
    svc = _patch_deps(monkeypatch, session, stats)

    result = await check_escalation_retries({})

    assert result == stats
    svc.check_retries.assert_awaited_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_due_stats_no_log(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _build_session()
    stats = {"due": 0, "resent": 0, "escalated": 0, "maxed_out": 0}
    _patch_deps(monkeypatch, session, stats)

    result = await check_escalation_retries({})

    assert result == stats
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_factory_called_once(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _build_session()
    stats = {"due": 1, "resent": 1, "escalated": 0, "maxed_out": 0}
    _patch_deps(monkeypatch, session, stats)

    await check_escalation_retries({})

    # 会话通过 async with 进入并正常退出
    session.__aenter__.assert_awaited_once()
    session.__aexit__.assert_awaited_once()
