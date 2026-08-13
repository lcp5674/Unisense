"""冲突仲裁 SLA 自动升级任务单测（审查发现：仲裁无 SLA，冲突可永久滞留 OPEN）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.conflict import Conflict, ConflictStatus, ConflictType
from app.services.conflict.sla_tasks import _CONFLICT_SLA_DAYS, auto_escalate_overdue


def _conflict(
    conflict_id: str = "CF-001",
    status: ConflictStatus = ConflictStatus.OPEN,
    age_days: float = _CONFLICT_SLA_DAYS + 1,
) -> Conflict:
    return Conflict(
        id=1,
        conflict_id=conflict_id,
        metric_a=1,
        metric_b=2,
        type=ConflictType.SAME_NAME_DIFF_DEF,
        status=status,
        created_at=datetime.now(UTC) - timedelta(days=age_days),
    )


async def test_auto_escalates_overdue_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超时 OPEN 冲突被自动升级为 ESCALATED。"""
    overdue = _conflict()
    db = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: [overdue]))
    )
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("app.db.mysql.async_session_factory", lambda: db)

    escalated_by = {}

    class _FakeSvc:
        def __init__(self, db: object) -> None:
            pass

        async def escalate(self, conflict_id: str, req: object) -> Conflict:
            escalated_by[conflict_id] = req
            return overdue

    monkeypatch.setattr(
        "app.services.conflict.service.ConflictService", _FakeSvc
    )

    result = await auto_escalate_overdue({})
    assert result["scanned"] == 1
    assert result["escalated"] == 1
    assert "CF-001" in escalated_by
    # 自动升级携带 SLA 备注
    assert escalated_by["CF-001"].note == "SLA 超时自动升级"


async def test_skips_recent_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """SLA 窗口内（未超时）的冲突不升级。"""
    db = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: []))
    )
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("app.db.mysql.async_session_factory", lambda: db)

    # 查询带 cutoff 条件（created_at < cutoff）
    result = await auto_escalate_overdue({})
    assert result == {"scanned": 0, "escalated": 0}


async def test_single_failure_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """单条升级失败不阻断整批（其余仍升级）。"""
    c1 = _conflict("CF-A")
    c2 = _conflict("CF-B")
    db = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: [c1, c2]))
    )
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("app.db.mysql.async_session_factory", lambda: db)

    class _FakeSvc:
        def __init__(self, db: object) -> None:
            pass

        async def escalate(self, conflict_id: str, req: object) -> Conflict:
            if conflict_id == "CF-A":
                raise RuntimeError("状态机拒绝")
            return c2

    monkeypatch.setattr(
        "app.services.conflict.service.ConflictService", _FakeSvc
    )

    result = await auto_escalate_overdue({})
    assert result["scanned"] == 2
    assert result["escalated"] == 1
