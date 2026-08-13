"""可观测性服务单元测试（TD §12.10 / FR-16）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import NotFoundError, UnisenseError
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


async def test_list_feedback_delegates() -> None:
    svc, repo = await _svc()
    repo.list_feedback = AsyncMock(return_value=[Feedback(id=1)])
    out = await svc.list_feedback("term", 10)
    assert len(out) == 1
    repo.list_feedback.assert_awaited_once_with("term", 10)


async def test_quality_stats_delegates() -> None:
    svc, repo = await _svc()
    repo.quality_stats = AsyncMock(return_value={"total": 3})
    assert await svc.quality_stats() == {"total": 3}


async def test_api_stats_delegates() -> None:
    svc, repo = await _svc()
    repo.api_stats = AsyncMock(return_value={"total": 10})
    assert await svc.api_stats() == {"total": 10}


async def test_notification_stats_delegates() -> None:
    svc, repo = await _svc()
    repo.notification_stats = AsyncMock(return_value={"sent": 1})
    assert await svc.notification_stats() == {"sent": 1}


async def test_lineage_stats_delegates() -> None:
    svc, repo = await _svc()
    repo.lineage_stats = AsyncMock(return_value={"edges": 7})
    assert await svc.lineage_stats() == {"edges": 7}


async def test_submit_nps_valid() -> None:
    svc, repo = await _svc()
    out = await svc.submit_nps(user_id=3, score=9)
    assert out.id == 1
    assert out.comment == "NPS: 9/10"
    repo.save_feedback.assert_awaited()
    repo.commit.assert_awaited()


async def test_submit_nps_invalid_score() -> None:
    svc, _ = await _svc()
    with pytest.raises(UnisenseError):
        await svc.submit_nps(user_id=3, score=11)


async def test_update_feedback_status_adopted() -> None:
    svc, repo = await _svc()
    fb = Feedback(id=1, comment="原始反馈")
    repo.get_feedback = AsyncMock(return_value=fb)
    out = await svc.update_feedback_status(1, "adopted", resolver_id=5, resolution_note="已处理")
    assert "status=adopted" in (out.comment or "")
    assert "by=5" in (out.comment or "")
    repo.save_feedback.assert_awaited()
    repo.commit.assert_awaited()


async def test_update_feedback_status_not_found() -> None:
    svc, repo = await _svc()
    repo.get_feedback = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.update_feedback_status(999, "adopted")
