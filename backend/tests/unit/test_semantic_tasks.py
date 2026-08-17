"""语义模块定时任务（app/tasks/semantic_tasks.py）单测。

覆盖 P1-5（健康恶化告警闭环）：
- 每日刷新发现 CRITICAL/WARNING → 定向通知指标 Owner + 备份 Owner（不依赖订阅偏好）
- 健康（HEALTHY）不触发通知
- 通知失败 best-effort 不阻断每日刷新
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _AsyncCM:
    """异步上下文管理器桩（async with async_session_factory() as db）。"""

    def __init__(self, db: MagicMock) -> None:
        self._db = db

    async def __aenter__(self) -> MagicMock:
        return self._db

    async def __aexit__(self, *args: object) -> None:
        return None


def _metric(
    code: str = "sales_gmv_d",
    owner_id: int = 11,
    backup_owner_id: int | None = 12,
    status: str = "PUBLISHED",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        metric_code=code,
        owner_id=owner_id,
        backup_owner_id=backup_owner_id,
        status=status,
        deleted_at=None,
        domain="sales",
    )


def _health(level: str, score: int) -> SimpleNamespace:
    return SimpleNamespace(
        level=level,
        score=score,
        missing_dimensions=["quality", "activity"] if level == "CRITICAL" else [],
    )


def _mock_db(metrics: list) -> MagicMock:
    """构造可 await 的 mock 会话：execute 返回 metrics、commit/rollback 可 await。"""
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: metrics))
    )
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture
def _patch_health_refresh_env() -> None:
    """把 refresh_health_scores 的 DB/评分/仓库/通知依赖全部替换为可控 mock。

    semantic_tasks 在函数体内 ``from X import Y`` 导入，patch 目标是 Y 的定义模块。
    """
    patches = [
        patch("app.db.mysql.async_session_factory"),
        patch("app.services.semantic.health_scorer.HealthScorer"),
        patch("app.services.semantic.repository.MetricRepository"),
        patch("app.services.notify.service.NotifyService"),
    ]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


async def test_refresh_health_notifies_owner_on_critical(_patch_health_refresh_env) -> None:
    """P1-5: CRITICAL 指标 → 定向通知 Owner + 备份 Owner（metric.health_critical）。"""
    from app.db.mysql import async_session_factory
    from app.services.notify.service import NotifyService
    from app.services.semantic.health_scorer import HealthScorer
    from app.services.semantic.repository import MetricRepository
    from app.tasks.semantic_tasks import refresh_health_scores

    metric = _metric()
    db = _mock_db([metric])
    async_session_factory.return_value = _AsyncCM(db)

    scorer = MagicMock()
    scorer.calculate = AsyncMock(return_value=_health("CRITICAL", 40))
    HealthScorer.return_value = scorer

    repo = MagicMock()
    repo.save_health_score = AsyncMock()
    MetricRepository.return_value = repo

    notif_svc = MagicMock()
    notif_svc.notify_user = AsyncMock()
    NotifyService.return_value = notif_svc

    count = await refresh_health_scores({})

    assert count == 1
    # Owner + 备份 Owner 均收到定向告警
    assert notif_svc.notify_user.await_count == 2
    owner_calls = notif_svc.notify_user.await_args_list
    assert {c.kwargs["user_id"] for c in owner_calls} == {11, 12}
    for c in owner_calls:
        assert c.kwargs["event_type"] == "metric.health_critical"
        assert c.kwargs["payload"]["metric_code"] == "sales_gmv_d"
        assert c.kwargs["payload"]["level"] == "CRITICAL"


async def test_refresh_health_notifies_backup_owner_once_if_same(_patch_health_refresh_env) -> None:
    """备份 Owner 与 Owner 相同时只通知一次（不重复打扰）。"""
    from app.db.mysql import async_session_factory
    from app.services.notify.service import NotifyService
    from app.services.semantic.health_scorer import HealthScorer
    from app.services.semantic.repository import MetricRepository
    from app.tasks.semantic_tasks import refresh_health_scores

    metric = _metric(owner_id=11, backup_owner_id=11)
    db = _mock_db([metric])
    async_session_factory.return_value = _AsyncCM(db)
    scorer = MagicMock()
    scorer.calculate = AsyncMock(return_value=_health("WARNING", 60))
    HealthScorer.return_value = scorer
    MetricRepository.return_value = MagicMock(save_health_score=AsyncMock())
    notif_svc = MagicMock()
    notif_svc.notify_user = AsyncMock()
    NotifyService.return_value = notif_svc

    await refresh_health_scores({})

    assert notif_svc.notify_user.await_count == 1
    assert notif_svc.notify_user.await_args.kwargs["user_id"] == 11


async def test_refresh_health_healthy_metric_no_notification(_patch_health_refresh_env) -> None:
    """健康（HEALTHY）指标不触发告警通知。"""
    from app.db.mysql import async_session_factory
    from app.services.notify.service import NotifyService
    from app.services.semantic.health_scorer import HealthScorer
    from app.services.semantic.repository import MetricRepository
    from app.tasks.semantic_tasks import refresh_health_scores

    metric = _metric()
    db = _mock_db([metric])
    async_session_factory.return_value = _AsyncCM(db)
    scorer = MagicMock()
    scorer.calculate = AsyncMock(return_value=_health("HEALTHY", 95))
    HealthScorer.return_value = scorer
    MetricRepository.return_value = MagicMock(save_health_score=AsyncMock())
    notif_svc = MagicMock()
    notif_svc.notify_user = AsyncMock()
    NotifyService.return_value = notif_svc

    count = await refresh_health_scores({})

    assert count == 1
    notif_svc.notify_user.assert_not_awaited()


async def test_refresh_health_notify_failure_does_not_break(_patch_health_refresh_env) -> None:
    """通知失败 best-effort：不阻断评分刷新与落库。"""
    from app.db.mysql import async_session_factory
    from app.services.notify.service import NotifyService
    from app.services.semantic.health_scorer import HealthScorer
    from app.services.semantic.repository import MetricRepository
    from app.tasks.semantic_tasks import refresh_health_scores

    metric = _metric()
    db = _mock_db([metric])
    async_session_factory.return_value = _AsyncCM(db)
    scorer = MagicMock()
    scorer.calculate = AsyncMock(return_value=_health("CRITICAL", 40))
    HealthScorer.return_value = scorer
    repo = MagicMock()
    repo.save_health_score = AsyncMock()
    MetricRepository.return_value = repo
    notif_svc = MagicMock()
    notif_svc.notify_user = AsyncMock(side_effect=RuntimeError("notify down"))
    NotifyService.return_value = notif_svc

    count = await refresh_health_scores({})

    assert count == 1
    repo.save_health_score.assert_awaited_once()


# ---- P1-7: 灰度超期强制回收 ----


@pytest.fixture
def _patch_gray_expiry_env() -> None:
    """check_experimental_expiry 依赖替换为可控 mock（函数体内 import）。"""
    patches = [
        patch("app.db.mysql.async_session_factory"),
        patch("app.services.semantic.service.MetricService"),
        patch("app.services.notify.service.NotifyService"),
    ]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


async def test_check_experimental_expiry_recycles_overage(_patch_gray_expiry_env) -> None:
    """P1-7: 超期 EXPERIMENTAL → 通知 Owner + 强制回收 EXPERIMENTAL→DRAFT。"""
    from app.db.mysql import async_session_factory
    from app.services.notify.service import NotifyService
    from app.services.semantic.service import MetricService
    from app.tasks.semantic_tasks import check_experimental_expiry

    metric = _metric(status="EXPERIMENTAL", owner_id=11, backup_owner_id=12)
    db = _mock_db([metric])
    async_session_factory.return_value = _AsyncCM(db)

    svc = MagicMock()
    svc.recycle_expired_gray = AsyncMock(return_value=metric)
    MetricService.return_value = svc

    notif_svc = MagicMock()
    notif_svc.notify_user = AsyncMock()
    NotifyService.return_value = notif_svc

    recycled = await check_experimental_expiry({})

    assert recycled == [metric.id]
    # 通知 Owner + 备份 Owner
    assert notif_svc.notify_user.await_count == 2
    assert {c.kwargs["user_id"] for c in notif_svc.notify_user.await_args_list} == {11, 12}
    # 系统触发回收
    svc.recycle_expired_gray.assert_awaited_once_with(metric.metric_code, actor_id=0)


async def test_check_experimental_expiry_no_overage_no_action(_patch_gray_expiry_env) -> None:
    """无超期灰度指标 → 不通知不回收。"""
    from app.db.mysql import async_session_factory
    from app.services.notify.service import NotifyService
    from app.services.semantic.service import MetricService
    from app.tasks.semantic_tasks import check_experimental_expiry

    db = _mock_db([])
    async_session_factory.return_value = _AsyncCM(db)

    svc = MagicMock()
    svc.recycle_expired_gray = AsyncMock()
    MetricService.return_value = svc
    NotifyService.return_value = MagicMock(notify_user=AsyncMock())

    recycled = await check_experimental_expiry({})

    assert recycled == []
    svc.recycle_expired_gray.assert_not_awaited()
