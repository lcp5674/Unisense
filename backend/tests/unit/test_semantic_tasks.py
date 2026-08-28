"""语义模块定时任务（app/tasks/semantic_tasks.py）单测。

覆盖 P1-5（健康恶化告警闭环）：
- 每日刷新发现 CRITICAL/WARNING → 定向通知指标 Owner + 备份 Owner（不依赖订阅偏好）
- 健康（HEALTHY）不触发通知
- 通知失败 best-effort 不阻断每日刷新
"""

from __future__ import annotations

import datetime as _dt
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


def _mock_db_health(metrics: list) -> MagicMock:
    """health 专用 mock：refresh_health_scores 分批加载的 execute 序列。

    T17（审查修复）：execute = COUNT(总数) → 批量查询[metrics] → 批量查询[]（结束）。
    """
    db = MagicMock()
    count_res = MagicMock()
    count_res.scalar_one.return_value = len(metrics)
    batch_res = MagicMock()
    batch_res.scalars.return_value.all.return_value = metrics
    empty_res = MagicMock()
    empty_res.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[count_res, batch_res, empty_res])
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
    db = _mock_db_health([metric])
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
    db = _mock_db_health([metric])
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
    db = _mock_db_health([metric])
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
    db = _mock_db_health([metric])
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


# ---- P3-14: DSD 7 天超期升级提醒 ----


@pytest.fixture
def _patch_dsd_overdue_env() -> None:
    """check_dsd_overdue 依赖替换为可控 mock（函数体内 import）。"""
    patches = [
        patch("app.db.mysql.async_session_factory"),
        patch("app.services.notify.service.NotifyService"),
    ]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


def _dsd_metric(overdue: bool) -> SimpleNamespace:
    """构造 DATA_SOURCE_DROPPED 指标；overdue=True 时 updated_at 早于 7 天前。"""
    old = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=8)
    recent = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1)
    return SimpleNamespace(
        id=7,
        metric_code="dsd_gmv_d",
        owner_id=11,
        backup_owner_id=12,
        status="DATA_SOURCE_DROPPED",
        deleted_at=None,
        domain="sales",
        updated_at=old if overdue else recent,
    )


async def test_check_dsd_overdue_notifies_owner_on_overage(_patch_dsd_overdue_env) -> None:
    """P3-14: DSD 超 7 天 → 定向升级提醒 Owner + 备份 Owner（metric.source_dropped）。"""
    from app.db.mysql import async_session_factory
    from app.services.notify.service import NotifyService
    from app.tasks.semantic_tasks import check_dsd_overdue

    metric = _dsd_metric(overdue=True)
    db = _mock_db([metric])
    async_session_factory.return_value = _AsyncCM(db)

    notif_svc = MagicMock()
    notif_svc.notify_user = AsyncMock()
    NotifyService.return_value = notif_svc

    reminded = await check_dsd_overdue({})

    assert reminded == [metric.id]
    # Owner + 备份 Owner 均收到升级提醒
    assert notif_svc.notify_user.await_count == 2
    calls = notif_svc.notify_user.await_args_list
    assert {c.kwargs["user_id"] for c in calls} == {11, 12}
    for c in calls:
        assert c.kwargs["event_type"] == "metric.source_dropped"
        assert c.kwargs["payload"]["reason"] == "dsd_overdue"
        assert c.kwargs["payload"]["metric_code"] == "dsd_gmv_d"


async def test_check_dsd_overdue_no_metrics_no_notify(_patch_dsd_overdue_env) -> None:
    """P3-14: 查询无命中 DSD 指标 → 不提醒（updated_at 过滤由 DB WHERE 负责）。"""
    from app.db.mysql import async_session_factory
    from app.services.notify.service import NotifyService
    from app.tasks.semantic_tasks import check_dsd_overdue

    db = _mock_db([])
    async_session_factory.return_value = _AsyncCM(db)

    notif_svc = MagicMock()
    notif_svc.notify_user = AsyncMock()
    NotifyService.return_value = notif_svc

    reminded = await check_dsd_overdue({})

    assert reminded == []
    notif_svc.notify_user.assert_not_awaited()



