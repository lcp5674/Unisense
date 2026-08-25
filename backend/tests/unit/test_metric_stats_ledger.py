"""指标资产账本单测（P1：活跃/僵尸/重复建设清单）。

覆盖：
- asset_ledger 僵尸判定：复用 HealthScorer 活跃度维度（近 30 天无更新）+ 零引用
- 有引用/近期更新的指标归为活跃；空数据降级（0 而非 500）
- 重复建设信号：SAME_DEF_DIFF_NAME 预检标记 → duplicate_count；其他冲突不算
- API 端点 metric_asset_ledger：统一信封 + Pydantic 校验
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.metric_stats import metric_asset_ledger
from app.services.semantic.metric_stats import MetricStatsService


def _metric(
    code: str,
    *,
    updated_days_ago: int | None = None,
    pending_conflict: bool = False,
    detail: dict | None = None,
) -> MagicMock:
    m = MagicMock()
    m.metric_code = code
    m.name = f"指标 {code}"
    m.domain = "sales"
    m.type = "atomic"
    m.status = "PUBLISHED"
    m.updated_at = (
        None if updated_days_ago is None else datetime.now(UTC) - timedelta(days=updated_days_ago)
    )
    m.pending_conflict = pending_conflict
    m.pending_conflict_detail = detail
    return m


def _db_with_metrics(metrics: list) -> MagicMock:
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = metrics
    db.execute = AsyncMock(return_value=result)
    return db


async def test_ledger_classifies_zombie_vs_active() -> None:
    """僵尸=长期无更新且零引用；有引用或近期更新 → 活跃。"""
    metrics = [
        _metric("recent_no_ref", updated_days_ago=2),
        _metric("stale_with_ref", updated_days_ago=200),
        _metric("stale_no_ref", updated_days_ago=300),
        _metric("never_updated", updated_days_ago=None),
    ]
    db = _db_with_metrics(metrics)
    with patch("app.services.semantic.metric_stats.LineageRepository") as repo_cls:
        repo = repo_cls.return_value
        repo.metric_reuse_counts = AsyncMock(
            return_value={
                "recent_no_ref": {"derived_by": 0, "consumed_by": 0},
                "stale_with_ref": {"derived_by": 1, "consumed_by": 0},
                "stale_no_ref": {"derived_by": 0, "consumed_by": 0},
                "never_updated": {"derived_by": 0, "consumed_by": 0},
            }
        )
        result = await MetricStatsService(db).asset_ledger()

    assert result["total"] == 4
    assert result["active_count"] == 2  # recent_no_ref / stale_with_ref（有引用）
    assert result["zombie_count"] == 2  # stale_no_ref / never_updated
    codes = {z["metric_code"] for z in result["zombies"]}
    assert codes == {"stale_no_ref", "never_updated"}
    zombie = next(z for z in result["zombies"] if z["metric_code"] == "stale_no_ref")
    assert zombie["reuse_count"] == 0
    assert zombie["days_since_update"] is not None
    assert zombie["last_updated_at"] is not None
    assert zombie["derived_by_count"] == 0
    assert zombie["consumed_by_count"] == 0


async def test_ledger_duplicate_signal_only_for_same_def_diff_name() -> None:
    """重复建设信号仅统计 SAME_DEF_DIFF_NAME；同名不同义等其他冲突不算。"""
    metrics = [
        _metric(
            "dup_a",
            updated_days_ago=3,
            pending_conflict=True,
            detail={
                "conflict_type": "same_def_diff_name",
                "score": 0.92,
                "existing_code": "dup_b",
                "reason": "口径实质相同但命名各异，建议合并",
            },
        ),
        _metric(
            "conflict_x",
            updated_days_ago=3,
            pending_conflict=True,
            detail={
                "conflict_type": "same_name_diff_def",
                "score": 0.5,
                "existing_code": "conflict_y",
                "reason": "同名不同义",
            },
        ),
        _metric("normal", updated_days_ago=1),
    ]
    db = _db_with_metrics(metrics)
    with patch("app.services.semantic.metric_stats.LineageRepository") as repo_cls:
        repo = repo_cls.return_value
        repo.metric_reuse_counts = AsyncMock(return_value={})
        result = await MetricStatsService(db).asset_ledger()

    assert result["duplicate_count"] == 1
    dup = result["duplicates"][0]
    assert dup["metric_code"] == "dup_a"
    assert dup["existing_code"] == "dup_b"
    assert dup["conflict_score"] == 0.92
    assert dup["reason"].startswith("口径实质相同")


async def test_ledger_degrades_on_empty_data() -> None:
    """无任何指标时返回 0 计数而不是 500。"""
    db = _db_with_metrics([])
    with patch("app.services.semantic.metric_stats.LineageRepository") as repo_cls:
        repo = repo_cls.return_value
        repo.metric_reuse_counts = AsyncMock(return_value={})
        result = await MetricStatsService(db).asset_ledger()
    assert result == {
        "total": 0,
        "active_count": 0,
        "zombie_count": 0,
        "duplicate_count": 0,
        "zombies": [],
        "duplicates": [],
    }


async def test_ledger_api_envelope_and_validation() -> None:
    """端点返回统一信封，账本字段完整且经 Pydantic 校验。"""
    db = MagicMock()
    user = MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    with patch("app.api.metric_stats.MetricStatsService") as svc_cls:
        svc = svc_cls.return_value
        svc.asset_ledger = AsyncMock(
            return_value={
                "total": 1,
                "active_count": 0,
                "zombie_count": 1,
                "duplicate_count": 0,
                "zombies": [
                    {
                        "metric_code": "zombie_1",
                        "name": "僵尸",
                        "domain": "sales",
                        "type": "atomic",
                        "status": "DRAFT",
                        "last_updated_at": "2026-01-01T00:00:00+00:00",
                        "days_since_update": 200,
                        "derived_by_count": 0,
                        "consumed_by_count": 0,
                        "reuse_count": 0,
                    }
                ],
                "duplicates": [],
            }
        )
        resp = await metric_asset_ledger(db=db, user=user, trace_id="t2")

    assert resp.code == "OK"
    assert resp.trace_id == "t2"
    assert resp.data is not None
    assert resp.data.zombie_count == 1
    assert resp.data.zombies[0].days_since_update == 200
    assert resp.data.zombies[0].reuse_count == 0
