"""采集 API 端到端单测（对齐 P1-4 drift-logs / P1-7 调度与立即采集分离）。

仅覆盖 http 路由层行为，DB/Redis 以依赖覆盖 + mock 注入，无外部依赖。
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
async def collector_client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户依赖（平台管理员），返回 httpx 异步客户端。"""

    async def fake_db() -> AsyncIterator[MagicMock]:
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock())
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        session.flush = AsyncMock()
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=1, role="platform_admin")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_schedule_only_persists_config_does_not_collect(
    collector_client: httpx.AsyncClient,
) -> None:
    """P1-7: /schedule 只保存调度配置，不立即入队采集（schedule_collection 不被调用）。"""
    with patch(
        "app.api.collector.CollectorService.schedule_collection", new_callable=AsyncMock
    ) as mock_schedule:
        resp = await collector_client.post(
            "/api/v1/data-sources/s1/schedule",
            json={"cron": "0 0 * * *", "mode": "FULL"},
        )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["scheduled"] is True
    assert body["cron"] == "0 0 * * *"
    assert body["mode"] == "FULL"
    # 定时调度不应触发立即采集
    mock_schedule.assert_not_called()


async def test_collect_now_triggers_immediate_collection(
    collector_client: httpx.AsyncClient,
) -> None:
    """P1-7: /collect-now 立即入队采集，返回 job_id 且 schedule_collection 被调用。"""
    with patch(
        "app.api.collector.CollectorService.schedule_collection",
        new_callable=AsyncMock,
        return_value="job-immediate-1",
    ) as mock_schedule:
        resp = await collector_client.post(
            "/api/v1/data-sources/s1/collect-now",
            json={"mode": "FULL"},
        )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["job_id"] == "job-immediate-1"
    assert body["status"] == "QUEUED"
    assert body["mode"] == "FULL"
    mock_schedule.assert_awaited_once_with("s1", 1)


async def test_drift_logs_endpoint_returns_paged(
    collector_client: httpx.AsyncClient,
) -> None:
    """P1-4: GET /{source_id}/drift-logs 返回分页 drift 记录。"""
    with patch(
        "app.api.collector.CollectorService.list_drift_logs",
        new_callable=AsyncMock,
        return_value={
            "items": [
                {
                    "source_id": "s1",
                    "entity_name": "users",
                    "change_type": "ADD_COLUMN",
                    "before_signature": None,
                    "after_signature": "sig2",
                    "before_schema": None,
                    "after_schema": {"columns": [{"name": "age", "type": "int"}]},
                    "diff_json": {"added": ["age"], "removed": [], "changed": []},
                    "detected_at": "2026-02-01T00:00:00+00:00",
                }
            ],
            "total": 1,
            "page": 1,
            "page_size": 20,
        },
    ):
        resp = await collector_client.get("/api/v1/data-sources/s1/drift-logs")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["entity_name"] == "users"
    assert body["items"][0]["change_type"] == "ADD_COLUMN"


async def test_list_jobs_returns_jobs(
    collector_client: httpx.AsyncClient,
) -> None:
    """采集任务中心：GET /jobs 返回任务列表（含状态/详情）。"""
    with patch(
        "app.api.collector.CollectorService.list_jobs",
        new_callable=AsyncMock,
        return_value=[
            {
                "job_id": "job-abc123",
                "source_id": "mysql_src_1",
                "status": "QUEUED",
                "detail": {"mode": "FULL"},
            }
        ],
    ):
        resp = await collector_client.get("/api/v1/data-sources/jobs?limit=10&offset=0")
    assert resp.status_code == 200
    jobs = resp.json()["data"]
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "job-abc123"
    assert jobs[0]["status"] == "QUEUED"


