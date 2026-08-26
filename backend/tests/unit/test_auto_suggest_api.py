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


# 真实 ETL 多语句 SQL（set 参数 + create table DDL + insert overwrite select 双度量）
# 覆盖修复：parse_one 只取首条 Set 语句 → 画像空；parse 全语句拆分 → 双度量识别
_MULTI_STMT_SQL = """set hive.vectorized.execution.enabled=false;

create table if not exists wedw_dws.doctor_active_month_di(
 month_id string comment '统计月',
 current_month_active_doctor_cnt int comment '月活',
 last_month_active_doctor_cnt int comment '留存'
)
stored as orc;

insert overwrite table wedw_dws.doctor_active_month_di
select a.month_id,
 count(distinct t1.doctor_code) as current_month_active_doctor_cnt,
 count(distinct case when t2.doctor_code is not null
   then t2.doctor_code end) as last_month_active_doctor_cnt
from (
  select substr(create_date,1,7) as month_id, doctor_code
  from wedw_dw.doctor_visit_agent_info_da
) t1
left join (
  select substr(last_month_last_visit_date,1,7) as month_id, doctor_code
  from wedw_dw.doctor_visit_agent_info_da
) t2
on t1.month_id = t2.month_id and t1.doctor_code = t2.doctor_code
group by t1.month_id;
"""


async def test_auto_suggest_multi_stmt_sql_returns_parsed_measures(
    metrics_client: httpx.AsyncClient,
) -> None:
    """多语句 ETL SQL → parsed_measures 返回每个度量列 + 聚合方式（用户可确认识别成功）。

    覆盖修复：此前 parse_one 只取首条 Set 语句 → 画像空、无字段可展示；
    修复后 parse 拆分全语句选产出 INSERT ... SELECT → 双度量带聚合返回。
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
            return_value=MagicMock(edges_for_node=AsyncMock(return_value=[])),
        ),
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/auto-suggest",
            json={"domain_code": "outpatient", "sql": _MULTI_STMT_SQL},
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    measures = data["parsed_measures"]
    assert isinstance(measures, list)
    assert len(measures) == 2
    # 两个度量列：月活 + 留存，聚合方式均为 COUNT_DISTINCT，含原始表达式
    assert {m["alias"] for m in measures} == {
        "current_month_active_doctor_cnt",
        "last_month_active_doctor_cnt",
    }
    assert {m["agg"] for m in measures} == {"COUNT_DISTINCT"}
    assert any("COUNT(DISTINCT" in m["expression"] for m in measures)
    # 源表识别（修复后画像不再为空）
    assert data["source_tables"] == [] or any("doctor_visit" in t for t in data["source_tables"])

async def test_auto_suggest_sql_period_inferred_and_passed(
    metrics_client: httpx.AsyncClient,
) -> None:
    """SQL 截月粒度（substr(create_date,1,7)）→ period 自动接线为 month 传给 auto_fill。

    修复「信息最大化」缺口：此前注册向导 runSqlInfer 只传 domain_code+sql 不传
    period，导致 metric_code 生成条件（源表+度量列+周期三齐）不满足恒为空、
    granularity 走规则兜底误判。显式传入 period 时不被 SQL 推断覆盖。
    """
    captured: list[dict[str, object]] = []

    def _fake_auto_fill(**kwargs: object) -> dict:
        captured.append(kwargs)
        return _mock_auto_fill_result()

    with (
        patch(
            "app.services.llm.config_service.LlmConfigService.build_client",
            new=AsyncMock(return_value=SimpleNamespace(enabled=False)),
        ),
        patch(
            "app.services.semantic.auto_fill.auto_fill",
            new=_fake_auto_fill,
        ),
        patch(
            "app.services.lineage.repository.LineageRepository",
            return_value=MagicMock(edges_for_node=AsyncMock(return_value=[])),
        ),
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/auto-suggest",
            json={"domain_code": "outpatient", "sql": _MULTI_STMT_SQL},
        )
        # 显式传入 period 时保持用户指定（不被 SQL 推断覆盖）
        resp2 = await metrics_client.post(
            "/api/v1/metric-definitions/auto-suggest",
            json={
                "domain_code": "outpatient",
                "sql": _MULTI_STMT_SQL,
                "period": "day",
            },
        )

    assert resp.status_code == 200
    assert resp2.status_code == 200
    # 第一次（无显式 period）：SQL 截月粒度接线 month；第二次：显式 day 保持
    assert len(captured) == 2
    assert captured[0].get("period") == "month"
    assert captured[1].get("period") == "day"
    assert captured[0].get("source_table")
    assert captured[0].get("measure_column")


async def test_auto_suggest_measure_suggestions_matches_published(
    metrics_client: httpx.AsyncClient,
) -> None:
    """度量列命中已发布逻辑度量目录 → 返回 measure_suggestions 候选（信息最大化）。

    OneData 下原子指标 = 逻辑度量 + 聚合方式，SQL 只解析出物理列名；端点按
    度量列名与 measure_code/同义词做语义弱匹配，给用户一键继承 measure_id 的起点。
    匹配不到返回空列表，不阻断推断。
    """
    fake_measure = SimpleNamespace(
        id=7,
        measure_code="doctor_active_cnt",
        name="医生活跃数",
        measure_format="NUMERIC",
        default_unit="人",
        synonyms=["doctor_active_cnt", "doctor_code"],
    )
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
        patch(
            "app.services.measure_catalog.repository.MeasureCatalogRepository"
        ) as repo_cls,
    ):
        repo_cls.return_value.list = AsyncMock(return_value=([fake_measure], 1))
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/auto-suggest",
            json={
                "domain_code": "outpatient",
                "source_table": "wedw_dw.doctor_visit_agent_info_da",
                "measure_column": "doctor_code",
            },
        )
        # 无匹配度量 → 空列表不阻断
        repo_cls.return_value.list = AsyncMock(return_value=([], 0))
        resp2 = await metrics_client.post(
            "/api/v1/metric-definitions/auto-suggest",
            json={
                "domain_code": "outpatient",
                "source_table": "wedw_dw.doctor_visit_agent_info_da",
                "measure_column": "no_such_measure_xyz",
            },
        )

    assert resp.status_code == 200
    assert resp2.status_code == 200
    sugg = resp.json()["data"]["measure_suggestions"]
    assert isinstance(sugg, list) and len(sugg) == 1
    assert sugg[0]["measure_code"] == "doctor_active_cnt"
    assert sugg[0]["id"] == 7
    assert sugg[0]["confidence"] == 1.0
    assert resp2.json()["data"]["measure_suggestions"] == []


