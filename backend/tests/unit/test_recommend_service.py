"""推荐服务单元测试（TD §12.12 / FR-19）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.models.lineage import LineageEdge
from app.models.notify import EventLog
from app.models.term import Term
from app.services.recommend.service import RecommendService


def _edge(source: str, target: str, etype: str = "LINEAGE") -> LineageEdge:
    e = LineageEdge(source_node=source, target_node=target, edge_type=etype)
    e.id = 1
    return e


async def _svc() -> tuple[RecommendService, MagicMock]:
    db = MagicMock()
    svc = RecommendService(db)
    repo = MagicMock()
    repo.related_edges = AsyncMock(
        side_effect=lambda node, limit: [_edge(node, "m2")] if node == "m1" else [_edge("m1", node)]
    )
    repo.recent_user_events = AsyncMock(
        return_value=[EventLog(event_type="metric.access", source="7", payload={"metric_id": "m1"})]
    )
    repo.published_terms = AsyncMock(
        return_value=[
            Term(
                term_code="t1",
                name="n",
                definition="d",
                domain="x",
                status="PUBLISHED",
                owner_id=1,
            )
        ]
    )
    svc._repo = repo  # noqa: SLF001
    # 协同过滤依赖 _session 查询 tracking_events：mock 为空 → 走血缘兜底
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    svc._session = MagicMock()
    svc._session.execute = AsyncMock(return_value=mock_result)
    return svc, repo


async def test_related_metrics() -> None:
    svc, repo = await _svc()
    items = await svc.related_metrics("m1", 10)
    assert len(items) == 1
    assert items[0]["metric_id"] == "m2"
    repo.related_edges.assert_awaited()


async def test_recommend_metrics_from_events() -> None:
    svc, repo = await _svc()
    items = await svc.recommend_metrics(7, 10)
    # 用户关注 m1，血缘扩展出 m2
    assert any(i["metric_id"] == "m2" for i in items)


async def test_recommend_terms() -> None:
    svc, repo = await _svc()
    items = await svc.recommend_terms(10)
    assert len(items) == 1