async def test_list_jobs_must_precede_source_id_route(
    collector_client: httpx.AsyncClient,
) -> None:
    """GET /jobs 必须命中列表端点而非被 /{source_id} 吞掉（静态路由先注册）。"""
    with (
        patch(
            "app.api.collector.CollectorService.list_jobs",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_list,
        patch(
            "app.api.collector.CollectorService.get_source",
            new_callable=AsyncMock,
        ) as mock_get,
    ):
        resp = await collector_client.get("/api/v1/data-sources/jobs")
    assert resp.status_code == 200
    mock_list.assert_awaited_once()
    mock_get.assert_not_awaited()


async def test_list_catalog_databases_returns_distinct(
    collector_client: httpx.AsyncClient,
) -> None:
    """GET /catalogs/databases 返回去重库名列表，并可随 source_id 过滤。"""
    with patch(
        "app.api.collector.CollectorService.list_catalog_databases",
        new_callable=AsyncMock,
        return_value=["unisense", "sales"],
    ) as mock_list:
        resp = await collector_client.get("/api/v1/catalogs/databases?source_id=mysql_unisense")
    assert resp.status_code == 200
    assert resp.json()["data"]["items"] == ["unisense", "sales"]
    mock_list.assert_awaited_once()
    # 校验 source_id 透传到 service
    call_kwargs = mock_list.await_args.args
    assert call_kwargs[0] == "mysql_unisense"


# ---- 表级业务描述 + 描述缺失统计（TD §12.1） ----


async def test_get_description_coverage_endpoint(collector_client: httpx.AsyncClient) -> None:
    """GET /catalogs/description-coverage 返回表/字段覆盖统计。"""
    fake_svc = MagicMock()
    fake_svc._repo.get_description_coverage = AsyncMock(
        return_value={
            "total_tables": 2,
            "tables_with_desc": 1,
            "tables_missing_desc": 1,
            "total_fields": 4,
            "fields_with_desc": 2,
            "fields_missing_desc": 2,
            "per_table": [
                {
                    "catalog_id": 1,
                    "entity_name": "ods_order",
                    "source_id": "s1",
                    "entity_type": "TABLE",
                    "domain": "sales",
                    "sensitivity_level": "INTERNAL",
                    "table_desc": False,
                    "total_fields": 2,
                    "covered_fields": 1,
                    "missing_fields": 1,
                }
            ],
        }
    )
    with patch("app.api.collector._svc", return_value=fake_svc):
        resp = await collector_client.get("/api/v1/catalogs/description-coverage")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total_tables"] == 2
    assert data["fields_missing_desc"] == 2
    assert data["per_table"][0]["entity_name"] == "ods_order"


async def test_update_table_description_endpoint(
    collector_client: httpx.AsyncClient,
) -> None:
    """PUT /catalogs/{id}/description 人工编辑表级描述。"""
    fake_svc = MagicMock()
    fake_svc._repo.update_table_description = AsyncMock(
        return_value=SimpleNamespace(
            id=1,
            description="订单明细表",
            description_source="manual",
            description_updated_by=7,
            description_updated_at=None,
        )
    )
    with patch("app.api.collector._svc", return_value=fake_svc):
        resp = await collector_client.put(
            "/api/v1/catalogs/1/description",
            json={"description": "订单明细表"},
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["description"] == "订单明细表"
    assert data["source"] == "manual"
    assert data["catalog_id"] == 1


async def test_infer_table_description_endpoint(
    collector_client: httpx.AsyncClient,
) -> None:
    """POST /catalogs/{id}/infer-table-description LLM 推断表级描述并落库。"""
    fake_svc = MagicMock()
    fake_svc._llm_infer_table_description = AsyncMock(
        return_value={"description": "订单明细事实表", "confidence": 0.9}
    )
    fake_svc._repo.update_table_description = AsyncMock(
        return_value=SimpleNamespace(
            id=1,
            description="订单明细事实表",
            description_source="llm",
            description_updated_by=None,
            description_updated_at=None,
        )
    )
    with patch("app.api.collector._svc", return_value=fake_svc):
        resp = await collector_client.post(
            "/api/v1/catalogs/1/infer-table-description",
            json={"fields": [{"name": "order_id", "type": "bigint"}]},
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["description"] == "订单明细事实表"
    assert data["source"] == "llm"
    assert data["confidence"] == 0.9


async def test_infer_column_description_inflight_conflict(
    collector_client: httpx.AsyncClient,
) -> None:
    """FR-023: 单字段推断进行中时，重复请求返回 409 LLM_INFER_IN_PROGRESS。"""
    fake_svc = MagicMock()
    fake_svc._llm_infer_column_description = AsyncMock(
        return_value={"description": "订单ID", "confidence": 0.9}
    )
    fake_svc._repo.upsert_description = AsyncMock()
    # 幂等短路查询：无已有 LLM 描述 → 继续走 in-flight 锁
    fake_svc._repo.get_description = AsyncMock(return_value=None)
    mock_guard = MagicMock()
    mock_guard.acquire = AsyncMock(return_value=False)
    mock_guard.release = AsyncMock(return_value=True)
    with patch("app.api.collector._svc", return_value=fake_svc), patch(
        "app.api.collector.InferInflightGuard", return_value=mock_guard
    ):
        resp = await collector_client.post(
            "/api/v1/catalogs/1/columns/id/infer-description",
            json={"entity_name": "ods_order", "column_type": "bigint"},
        )
    assert resp.status_code == 409
    assert resp.json()["code"] == "LLM_INFER_IN_PROGRESS"
    # 推断未执行（被去重拦截）
    fake_svc._llm_infer_column_description.assert_not_awaited()
    mock_guard.acquire.assert_awaited_once()


async def test_infer_table_description_shortcircuits_existing_llm(
    collector_client: httpx.AsyncClient,
) -> None:
    """幂等短路：已有 LLM 表描述且未 force → 直接返回现有描述，不调 LLM。"""
    cat = SimpleNamespace(
        id=1,
        entity_name="ods_order",
        schema_json={},
        description="已有表描述",
        description_source="llm",
        description_updated_by=None,
        description_updated_at=None,
    )
    session = MagicMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = cat
    session.execute = AsyncMock(return_value=exec_result)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    fake_svc = MagicMock()
    fake_svc._llm_infer_table_description = AsyncMock(
        return_value={"description": "新描述", "confidence": 0.9}
    )
    app.dependency_overrides[deps.get_db_session] = fake_db
    try:
        with patch("app.api.collector._svc", return_value=fake_svc):
            resp = await collector_client.post(
                "/api/v1/catalogs/1/infer-table-description",
                json={"fields": []},
            )
    finally:
        app.dependency_overrides.pop(deps.get_db_session, None)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["description"] == "已有表描述"
    assert data["source"] == "llm"
    fake_svc._llm_infer_table_description.assert_not_awaited()


async def test_infer_table_description_force_regenerates(
    collector_client: httpx.AsyncClient,
) -> None:
    """force=true 时即使已有 LLM 表描述也重新推断。"""
    cat = SimpleNamespace(
        id=1,
        entity_name="ods_order",
        schema_json={},
        description="已有表描述",
        description_source="llm",
        description_updated_by=None,
        description_updated_at=None,
    )
    session = MagicMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = cat
    session.execute = AsyncMock(return_value=exec_result)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    fake_svc = MagicMock()
    fake_svc._llm_infer_table_description = AsyncMock(
        return_value={"description": "新描述", "confidence": 0.9}
    )
    fake_svc._repo.update_table_description = AsyncMock(
        return_value=SimpleNamespace(
            id=1,
            description="新描述",
            description_source="llm",
            description_updated_by=None,
            description_updated_at=None,
        )
    )
    app.dependency_overrides[deps.get_db_session] = fake_db
    try:
        with patch("app.api.collector._svc", return_value=fake_svc):
            resp = await collector_client.post(
                "/api/v1/catalogs/1/infer-table-description",
                json={"fields": [], "force": True},
            )
    finally:
        app.dependency_overrides.pop(deps.get_db_session, None)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["description"] == "新描述"
    fake_svc._llm_infer_table_description.assert_awaited_once()


async def test_infer_column_description_shortcircuits_existing_llm(
    collector_client: httpx.AsyncClient,
) -> None:
    """幂等短路：已有 LLM 字段描述且未 force → 直接返回现有描述，不调 LLM。"""
    cat = SimpleNamespace(id=1, entity_name="ods_order", schema_json={})
    session = MagicMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = cat
    session.execute = AsyncMock(return_value=exec_result)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    fake_svc = MagicMock()
    fake_svc._llm_infer_column_description = AsyncMock(
        return_value={"description": "新字段描述", "confidence": 0.9}
    )
    fake_svc._repo.get_description = AsyncMock(
        return_value=SimpleNamespace(source="llm", description="已有字段描述")
    )
    app.dependency_overrides[deps.get_db_session] = fake_db
    try:
        with patch("app.api.collector._svc", return_value=fake_svc):
            resp = await collector_client.post(
                "/api/v1/catalogs/1/columns/id/infer-description",
                json={"entity_name": "ods_order", "column_type": "bigint"},
            )
    finally:
        app.dependency_overrides.pop(deps.get_db_session, None)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["description"] == "已有字段描述"
    assert data["source"] == "llm"
    fake_svc._llm_infer_column_description.assert_not_awaited()


async def test_infer_descriptions_batch_inflight_conflict(
    collector_client: httpx.AsyncClient,
) -> None:
    """FR-023: 批量推断进行中时，重复请求返回 409，且不读取描述/不逐字段推断。"""
    fake_svc = MagicMock()
    fake_svc._repo.get_descriptions = AsyncMock(return_value=[])
    mock_guard = MagicMock()
    mock_guard.acquire = AsyncMock(return_value=False)
    mock_guard.release = AsyncMock(return_value=True)
    with patch("app.api.collector._svc", return_value=fake_svc), patch(
        "app.api.collector.InferInflightGuard", return_value=mock_guard
    ):
        resp = await collector_client.post("/api/v1/catalogs/1/infer-descriptions")
    assert resp.status_code == 409
    assert resp.json()["code"] == "LLM_INFER_IN_PROGRESS"
    fake_svc._repo.get_descriptions.assert_not_awaited()


async def test_infer_descriptions_batch_success_concurrent(
    collector_client: httpx.AsyncClient,
) -> None:
    """批量推断：一次 LLM 调用返回全部字段描述，按 column_name 回填并正确分类。"""
    cat = SimpleNamespace(
        id=1,
        entity_name="ods_order",
        schema_json={
            "columns": [
                {"name": "id", "type": "bigint", "comment": "主键"},
                {"name": "amount", "type": "decimal", "comment": ""},
                {"name": "note", "type": "varchar", "comment": ""},
            ]
        },
    )
    session = MagicMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = cat
    session.execute = AsyncMock(return_value=exec_result)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    fake_svc = MagicMock()
    fake_svc._repo.get_descriptions = AsyncMock(return_value=[])
    fake_svc._repo.upsert_description = AsyncMock()
    # 一次调用返回全部字段（顺序与请求清单不同，验证按 column_name 匹配）
    fake_svc._llm_infer_batch_descriptions = AsyncMock(
        return_value={"note": ("备注说明", 0.7), "amount": ("订单金额", 0.8)}
    )

    app.dependency_overrides[deps.get_db_session] = fake_db
    try:
        with patch("app.api.collector._svc", return_value=fake_svc):
            resp = await collector_client.post("/api/v1/catalogs/1/infer-descriptions")
    finally:
        app.dependency_overrides.pop(deps.get_db_session, None)

    assert resp.status_code == 200
    data = resp.json()["data"]
    # 已有 comment 且无描述记录 → 跳过
    assert data["skipped"] == ["id"]
    assert [i["column_name"] for i in data["inferred"]] == ["amount", "note"]
    assert data["failed"] == []
    # 仅一次 LLM 调用（整表清单一次发送）
    fake_svc._llm_infer_batch_descriptions.assert_awaited_once()
    # 按 targets 顺序回填（amount 在 note 前）
    assert data["inferred"][0]["description"] == "订单金额"
    assert data["inferred"][1]["description"] == "备注说明"
    # 写库保持 targets 顺序（upsert_description 走关键字参数）
    upsert_calls = [c.kwargs for c in fake_svc._repo.upsert_description.await_args_list]
    assert [c["column_name"] for c in upsert_calls] == ["amount", "note"]
    assert all(c["source"] == "llm" for c in upsert_calls)


async def test_infer_descriptions_batch_partial_failure(
    collector_client: httpx.AsyncClient,
) -> None:
    """批量推断：LLM 返回缺失个别字段 → 该字段进 failed，其余正常落库。"""
    cat = SimpleNamespace(
        id=1,
        entity_name="ods_order",
        schema_json={
            "columns": [
                {"name": "a", "type": "int", "comment": ""},
                {"name": "b", "type": "varchar", "comment": ""},
                {"name": "c", "type": "int", "comment": ""},
            ]
        },
    )
    session = MagicMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = cat
    session.execute = AsyncMock(return_value=exec_result)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    fake_svc = MagicMock()
    fake_svc._repo.get_descriptions = AsyncMock(return_value=[])
    fake_svc._repo.upsert_description = AsyncMock()
    # 只返回 a/b，c 缺失 → c 进 failed
    fake_svc._llm_infer_batch_descriptions = AsyncMock(
        return_value={"a": ("字段A", 0.8), "b": ("字段B", 0.6)}
    )

    app.dependency_overrides[deps.get_db_session] = fake_db
    try:
        with patch("app.api.collector._svc", return_value=fake_svc):
            resp = await collector_client.post("/api/v1/catalogs/1/infer-descriptions")
    finally:
        app.dependency_overrides.pop(deps.get_db_session, None)

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [i["column_name"] for i in data["inferred"]] == ["a", "b"]
    assert data["failed"] == ["c"]
    # c 未被 upsert
    upsert_cols = [
        c.kwargs["column_name"]
        for c in fake_svc._repo.upsert_description.await_args_list
    ]
    assert upsert_cols == ["a", "b"]


async def test_infer_table_description_inflight_conflict(
    collector_client: httpx.AsyncClient,
) -> None:
    """FR-023: 表级推断进行中时，重复请求返回 409。"""
    fake_svc = MagicMock()
    fake_svc._llm_infer_table_description = AsyncMock(
        return_value={"description": "订单明细事实表", "confidence": 0.9}
    )
    fake_svc._repo.update_table_description = AsyncMock()
    mock_guard = MagicMock()
    mock_guard.acquire = AsyncMock(return_value=False)
    mock_guard.release = AsyncMock(return_value=True)
    with patch("app.api.collector._svc", return_value=fake_svc), patch(
        "app.api.collector.InferInflightGuard", return_value=mock_guard
    ):
        resp = await collector_client.post(
            "/api/v1/catalogs/1/infer-table-description",
            json={"fields": [{"name": "order_id", "type": "bigint"}]},
        )
    assert resp.status_code == 409
    assert resp.json()["code"] == "LLM_INFER_IN_PROGRESS"
    fake_svc._llm_infer_table_description.assert_not_awaited()
