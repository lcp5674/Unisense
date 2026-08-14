"""推荐服务单元测试（TD §12.12 / FR-19）。

协同过滤画像与血缘兜底种子均来自 tracking_events（经 session 查询），
测试用 dual-result mock 同时满足 ``.scalars().all()``（用户画像）与
``.all()``（全量画像）两种取数方式。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.lineage import LineageEdge
from app.models.term import Term
from app.services.glossary.schemas import TermResponse
from app.services.recommend.service import RecommendService


def _edge(source: str, target: str, etype: str = "LINEAGE") -> LineageEdge:
    e = LineageEdge(source_node=source, target_node=target, edge_type=etype)
    e.id = 1
    return e


def _dual_result(scalar_rows: list, all_rows: list) -> MagicMock:
    """同一 execute 结果同时支持 .scalars().all() 与 .all() 两种消费方式。"""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = scalar_rows
    res = MagicMock()
    res.scalars.return_value = scalars_mock
    res.all.return_value = all_rows
    return res


def _edge_repo() -> MagicMock:
    repo = MagicMock()
    repo.related_edges = AsyncMock(
        side_effect=lambda node, limit: [_edge(node, "m2")] if node == "m1" else [_edge("m1", node)]
    )
    repo.published_terms = AsyncMock(
        return_value=[
            Term(
                id=1,
                term_code="t1",
                name="n",
                definition="d",
                domain="x",
                status="PUBLISHED",
                owner_id=1,
            )
        ]
    )
    return repo


async def _svc() -> tuple[RecommendService, MagicMock]:
    db = MagicMock()
    svc = RecommendService(db)
    repo = _edge_repo()
    svc._repo = repo  # noqa: SLF001
    # 当前用户画像 = {m1}，全量画像为空 → 协同过滤无结果 → 血缘兜底以 m1 为种子
    svc._session = MagicMock()
    svc._session.execute = AsyncMock(return_value=_dual_result(["m1"], []))
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
    # ORM → TermResponse 转换，保证 API 可序列化（不再 500）
    assert isinstance(items[0], TermResponse)
    assert items[0].term_code == "t1"


async def test_recommend_terms_api_serializable(client) -> None:
    """回归：GET /recommend/terms 返回 TermResponse 而非原始 ORM，不再 500。"""
    term = Term(
        id=1,
        term_code="t1",
        name="n",
        definition="d",
        domain="x",
        synonyms=[],
        status="PUBLISHED",
        owner_id=1,
    )
    with patch("app.api.recommend.RecommendService") as mock_svc:
        instance = mock_svc.return_value
        instance.recommend_terms = AsyncMock(return_value=[TermResponse.from_model(term)])

        resp = await client.get("/api/v1/recommend/terms")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "OK"
    assert body["data"]["items"][0]["term_code"] == "t1"
    assert body["data"]["total"] == 1


def _svc_with_profiles(
    my_actions: list[str], profile_rows: list[SimpleNamespace]
) -> tuple[RecommendService, MagicMock]:
    """构造 session 查询：.scalars() → 用户画像，.all() → 全量画像。"""
    db = MagicMock()
    svc = RecommendService(db)
    repo = MagicMock()
    repo.related_edges = AsyncMock(return_value=[])
    repo.published_terms = AsyncMock(return_value=[])
    svc._repo = repo
    svc._session = MagicMock()
    svc._session.execute = AsyncMock(return_value=_dual_result(my_actions, profile_rows))
    return svc, repo


def _seed_session(svc: RecommendService, seeds: list[str]) -> None:
    """血缘兜底测试：session 仅返回用户画像种子，全量画像为空。"""
    svc._session = MagicMock()
    svc._session.execute = AsyncMock(return_value=_dual_result(seeds, []))


def _fallback_repo(edge_side_effect) -> MagicMock:
    repo = MagicMock()
    repo.related_edges = AsyncMock(side_effect=edge_side_effect)
    repo.published_terms = AsyncMock(return_value=[])
    repo.popular_metrics = AsyncMock(return_value=[])
    repo.recent_published_metrics = AsyncMock(return_value=[])
    return repo


async def test_related_metrics_reverse_direction() -> None:
    """血缘边反向（metric_id 为目标节点）时取 source_node。"""
    svc, repo = await _svc()
    items = await svc.related_metrics("m2", 10)
    assert len(items) == 1
    assert items[0]["metric_id"] == "m1"


async def test_recommend_metrics_uses_collaborative_filtering() -> None:
    """协同过滤命中：相似用户偏好但当前用户未交互的指标被推荐。"""
    rows = [
        SimpleNamespace(actor_id="1", target_id="m5"),  # 当前用户自身 → 相似度计算排除
        SimpleNamespace(actor_id="2", target_id="m1"),
        SimpleNamespace(actor_id="2", target_id="m2"),
        SimpleNamespace(actor_id="2", target_id="m3"),
        SimpleNamespace(actor_id="3", target_id="m9"),
    ]
    svc, _ = _svc_with_profiles(["m1", "m2"], rows)
    items = await svc.recommend_metrics(1, 10)
    assert len(items) == 1
    assert items[0]["metric_id"] == "m3"
    assert items[0]["via"] == "collaborative_filtering"
    assert items[0]["edge_type"] == "CF_RECOMMEND"
    assert items[0]["score"] == round(2 / 3, 4)


async def test_recommend_metrics_few_profiles_falls_back() -> None:
    """协同过滤画像不足（< 2 用户）时降级为血缘兜底。"""
    rows = [SimpleNamespace(actor_id="2", target_id="m1")]
    svc, repo = _svc_with_profiles(["m1"], rows)
    repo.related_edges = AsyncMock(
        side_effect=lambda node, limit: [_edge("m1", "m2")] if node == "m1" else [_edge("m1", node)]
    )
    items = await svc.recommend_metrics(7, 10)
    assert items
    assert items[0]["via"] == "m1"


async def test_recommend_metrics_no_similar_user_falls_back() -> None:
    """协同过滤无相似用户（overlap < 2）时降级为血缘兜底。"""
    rows = [
        SimpleNamespace(actor_id="2", target_id="m9"),
        SimpleNamespace(actor_id="3", target_id="m10"),
    ]
    svc, repo = _svc_with_profiles(["m1"], rows)
    repo.related_edges = AsyncMock(
        side_effect=lambda node, limit: [_edge("m1", "m2")] if node == "m1" else [_edge("m1", node)]
    )
    items = await svc.recommend_metrics(7, 10)
    assert items
    assert items[0]["via"] == "m1"


def test_find_similar_users_ranking_and_filter() -> None:
    """相似用户按 Jaccard 降序，overlap < 2 的用户被过滤。"""
    my_metrics = {"a", "b"}
    profiles = {
        "2": {"a", "b", "d"},  # overlap 2 → jaccard 2/3 ≈ 0.667
        "3": {"a", "x"},  # overlap 1 → 过滤
        "4": {"a", "b", "c", "d", "e", "f"},  # overlap 2 → jaccard 2/6 ≈ 0.333
    }
    result = RecommendService._find_similar_users(my_metrics, profiles, exclude_user="9")  # noqa: SLF001
    assert [uid for uid, _ in result] == ["2", "4"]
    assert result[0][1] == 2 / 3
    assert abs(result[1][1] - 2 / 6) < 1e-9


async def test_lineage_fallback_skips_seed_metrics() -> None:
    """血缘扩展应跳过已在种子集合中的指标。"""
    svc = RecommendService(MagicMock())
    svc._repo = _fallback_repo(
        lambda node, limit: [_edge(node, "m2")] if node == "m1" else [_edge("m1", node)]
    )
    _seed_session(svc, ["m1", "m2"])
    items = await svc.recommend_metrics(7, 10)
    assert items == []


async def test_lineage_fallback_dedupes_seen() -> None:
    """血缘扩展应去重已推荐过的指标。"""
    svc = RecommendService(MagicMock())
    svc._repo = _fallback_repo(
        lambda node, limit: [_edge("m1", "m2"), _edge("m1", "m2")],
    )
    _seed_session(svc, ["m1"])
    items = await svc.recommend_metrics(7, 10)
    assert len(items) == 1
    assert items[0]["metric_id"] == "m2"


async def test_lineage_fallback_hits_limit() -> None:
    """达到 limit 后提前返回。"""
    svc = RecommendService(MagicMock())
    svc._repo = _fallback_repo(
        lambda node, limit: [_edge("m1", "m2")],
    )
    _seed_session(svc, ["m1"])
    items = await svc.recommend_metrics(7, 1)
    assert items == [{"metric_id": "m2", "via": "m1", "edge_type": "LINEAGE"}]


async def test_recommend_metrics_global_popular_fallback() -> None:
    """协同过滤与血缘均无可推时，回退到全站热门指标。"""
    svc = RecommendService(MagicMock())
    svc._repo = _fallback_repo(lambda node, limit: [])
    svc._session = MagicMock()
    svc._session.execute = AsyncMock(return_value=_dual_result([], []))
    svc._repo.popular_metrics = AsyncMock(return_value=[("m_hot_1", 12), ("m_hot_2", 5)])
    items = await svc.recommend_metrics(7, 6)
    assert items
    assert items[0]["metric_id"] == "m_hot_1"
    assert items[0]["via"] == "global_hot"
    assert items[0]["reason"] == "全站热门指标"


async def test_recommend_metrics_global_popular_excludes_seeds() -> None:
    """全局热门应排除用户已交互过的指标，避免重复推荐。"""
    svc = RecommendService(MagicMock())
    svc._repo = _fallback_repo(lambda node, limit: [])
    svc._session = MagicMock()
    svc._session.execute = AsyncMock(return_value=_dual_result(["m_hot_1"], []))
    svc._repo.popular_metrics = AsyncMock(return_value=[("m_hot_1", 12), ("m_hot_2", 5)])
    items = await svc.recommend_metrics(7, 6)
    assert [i["metric_id"] for i in items] == ["m_hot_2"]


async def test_recommend_metrics_latest_published_fallback() -> None:
    """全站无任何行为信号时，回退到最新发布指标，保证面板非空。"""
    svc = RecommendService(MagicMock())
    svc._repo = _fallback_repo(lambda node, limit: [])
    svc._session = MagicMock()
    svc._session.execute = AsyncMock(return_value=_dual_result([], []))
    svc._repo.popular_metrics = AsyncMock(return_value=[])
    svc._repo.recent_published_metrics = AsyncMock(return_value=["m_new_1", "m_new_2"])
    items = await svc.recommend_metrics(7, 6)
    assert items
    assert items[0]["metric_id"] == "m_new_1"
    assert items[0]["via"] == "latest_published"
    assert items[0]["reason"] == "最新发布指标"
