"""可观测性服务单元测试（TD §12.10 / FR-16）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import NotFoundError, UnisenseError
from app.models.feedback import Feedback
from app.services.observability.schemas import FeedbackCreate, FeedbackResponse
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


async def test_submit_feedback_richness_fields() -> None:
    """反馈丰富度字段透传：category/priority/source_url 落库。"""
    svc, repo = await _svc()
    out = await svc.submit_feedback(
        FeedbackCreate(
            user_id=3,
            target_type="metric",
            comment="加个导出",
            category="feature",
            priority="high",
            source_url="/catalog",
        )
    )
    assert out.category == "feature"
    assert out.priority == "high"
    assert out.source_url == "/catalog"
    # 未显式传时回退默认值
    out2 = await svc.submit_feedback(FeedbackCreate(user_id=3, target_type="term"))
    assert out2.category == "improvement"
    assert out2.priority == "medium"
    assert out2.source_url is None


async def test_submit_feedback_invalid_rating() -> None:
    svc, repo = await _svc()
    with pytest.raises(UnisenseError):
        await svc.submit_feedback(FeedbackCreate(user_id=3, target_type="term", rating=9))


async def test_feedback_create_category_priority_fallback() -> None:
    """非法分类/优先级回退默认，不因脏数据 422。"""
    c = FeedbackCreate(target_type="term", category="spam", priority="urgent")
    assert c.category == "improvement"
    assert c.priority == "medium"
    assert FeedbackCreate(target_type="term", category="bug", priority="low").category == "bug"


async def test_list_feedback_delegates() -> None:
    svc, repo = await _svc()
    repo.list_feedback = AsyncMock(
        return_value=([Feedback(id=1, target_type="metric", target_id="m1")], 7)
    )
    repo.resolve_target_names = AsyncMock(return_value={1: "指标1"})
    out = await svc.list_feedback("term", None, 1, 20)
    assert out["total"] == 7
    assert out["page"] == 1
    assert out["page_size"] == 20
    assert len(out["items"]) == 1
    # 服务端解析的对象名称映射一并返回（前端直显，避免 N+1 探测）
    assert out["target_names"] == {1: "指标1"}
    repo.resolve_target_names.assert_awaited_once()


async def test_feedback_response_target_name_field() -> None:
    """FeedbackResponse 透出 target_name（前端直显对象名称）。"""
    resp = FeedbackResponse.from_model(
        Feedback(id=1, user_id=3, target_type="metric", target_id="m1")
    )
    resp.target_name = "指标1"
    assert resp.target_name == "指标1"
    assert resp.model_dump()["target_name"] == "指标1"


async def test_nps_stats_delegates() -> None:
    svc, repo = await _svc()
    repo.nps_stats = AsyncMock(return_value={"total": 3, "score": 33.33})
    assert await svc.nps_stats() == {"total": 3, "score": 33.33}


async def test_quality_events_delegates() -> None:
    svc, repo = await _svc()
    repo.quality_events = AsyncMock(return_value=[{"id": 1}])
    out = await svc.quality_events(20)
    assert out == [{"id": 1}]


async def test_update_feedback_status_valid() -> None:
    """合法状态更新追加到 comment 并提交。"""
    svc, repo = await _svc()
    fb = Feedback(id=1, user_id=1, target_type="term", comment="NPS: 9/10")
    repo.get_feedback = AsyncMock(return_value=fb)
    out = await svc.update_feedback_status(1, "adopted", resolver_id=5, resolution_note="done")
    assert "status=adopted" in out.comment
    assert "done" in out.comment
    repo.save_feedback.assert_awaited()
    repo.commit.assert_awaited()


async def test_update_feedback_status_rejects_invalid() -> None:
    """非法状态必须被白名单拒绝，禁止任意字符串写入 comment。"""
    svc, repo = await _svc()
    repo.get_feedback = AsyncMock(
        return_value=Feedback(id=1, user_id=1, target_type="term", comment="c")
    )
    with pytest.raises(UnisenseError) as exc:
        await svc.update_feedback_status(1, "hacked")
    assert exc.value.error_code == "INVALID_FEEDBACK_STATUS"
    repo.save_feedback.assert_not_awaited()


async def test_update_feedback_status_missing_not_found() -> None:
    """反馈不存在时抛 NotFoundError。"""
    svc, repo = await _svc()
    repo.get_feedback = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.update_feedback_status(999, "adopted")


async def test_update_feedback_status_idempotent_no_growth() -> None:
    """重复更新同一状态不得使 comment 无界增长（幂等去重）。"""
    svc, repo = await _svc()
    fb = Feedback(id=1, user_id=1, target_type="term", comment="NPS: 9/10")
    repo.get_feedback = AsyncMock(return_value=fb)
    await svc.update_feedback_status(1, "adopted")
    first = fb.comment
    # 同一状态下再次更新，不追加（时间戳不同，但语义状态相同）
    await svc.update_feedback_status(1, "adopted")
    assert fb.comment == first
    # 真正的新状态仍会追加
    await svc.update_feedback_status(1, "in_progress", resolver_id=2)
    assert "status=in_progress" in fb.comment
    assert fb.comment != first


# ---- 质疑闭环（P2#19：质疑→澄清→修订）----


async def test_clarify_feedback_valid() -> None:
    """clarifying 状态本人澄清：内容落库、状态回 in_progress、clarified_at 置位。"""
    svc, repo = await _svc()
    fb = Feedback(
        id=1,
        user_id=3,
        target_type="metric",
        comment="口径冲突",
        status="clarifying",
        resolver_id=5,
    )
    repo.get_feedback = AsyncMock(return_value=fb)
    out = await svc.clarify_feedback(1, "按门诊人次统计，含退号", user_id=3)
    assert out.status == "in_progress"
    assert out.clarification == "按门诊人次统计，含退号"
    assert out.clarified_at is not None
    repo.save_feedback.assert_awaited()
    repo.commit.assert_awaited()


async def test_clarify_feedback_rejects_non_clarifying() -> None:
    """非 clarifying 状态不可澄清。"""
    svc, repo = await _svc()
    fb = Feedback(id=1, user_id=3, target_type="metric", status="pending")
    repo.get_feedback = AsyncMock(return_value=fb)
    with pytest.raises(UnisenseError) as exc:
        await svc.clarify_feedback(1, "说明", user_id=3)
    assert exc.value.error_code == "INVALID_FEEDBACK_STATUS"
    repo.save_feedback.assert_not_awaited()


async def test_clarify_feedback_rejects_non_owner() -> None:
    """非提交人本人不可代答澄清（PLAT-2：防止他人污染口径说明）。"""
    svc, repo = await _svc()
    fb = Feedback(id=1, user_id=3, target_type="metric", status="clarifying")
    repo.get_feedback = AsyncMock(return_value=fb)
    with pytest.raises(UnisenseError) as exc:
        await svc.clarify_feedback(1, "说明", user_id=99)
    assert exc.value.error_code == "FORBIDDEN"
    repo.save_feedback.assert_not_awaited()


async def test_clarify_feedback_rejects_empty() -> None:
    """空澄清内容拒绝。"""
    svc, repo = await _svc()
    fb = Feedback(id=1, user_id=3, target_type="metric", status="clarifying")
    repo.get_feedback = AsyncMock(return_value=fb)
    with pytest.raises(UnisenseError) as exc:
        await svc.clarify_feedback(1, "   ", user_id=3)
    assert exc.value.error_code == "INVALID_CLARIFICATION"


async def test_clarify_feedback_missing_not_found() -> None:
    """反馈不存在抛 NotFoundError。"""
    svc, repo = await _svc()
    repo.get_feedback = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.clarify_feedback(999, "说明", user_id=3)


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


async def test_overview_stats_delegates() -> None:
    svc, repo = await _svc()
    repo.overview_stats = AsyncMock(return_value={"sources": {"total": 1}})
    assert await svc.overview_stats() == {"sources": {"total": 1}}


async def test_submit_nps_valid() -> None:
    svc, repo = await _svc()
    out = await svc.submit_nps(user_id=3, score=9)
    assert out.id == 1
    assert out.comment == "NPS: 9/10"
    # NPS 语义解耦：0-10 写进 nps_score，rating 保持 None（不再污染 1-5 语义）
    assert out.nps_score == 9
    assert out.rating is None
    repo.save_feedback.assert_awaited()
    repo.commit.assert_awaited()


async def test_update_feedback_status_sets_resolver_fields() -> None:
    """状态变化写入 resolver_id / resolved_at。"""
    svc, repo = await _svc()
    fb = Feedback(id=1, user_id=1, target_type="term", comment="原始", status="pending")
    repo.get_feedback = AsyncMock(return_value=fb)
    out = await svc.update_feedback_status(1, "adopted", resolver_id=9, resolution_note="已采纳")
    assert out.status == "adopted"
    assert out.resolver_id == 9
    assert out.resolved_at is not None


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