# ---- T-3（第七轮技术债）：补两个已注册 cron 任务的测试 ----
# check_pending_version_timeouts / check_emergency_review_overdue 已进 worker 调度但零测试。
# 覆盖「超时默认接受转正 / 无超时无动作」「紧急发布补审超时标记 / 无超时无动作」。


def _pending_conf(metric_id: int = 1, version: int = 3) -> SimpleNamespace:
    return SimpleNamespace(metric_id=metric_id, version=version)


async def test_check_pending_version_timeouts_accepts_expired() -> None:
    """T-3: 存在 deadline 已过的 PENDING 确认 → 按 (metric_id, version) 分组默认接受转正。"""
    with patch("app.db.mysql.async_session_factory") as factory, patch(
        "app.services.semantic.service.MetricService"
    ) as metric_svc_cls:
        from app.tasks.semantic_tasks import check_pending_version_timeouts

        db = _mock_db([_pending_conf(1, 3), _pending_conf(1, 3), _pending_conf(2, 1)])
        factory.return_value = _AsyncCM(db)

        svc = MagicMock()
        svc.auto_accept_timeout = AsyncMock(
            return_value=_metric(code="sales_gmv_d", status="PUBLISHED")
        )
        metric_svc_cls.return_value = svc

        promoted = await check_pending_version_timeouts({})

        # 两个 (metric_id, version) 分组各调一次，成功即入 promoted
        assert promoted == [1, 2]
        assert svc.auto_accept_timeout.await_count == 2
        assert svc.auto_accept_timeout.await_args_list[0].args == (1, 3)
        assert svc.auto_accept_timeout.await_args_list[1].args == (2, 1)
        db.commit.assert_awaited()


async def test_check_pending_version_timeouts_no_expired() -> None:
    """T-3: 无超时确认 → 返回空且不触碰 MetricService（不提交）。"""
    with patch("app.db.mysql.async_session_factory") as factory, patch(
        "app.services.semantic.service.MetricService"
    ) as metric_svc_cls:
        from app.tasks.semantic_tasks import check_pending_version_timeouts

        db = _mock_db([])
        factory.return_value = _AsyncCM(db)

        svc = MagicMock()
        svc.auto_accept_timeout = AsyncMock()
        metric_svc_cls.return_value = svc

        promoted = await check_pending_version_timeouts({})

        assert promoted == []
        svc.auto_accept_timeout.assert_not_awaited()


async def test_check_pending_version_timeouts_accept_failure_isolated() -> None:
    """T-3: 单组超时接受异常 → 记日志跳过，不阻断其余组转正。"""
    with patch("app.db.mysql.async_session_factory") as factory, patch(
        "app.services.semantic.service.MetricService"
    ) as metric_svc_cls:
        from app.tasks.semantic_tasks import check_pending_version_timeouts

        db = _mock_db([_pending_conf(1, 3), _pending_conf(2, 1)])
        factory.return_value = _AsyncCM(db)

        svc = MagicMock()
        svc.auto_accept_timeout = AsyncMock(side_effect=[None, _metric(status="PUBLISHED")])
        metric_svc_cls.return_value = svc

        promoted = await check_pending_version_timeouts({})

        # 第一组返回 None（未转正）不入列，第二组成功入列
        assert promoted == [2]


async def test_check_emergency_review_overdue_flags_overdue() -> None:
    """T-3: 紧急发布超 24h 未补审 → 标记为 overdue。"""
    with patch("app.db.mysql.async_session_factory") as factory:
        from app.tasks.semantic_tasks import check_emergency_review_overdue

        metric = _metric(code="emergency_sales_d", status="PUBLISHED")
        metric.emergency_publish = True
        metric.emergency_reviewed_at = None
        db = _mock_db([metric])
        factory.return_value = _AsyncCM(db)

        overdue = await check_emergency_review_overdue({})

        assert overdue == [metric.id]


async def test_check_emergency_review_overdue_no_metric() -> None:
    """T-3: 无超时紧急发布 → 返回空。"""
    with patch("app.db.mysql.async_session_factory") as factory:
        from app.tasks.semantic_tasks import check_emergency_review_overdue

        db = _mock_db([])
        factory.return_value = _AsyncCM(db)

        overdue = await check_emergency_review_overdue({})

        assert overdue == []
