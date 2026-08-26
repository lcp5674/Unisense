"""SQL 批量解析/注册 API 契约测试（FR-010 批量注册增强，场景A/B）。

覆盖（ASGI + 模拟 service 层，快速契约校验）：
- POST /parse-sql-batch：合法 200 返回候选清单；sql 缺失/非字符串 422；审计 action
- POST /batch-register-from-sql：合法 200 返回 batch_id；candidates 空/缺字段 422；审计 action
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app

_ATOMIC_CANDIDATE = {
    "key": "0:amount",
    "metric_code": "sales_order_amount_day",
    "name": "日订单金额",
    "type": "atomic",
    "source_table": "dwd_order_di",
    "measure_column": "amount",
    "aggregation": "SUM",
    "period": "day",
    "unit": "CNY",
    "definition_json": {
        "expression": "SUM(amount)",
        "source_fields": [{"table": "dwd_order_di", "column": "amount"}],
    },
}


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


# ---------------------------------------------------------------- parse-sql-batch


async def test_parse_sql_batch_returns_candidates(
    metrics_client: httpx.AsyncClient,
) -> None:
    """合法请求 → 200，返回 statements/candidates/domain。"""
    result = {
        "statements": [
            {
                "index": 0,
                "sql": "SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt",
                "source_tables": ["dwd_order_di"],
                "measure_count": 1,
                "group_by": ["dt"],
            }
        ],
        "candidates": [dict(_ATOMIC_CANDIDATE)],
        "skipped": [],
        "domain": {"code": "sales", "status": "user", "confidence": None},
    }
    with (
        patch(
            "app.services.semantic.sql_split.infer_sql_batch",
            new=AsyncMock(return_value=result),
        ) as m,
        patch("app.api.metrics.write_audit", new=AsyncMock()) as audit,
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/parse-sql-batch",
            json={
                "sql": "SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt",
                "split_mode": "statement",
                "synthesize_composite": True,
            },
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["metric_code"] == "sales_order_amount_day"
    assert data["domain"]["status"] == "user"
    m.assert_awaited_once()
    kwargs = m.await_args.kwargs
    assert kwargs["split_mode"] == "statement"
    assert kwargs["synthesize_composite"] is True
    # 审计 action
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "metric_definition.sql_batch_parse"


async def test_parse_sql_batch_passes_use_llm(metrics_client: httpx.AsyncClient) -> None:
    """use_llm 显式模式透传：请求带 use_llm=true → infer_sql_batch 收到 True。"""
    result = {
        "statements": [],
        "candidates": [],
        "skipped": [],
        "domain": {"code": None, "status": "none", "confidence": None},
    }
    with (
        patch(
            "app.services.semantic.sql_split.infer_sql_batch",
            new=AsyncMock(return_value=result),
        ) as m,
        patch("app.api.metrics.write_audit", new=AsyncMock()),
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/parse-sql-batch",
            json={
                "sql": "SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt",
                "split_mode": "statement",
                "use_llm": True,
            },
        )
    assert resp.status_code == 200
    kwargs = m.await_args.kwargs
    assert kwargs["use_llm"] is True
    # 缺省（不带 use_llm）→ False
    with (
        patch(
            "app.services.semantic.sql_split.infer_sql_batch",
            new=AsyncMock(return_value=result),
        ) as m2,
        patch("app.api.metrics.write_audit", new=AsyncMock()),
    ):
        await metrics_client.post(
            "/api/v1/metric-definitions/parse-sql-batch",
            json={"sql": "SELECT SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt"},
        )
    assert m2.await_args.kwargs["use_llm"] is False


async def test_parse_sql_batch_missing_sql_422(metrics_client: httpx.AsyncClient) -> None:
    """缺 sql → 422（必填）。"""
    resp = await metrics_client.post(
        "/api/v1/metric-definitions/parse-sql-batch",
        json={"split_mode": "statement"},
    )
    assert resp.status_code == 422


async def test_parse_sql_batch_non_string_sql_422(metrics_client: httpx.AsyncClient) -> None:
    """非字符串 sql（数字 payload）→ 422 而非 500。"""
    resp = await metrics_client.post(
        "/api/v1/metric-definitions/parse-sql-batch",
        json={"sql": 123},
    )
    assert resp.status_code == 422


async def test_parse_sql_batch_invalid_split_mode_422(
    metrics_client: httpx.AsyncClient,
) -> None:
    """非法 split_mode → 422（Literal 枚举收严）。"""
    resp = await metrics_client.post(
        "/api/v1/metric-definitions/parse-sql-batch",
        json={"sql": "SELECT 1", "split_mode": "regex"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------- batch-register-from-sql


async def test_batch_register_from_sql_success(
    metrics_client: httpx.AsyncClient,
) -> None:
    """合法候选清单 → 200，返回 batch_id 与逐条结果。"""
    with (
        patch("app.api.metrics.MetricService") as mock_svc,
        patch("app.api.metrics.write_audit", new=AsyncMock()) as audit,
    ):
        mock_svc.return_value.batch_register_from_sql = AsyncMock(
            return_value={
                "batch_id": "sqlbatch_abc123",
                "candidates": [
                    {
                        "metric_code": "sales_order_amount_day",
                        "status": "DRAFT",
                        "validation_errors": None,
                    }
                ],
            }
        )
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/batch-register-from-sql",
            json={"domain": "sales", "candidates": [dict(_ATOMIC_CANDIDATE)]},
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["batch_id"] == "sqlbatch_abc123"
    assert data["candidates"][0]["status"] == "DRAFT"
    mock_svc.return_value.batch_register_from_sql.assert_awaited_once()
    # 审计 action
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "metric_definition.sql_batch_register"


async def test_batch_register_from_sql_derived_candidate_200(
    metrics_client: httpx.AsyncClient,
) -> None:
    """派生候选（type=derived + 依赖指标 + 计算表达式 + 挂载实体）→ 200 透传 service。"""
    derived = {
        "key": "0:retention",
        "metric_code": "outpatient_doctor_retention_month",
        "name": "医生留存率",
        "type": "derived",
        "source_table": "wedw_dws.doctor_active_month_di",
        "measure_column": None,
        "aggregation": None,
        "period": "month",
        "unit": None,
        "definition_json": {
            "expression": (
                "outpatient_doctor_currentmonthactivedoctorcnt_month / "
                "outpatient_doctor_lastmonthactivedoctorcnt_month"
            ),
            "dependencies": [
                "outpatient_doctor_currentmonthactivedoctorcnt_month",
                "outpatient_doctor_lastmonthactivedoctorcnt_month",
            ],
        },
        "dependencies": [
            "outpatient_doctor_currentmonthactivedoctorcnt_month",
            "outpatient_doctor_lastmonthactivedoctorcnt_month",
        ],
        "mount": {
            "source_table": "wedw_dws.doctor_active_month_di",
            "source_column": "doctor_code",
            "granularity": "month",
            "default_period": "month",
            "domain": "outpatient",
        },
    }
    with (
        patch("app.api.metrics.MetricService") as mock_svc,
        patch("app.api.metrics.write_audit", new=AsyncMock()) as audit,
    ):
        mock_svc.return_value.batch_register_from_sql = AsyncMock(
            return_value={
                "batch_id": "sqlbatch_derived1",
                "candidates": [
                    {
                        "metric_code": "outpatient_doctor_retention_month",
                        "status": "DRAFT",
                        "validation_errors": None,
                    }
                ],
            }
        )
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/batch-register-from-sql",
            json={"domain": "outpatient", "candidates": [derived]},
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["candidates"][0]["status"] == "DRAFT"
    mock_svc.return_value.batch_register_from_sql.assert_awaited_once()
    req = mock_svc.return_value.batch_register_from_sql.await_args.args[0]
    assert req.candidates[0].type == "derived"
    assert req.candidates[0].mount is not None
    assert req.candidates[0].mount.source_table == "wedw_dws.doctor_active_month_di"
    audit.assert_awaited_once()


async def test_batch_register_from_sql_empty_candidates_422(
    metrics_client: httpx.AsyncClient,
) -> None:
    """candidates 为空 → 422（min_length=1）。"""
    resp = await metrics_client.post(
        "/api/v1/metric-definitions/batch-register-from-sql",
        json={"domain": "sales", "candidates": []},
    )
    assert resp.status_code == 422


async def test_batch_register_from_sql_missing_definition_422(
    metrics_client: httpx.AsyncClient,
) -> None:
    """候选缺 definition_json → 422（必填）。"""
    cand = dict(_ATOMIC_CANDIDATE)
    del cand["definition_json"]
    resp = await metrics_client.post(
        "/api/v1/metric-definitions/batch-register-from-sql",
        json={"domain": "sales", "candidates": [cand]},
    )
    assert resp.status_code == 422


async def test_batch_register_from_sql_missing_domain_422(
    metrics_client: httpx.AsyncClient,
) -> None:
    """缺 domain → 422（必填）。"""
    resp = await metrics_client.post(
        "/api/v1/metric-definitions/batch-register-from-sql",
        json={"candidates": [dict(_ATOMIC_CANDIDATE)]},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------- 注入守卫豁免


async def test_parse_sql_batch_etl_sql_with_injection_features_200(
    metrics_client: httpx.AsyncClient,
) -> None:
    """大段 ETL SQL（-- 注释 / /* */ 块注释 / UNION SELECT / 多语句）是合法输入，
    注入守卫须对 sql 字段豁免 → 200（对齐 /lineage/parse 模式）。"""
    etl_sql = (
        "DROP TABLE IF EXISTS tmp_gmv_stage;\n"
        "CREATE TABLE tmp_gmv_stage AS\n"
        "SELECT dt, region, SUM(amount) AS gmv /* +SET_VAR(enable_vectorized_engine=false) */\n"
        "FROM dwd_order_di WHERE dt >= '2026-01-01' -- 取本年\n"
        "GROUP BY dt, region;\n"
        "INSERT INTO dws_gmv_daily SELECT dt, region, gmv FROM tmp_gmv_stage\n"
        "UNION ALL SELECT dt, region, gmv FROM ods.archive_gmv"
    )
    with (
        patch(
            "app.services.semantic.sql_split.infer_sql_batch",
            new=AsyncMock(
                return_value={
                    "statements": [],
                    "candidates": [],
                    "skipped": [],
                    "domain": {"code": None, "status": "none", "confidence": None},
                }
            ),
        ) as m,
        patch("app.api.metrics.write_audit", new=AsyncMock()),
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/parse-sql-batch",
            json={"sql": etl_sql, "split_mode": "semicolon"},
        )
    assert resp.status_code == 200
    m.assert_awaited_once()


async def test_parse_sql_batch_custom_rules_with_comment_delimiters_200(
    metrics_client: httpx.AsyncClient,
) -> None:
    """custom_rules 承载用户自定义切分规则（可能含 -- / 正则），须豁免 → 200。"""
    with (
        patch(
            "app.services.semantic.sql_split.infer_sql_batch",
            new=AsyncMock(
                return_value={
                    "statements": [],
                    "candidates": [],
                    "skipped": [],
                    "domain": {"code": None, "status": "none", "confidence": None},
                }
            ),
        ) as m,
        patch("app.api.metrics.write_audit", new=AsyncMock()),
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/parse-sql-batch",
            json={
                "sql": "SELECT 1",
                "split_mode": "custom",
                "custom_rules": {
                    "delimiters": ["^--.*$"],
                    "start_markers": ["/* begin */"],
                },
            },
        )
    assert resp.status_code == 200
    m.assert_awaited_once()


async def test_batch_register_from_sql_definition_sql_exempted_200(
    metrics_client: httpx.AsyncClient,
) -> None:
    """候选 definition_json.sql 承载 SQL 口径（含注入特征）→ 路径豁免 → 200。"""
    cand = dict(_ATOMIC_CANDIDATE)
    cand["type"] = "composite"
    cand["measure_column"] = None
    cand["aggregation"] = None
    cand["definition_json"] = {
        "sql": (
            "SELECT dt, SUM(a) / SUM(b) AS ratio FROM dwd_fact_di -- 比值\n"
            "UNION ALL SELECT dt, 0 FROM ods.legacy"
        ),
        "dependencies": ["sales_order_amount_day"],
    }
    cand["dependencies"] = ["sales_order_amount_day"]
    with (
        patch("app.api.metrics.MetricService") as mock_svc,
        patch("app.api.metrics.write_audit", new=AsyncMock()),
    ):
        mock_svc.return_value.batch_register_from_sql = AsyncMock(
            return_value={"batch_id": "b", "candidates": []}
        )
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/batch-register-from-sql",
            json={"domain": "sales", "candidates": [cand]},
        )
    assert resp.status_code == 200


async def test_batch_register_from_sql_raw_sql_exempted_200(
    metrics_client: httpx.AsyncClient,
) -> None:
    """候选 raw_sql 承载候选所属语句整句原始 ETL SQL（含 -- 注释 / /* */ / ;insert /
    多语句等注入特征）→ 路径豁免 → 200（此前漏豁免实测 INJECTION_DETECTED 400）。"""
    cand = dict(_ATOMIC_CANDIDATE)
    cand["raw_sql"] = (
        "set hive.vectorized.execution.enabled=false;\n"
        "-- 取本年数据\n"
        "create table if not exists dws_tmp_gmv (dt string, gmv double) stored as orc;\n"
        "insert overwrite table dws_tmp_gmv /* 口径注释 */\n"
        "select dt, sum(amount) as gmv from dwd_order_di group by dt;\n"
        "select dt, gmv from dws_tmp_gmv"
    )
    with (
        patch("app.api.metrics.MetricService") as mock_svc,
        patch("app.api.metrics.write_audit", new=AsyncMock()),
    ):
        mock_svc.return_value.batch_register_from_sql = AsyncMock(
            return_value={"batch_id": "b", "candidates": []}
        )
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/batch-register-from-sql",
            json={"domain": "sales", "candidates": [cand]},
        )
    assert resp.status_code == 200


async def test_batch_register_from_sql_name_injection_still_blocked_400(
    metrics_client: httpx.AsyncClient,
) -> None:
    """豁免仅覆盖 definition_json 子树；候选 name 字段注入仍应被拦截 → 400。"""
    cand = dict(_ATOMIC_CANDIDATE)
    cand["name"] = "x'; DROP TABLE users--"
    resp = await metrics_client.post(
        "/api/v1/metric-definitions/batch-register-from-sql",
        json={"domain": "sales", "candidates": [cand]},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "INJECTION_DETECTED"


# ---- sql-infer-eval（评测报告 + 运行记录）


async def test_sql_infer_eval_report_200(metrics_client: httpx.AsyncClient) -> None:
    """GET 评测报告：实时计算返回 9 用例报告 + 历史列表（确定性）。"""
    with (
        patch(
            "app.services.semantic.sql_infer_eval.service.list_runs",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.semantic.sql_infer_eval.service.latest_run_cases",
            new=AsyncMock(return_value=(None, [])),
        ),
    ):
        resp = await metrics_client.get("/api/v1/metric-definitions/sql-infer-eval")
    assert resp.status_code == 200
    data = resp.json()["data"]
    report = data["report"]
    assert report["total"] >= 5
    assert report["exact_rate"] == 1.0
    assert report["measure_recall"] == 1.0
    assert report["measure_precision"] == 1.0
    assert "cases" in report and len(report["cases"]) == report["total"]
    assert data["history"] == []
    assert data["latest_run"] is None


async def test_sql_infer_eval_run_records(metrics_client: httpx.AsyncClient) -> None:
    """POST 运行评测：落一条历史记录并返回 report + run_id + 审计。"""
    with (
        patch(
            "app.services.semantic.sql_infer_eval.service.run_and_record",
            new=AsyncMock(
                return_value={"report": {"total": 9, "exact_rate": 1.0}, "run_id": 7}
            ),
        ) as m,
        patch("app.api.metrics.write_audit", new=AsyncMock()) as audit,
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sql-infer-eval/run"
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["run_id"] == 7
    m.assert_awaited_once()
    assert m.await_args.kwargs["actor_id"] == 1
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "metric_definition.sql_infer_eval_run"
