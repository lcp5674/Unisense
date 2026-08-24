"""指标复用度分析单测（P0：原子→派生→报表引用复用度清单）。

覆盖：
- LineageRepository.metric_reuse_counts：DERIVED_FROM/CONSUMED_BY 按指标聚合、
  去前缀、缺失边类型兜底 0
- MetricStatsService.reuse_summary：按复用度降序、零复用指标统计、无血缘时降级
- API 端点 metric_reuse_stats：统一信封 + Pydantic 校验
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.api.metric_stats import metric_reuse_stats
from app.services.lineage.repository import LineageRepository
from app.services.semantic.metric_stats import MetricStatsService

# ---- repository ----

async def test_repo_metric_reuse_counts_groups_by_edge_type() -> None:
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = [
        ("metric:gmv_total", "DERIVED_FROM", 2),
        ("metric:gmv_total", "CONSUMED_BY", 3),
        ("metric:arpu", "DERIVED_FROM", 1),
    ]
    db.execute = AsyncMock(return_value=result)
    out = await LineageRepository(db).metric_reuse_counts()
    assert out == {
        "gmv_total": {"derived_by": 2, "consumed_by": 3},
        "arpu": {"derived_by": 1, "consumed_by": 0},
    }


async def test_repo_metric_reuse_counts_empty() -> None:
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    assert await LineageRepository(db).metric_reuse_counts() == {}


# ---- service ----

def _db_with_metrics(rows: list[tuple]) -> MagicMock:
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute = AsyncMock(return_value=result)
    return db


async def test_reuse_summary_sorts_by_reuse_desc() -> None:
    db = _db_with_metrics(
        [
            ("unused", "未用指标", "mkt", "atomic", "DRAFT"),
            ("arpu", "ARPU", "sales", "atomic", "PUBLISHED"),
            ("gmv_total", "GMV 总览", "sales", "derived", "PUBLISHED"),
        ]
    )
    with patch(
        "app.services.semantic.metric_stats.LineageRepository"
    ) as repo_cls:
        repo = repo_cls.return_value
        repo.metric_reuse_counts = AsyncMock(
            return_value={
                "gmv_total": {"derived_by": 2, "consumed_by": 3},
                "arpu": {"derived_by": 1, "consumed_by": 0},
            }
        )
        result = await MetricStatsService(db).reuse_summary()

    assert result["total"] == 3
    assert result["referenced"] == 2
    assert result["zero_reuse"] == 1
    codes = [it["metric_code"] for it in result["items"]]
    assert codes == ["gmv_total", "arpu", "unused"]  # 复用度降序
    top = result["items"][0]
    assert top["derived_by_count"] == 2
    assert top["consumed_by_count"] == 3
    assert top["reuse_count"] == 5
    assert result["items"][-1]["reuse_count"] == 0


async def test_reuse_summary_degrades_when_lineage_unavailable() -> None:
    """血缘表无数据时全部指标零复用（降级而不是报错）。"""
    db = _db_with_metrics([("m1", "指标1", "sales", "atomic", "PUBLISHED")])
    with patch(
        "app.services.semantic.metric_stats.LineageRepository"
    ) as repo_cls:
        repo = repo_cls.return_value
        repo.metric_reuse_counts = AsyncMock(return_value={})
        result = await MetricStatsService(db).reuse_summary()
    assert result["total"] == 1
    assert result["referenced"] == 0
    assert result["zero_reuse"] == 1
    assert result["items"][0]["reuse_count"] == 0


# ---- API ----

async def test_reuse_api_envelope_and_validation() -> None:
    """端点返回统一信封，复用清单字段完整且经 Pydantic 校验。"""
    db = MagicMock()
    user = MagicMock(id=1, role="platform_admin")
    with patch("app.api.metric_stats.MetricStatsService") as svc_cls:
        svc = svc_cls.return_value
        svc.reuse_summary = AsyncMock(
            return_value={
                "total": 1,
                "referenced": 1,
                "zero_reuse": 0,
                "items": [
                    {
                        "metric_code": "gmv_total",
                        "name": "GMV",
                        "domain": "sales",
                        "type": "derived",
                        "status": "PUBLISHED",
                        "derived_by_count": 2,
                        "consumed_by_count": 1,
                        "reuse_count": 3,
                    }
                ],
            }
        )
        resp = await metric_reuse_stats(db=db, user=user, trace_id="t1")

    assert resp.code == "OK"
    assert resp.trace_id == "t1"
    data = resp.data
    assert data is not None
    assert data.total == 1
    assert data.items[0].reuse_count == 3
    assert data.items[0].consumed_by_count == 1
