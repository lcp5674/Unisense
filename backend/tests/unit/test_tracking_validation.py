"""埋点事件校验测试（P2-8 画像污染加固）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.tracking import TrackEventRequest, create_event
from app.core.exceptions import ValidationError


def _ctx_user() -> MagicMock:
    u = MagicMock()
    u.id = 1
    return u


async def _call(payload: TrackEventRequest) -> None:
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    await create_event(payload, db=db, user=_ctx_user())


async def test_unknown_event_type_rejected() -> None:
    with pytest.raises(ValidationError):
        await _call(TrackEventRequest(event_type="fake_spam_event"))


async def test_allowed_metric_event_accepted() -> None:
    await _call(TrackEventRequest(event_type="metric_detail_view", target_id="gmv_day"))
    await _call(TrackEventRequest(event_type="consume_query", target_id="gmv_day"))


async def test_context_too_many_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        await _call(
            TrackEventRequest(
                event_type="metric_search",
                context={f"k{i}": 1 for i in range(20)},
            )
        )


async def test_context_long_key_rejected() -> None:
    with pytest.raises(ValidationError):
        await _call(
            TrackEventRequest(
                event_type="metric_search",
                context={"x" * 100: 1},
            )
        )


async def test_context_long_value_rejected() -> None:
    with pytest.raises(ValidationError):
        await _call(
            TrackEventRequest(
                event_type="metric_search",
                context={"k": "v" * 300},
            )
        )


async def test_bounded_context_accepted() -> None:
    await _call(
        TrackEventRequest(
            event_type="metric_search",
            context={"keyword": "gmv", "page": 1},
        )
    )
