"""指标注册自动推断 API 契约测试（FR-010/FR-011 auto-suggest）。

覆盖（对齐 P0-2 LLM 额度防护整改）：
- 非字符串 ``sql``（数字/对象 payload）→ 422：显式请求 schema 防解析器 AttributeError→500
- 合法请求 → 200：域 + 源表/度量列 推断并返回 13 字段（LLM 不可用走规则路径）
- 端点角色收紧：写角色（platform_admin）可访问
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


@pytest.fixture
async def metrics_client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户依赖（platform_admin，写端点放行）。"""

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


def _mock_auto_fill_result() -> dict:
    """与 auto_fill 返回结构一致的推断结果（13 字段 + 段 + 口径定义）。"""
    return {
        "metric_code_suggestion": "sales_gmv_d",
        "segments": {
            "domain": "sales",
            "biz_object": "gmv",
            "measure": "gmv",
            "period": "day",
        },
        "fields": {
            "name": {
                "value": "销售金额",
                "source": "rule",
                "confidence": 0.6,
                "reason": "规则推断",
            },
            "type": {"value": "metric", "source": "rule", "confidence": 1.0},
            "granularity": {"value": "day", "source": "rule", "confidence": 0.8},
            "unit": {"value": "元", "source": "rule", "confidence": 0.7},
            "aggregation": {"value": "SUM", "source": "rule", "confidence": 0.9},
            "time_semantics": {"value": "occurrence", "source": "rule", "confidence": 0.6},
            "freshness": {"value": "daily", "source": "rule", "confidence": 0.5},
            "dw_layer": {"value": "dwd", "source": "rule", "confidence": 0.8},
            "additivity": {"value": "additive", "source": "rule", "confidence": 0.7},
            "serving_mode": {"value": "api", "source": "rule", "confidence": 0.6},
            "metric_tier": {"value": "core", "source": "rule", "confidence": 0.5},
            "definition_json": {
                "value": {"source_table": "dwd.sales_detail"},
                "source": "rule",
                "confidence": 0.6,
            },
            "definition_mode": {"value": "auto", "source": "rule", "confidence": 0.6},
        },
        "definition_json": {
            "source_table": "dwd.sales_detail",
            "measures": [{"name": "gmv", "aggregation": "SUM"}],
        },
        "definition_mode": "auto",
    }


async def test_auto_suggest_non_string_sql_rejected_422(
    metrics_client: httpx.AsyncClient,
) -> None:
    """P0-2: 非字符串 sql（数字 payload）→ 422 而非 500。

    此前端点收裸 dict，sql=123 进入 parse_sql_profile 抛 AttributeError → 500；
    显式 schema 后由 FastAPI 请求校验返回 422。
    """
    resp = await metrics_client.post(
        "/api/v1/metric-definitions/auto-suggest",
        json={"domain_code": "sales", "sql": 123},
    )
    assert resp.status_code == 422


async def test_auto_suggest_valid_request_returns_fields(
    metrics_client: httpx.AsyncClient,
) -> None:
    """合法请求（域+源表+度量列）→ 200，返回 13 字段推断（LLM 不可用走规则）。"""
    with (
        patch(
            "app.services.llm.config_service.LlmConfigService.build_client",
            new=AsyncMock(return_value=SimpleNamespace(enabled=False)),
        ),
        patch(
            "app.services.semantic.auto_fill.auto_fill",
            return_value=_mock_auto_fill_result(),
        ),
        patch(
            "app.services.lineage.repository.LineageRepository",
            return_value=MagicMock(edges_for_node=AsyncMock(return_value=[])),
        ),
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/auto-suggest",
            json={
                "domain_code": "sales",
                "source_table": "dwd.sales_detail",
                "measure_column": "gmv",
                "period": "day",
            },
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["fields"]["name"]["value"] == "销售金额"
    assert data["definition_json"]["source_table"] == "dwd.sales_detail"


async def test_auto_suggest_splits_lineage_direction(
    metrics_client: httpx.AsyncClient,
) -> None:
    """血缘推断按方向拆分：上游邻居 → source_tables，下游邻居 → downstream_tables。

    修复混向 bug：此前 direction="both" 一把抓，源表的下游消费表被误塞进
    source_tables（指标的上游依赖）。此处验证 metric 边被过滤、方向正确归位。
    """
    with (
        patch(
            "app.services.llm.config_service.LlmConfigService.build_client",
            new=AsyncMock(return_value=SimpleNamespace(enabled=False)),
        ),
        patch(
            "app.services.semantic.auto_fill.auto_fill",
            return_value=_mock_auto_fill_result(),
        ),
        patch(
            "app.services.lineage.repository.LineageRepository",
            return_value=MagicMock(
                edges_for_node=AsyncMock(
                    side_effect=lambda node, direction: (
                        [
                            SimpleNamespace(
                                source_node="table:ods.sales_order",
                                target_node="table:dwd.sales_detail",
                            ),
                            # 指标依赖边（入边 source=metric）应被过滤
                            SimpleNamespace(
                                source_node="metric:dep_gmv",
                                target_node="table:dwd.sales_detail",
                            ),
                        ]
                        if direction == "upstream"
                        else [
                            SimpleNamespace(
                                source_node="table:dwd.sales_detail",
                                target_node="table:ads.gmv_report",
                            ),
                            # 指标消费边（出边 target=metric）应被过滤
                            SimpleNamespace(
                                source_node="table:dwd.sales_detail",
                                target_node="metric:consumer_x",
                            ),
                        ]
                    )
                )
            ),
        ),
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/auto-suggest",
            json={
                "domain_code": "sales",
                "source_table": "dwd.sales_detail",
                "measure_column": "gmv",
                "period": "day",
            },
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    # 上游依赖表：仅入边 table 邻居；下游使用表：仅出边 table 邻居；metric 边过滤
    assert data["source_tables"] == ["ods.sales_order"]
    assert data["downstream_tables"] == ["ads.gmv_report"]
    # 兼容字段 related_tables = 上游 + 下游并集（旧前端无方向时兜底）
    assert data["related_tables"] == ["ods.sales_order", "ads.gmv_report"]
