"""可观测性服务单元测试（TD §12.10 / FR-16）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import UnisenseError
from app.models.feedback import Feedback
from app.services.observability.schemas import FeedbackCreate
from app.services.observability.service import ObservabilityService


def _persist(f: Feedback) -> Feedback:
    f.id = 1
    return f


async def _svc() -> tuple[ObservabilityService, MagicMock]:
    db = MagicMock()
    svc = ObservabilityService(db)
    repo = MagicMock()
    repo.save_feedback = AsyncMock(side_effect=_persist)
    repo.commit = AsyncMock()
    svc._repo = repo  # noqa: SLF001
    return svc, repo


async def test_submit_feedback_valid() -> None:
    svc, repo = await _svc()
    out = await svc.submit_feedback(
        FeedbackCreate(user_id=3, target_type="term", rating=5, comment="good")
    )
    assert out.id == 1
    repo.save_feedback.assert_awaited()
    repo.commit.assert_awaited()


async def test_submit_feedback_invalid_rating() -> None:
    svc, repo = await _svc()
    with pytest.raises(UnisenseError):
        await svc.submit_feedback(FeedbackCreate(user_id=3, target_type="term", rating=9))
