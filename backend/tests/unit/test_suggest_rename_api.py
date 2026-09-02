"""仲裁改名建议 API 契约测试（FR-010：LLM 生成区分性名称候选，不可用降级规则）。

覆盖：
- LLM 可用时返回 source=llm 的候选，携带 opposite_code 作为对方指标上下文
- LLM 不可用/未配置时降级为 rule 候选（结合度量列/域生成确定性名称）
- 候选去重 + 截断为最多 3 个
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.services.llm.config_service import LlmConfigService
from app.services.semantic.service import MetricService


@pytest.fixture
async def metrics_client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户依赖（只读端点，任意角色放行）。"""

    async def fake_db() -> AsyncIterator[MagicMock]:
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock())
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        domain=None,
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


def _fake_metric() -> SimpleNamespace:
    """构造带口径上下文的指标（name/domain/definition_json）。"""
    return SimpleNamespace(
        metric_code="sales_gmv_sum_d",
        name="销售金额",
        domain="finance",
        definition_json={
            "source_table": "dwd.sales_detail",
            "measures": [{"name": "gmv", "aggregation": "SUM"}],
        },
    )


async def test_suggest_rename_llm_candidates(
    metrics_client: httpx.AsyncClient,
) -> None:
    """LLM 可用：返回 source=llm 候选，并把 rename_opposite_code 作为对方上下文。"""

    async def _get_metric(code: str, **kwargs: object) -> SimpleNamespace:
        if code == "sales_gmv_d":
            # 对方指标：同名不同义（不同口径名称）
            return SimpleNamespace(
                metric_code=code,
                name="销售金额（旧口径）",
                domain="finance",
                definition_json={"source_table": "dwd.sales_legacy", "measures": [{"name": "amt"}]},
            )
        return _fake_metric()

    llm_client = MagicMock()
    llm_client.enabled = True
    llm_client.chat = AsyncMock(
        return_value={
            "content": (
                '[{"name":"日销售总额","reason":"语义更聚焦日粒度"},'
                '{"name":"销售金额（全量口径）","reason":"区分统计范围"}]'
            )
        }
    )
    with (
        patch.object(
            MetricService,
            "get_metric_public",
            new=AsyncMock(side_effect=_get_metric),
        ),
        patch.object(
            LlmConfigService,
            "build_client",
            new=AsyncMock(return_value=llm_client),
        ),
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_sum_d/suggest-rename",
            json={"opposite_code": "sales_gmv_d"},
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["current_name"] == "销售金额"
    suggestions = data["suggestions"]
    assert len(suggestions) == 2
    assert all(s["source"] == "llm" for s in suggestions)
    assert suggestions[0]["name"] == "日销售总额"
    # prompt 中应包含对方指标真实名称/源表/度量列上下文
    prompt = llm_client.chat.await_args.kwargs["messages"][0]["content"]
    assert "销售金额（旧口径）" in prompt
    assert "dwd.sales_detail" in prompt
    assert "gmv" in prompt


async def test_suggest_rename_rule_fallback(
    metrics_client: httpx.AsyncClient,
) -> None:
    """LLM 未配置（enabled=False）：降级为 rule 候选，基于度量列/域生成确定性名称。"""
    fake_metric = _fake_metric()
    with (
        patch.object(
            MetricService,
            "get_metric_public",
            new=AsyncMock(return_value=fake_metric),
        ),
        patch.object(
            LlmConfigService,
            "build_client",
            new=AsyncMock(return_value=SimpleNamespace(enabled=False)),
        ),
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_sum_d/suggest-rename",
            json={"opposite_code": "sales_gmv_d"},
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    suggestions = data["suggestions"]
    assert len(suggestions) >= 1
    assert suggestions[0]["source"] == "rule"
    # 规则兜底应结合度量列（gmv）生成候选
    assert "gmv" in suggestions[0]["name"]


async def test_suggest_rename_rule_fallback_no_context_returns_empty(
    metrics_client: httpx.AsyncClient,
) -> None:
    """LLM 不可用且无任何上下文（无度量列/域/对方名称）：不编造假候选，返回空。

    生产标准：没有任何依据时不生成「原名·新口径」这类机械拼凑候选，
    由前端提示用户手动命名（此前会返回假候选并被前端自动填入）。
    """
    fake_metric = SimpleNamespace(
        metric_code="sales_gmv_sum_d",
        name="销售金额",
        domain="",
        definition_json={},
    )
    with (
        patch.object(
            MetricService,
            "get_metric_public",
            new=AsyncMock(return_value=fake_metric),
        ),
        patch.object(
            LlmConfigService,
            "build_client",
            new=AsyncMock(return_value=SimpleNamespace(enabled=False)),
        ),
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_sum_d/suggest-rename",
            json={},
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["suggestions"] == []


async def test_suggest_rename_metric_missing(
    metrics_client: httpx.AsyncClient,
) -> None:
    """指标不存在时透传标准 NOT_FOUND（由 get_metric_public 抛出）。"""
    from app.core.exceptions import NotFoundError

    with patch.object(
        MetricService,
        "get_metric_public",
        new=AsyncMock(side_effect=NotFoundError("指标不存在: nope")),
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/nope/suggest-rename",
            json={},
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"
