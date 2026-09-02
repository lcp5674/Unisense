"""主数据批量治理端点测试（ASGI：measure/dimension/term batch-submit/approve）。

覆盖共享 ``app.api.batch_common`` 接入三模块的接线正确性：
- ``batch-submit`` 逐条调用 service.submit_*（含域作用域预检）
- ``batch-approve`` 逐条调用 service.approve_*（评审角色）
- 响应统一 ``BatchResponse``（results[].code）
- 逐条失败不阻断其余（单条抛业务异常，其余仍成功）

对齐 TD §13 批量治理闭环（与指标 batch 端点同一套共享执行语义）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.api import deps
from app.core.exceptions import BusinessError
from app.main import app


def _as_user(role: str, domain: str | None = "finance"):
    """覆盖当前用户角色与域（批量 submit 域作用域/approve 评审角色均需）。"""

    class _Ctx:
        def __enter__(self) -> None:
            app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
                id=1,
                role=role,
                domain=domain,
                roles_all=lambda: [role],
                has_role=lambda r: r == role,
                domains_all=lambda: [domain] if domain else [],
            )

        def __exit__(self, *exc: object) -> None:
            app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
                id=1,
                role="metric_owner",
                roles_all=lambda: ["metric_owner"],
                has_role=lambda r: r == "metric_owner",
                domains_all=lambda: [],
            )

    return _Ctx()


def _entity(code: str) -> MagicMock:
    """最小实体行（含域/状态/owner，满足域作用域与 service 层守卫）。"""
    e = MagicMock()
    e.code = code
    e.measure_code = code
    e.dim_code = code
    e.term_code = code
    e.domain = "finance"
    e.status = "DRAFT"
    e.owner_id = 1
    return e


# ---- 逻辑度量（/measure-catalogs）----


async def test_measure_batch_submit(client):
    """批量提交逻辑度量审核：逐条走 service.submit_measure，响应统一 BatchResponse。"""
    with patch("app.api.measure_catalog.MeasureCatalogService") as mock_svc:
        instance = mock_svc.return_value
        entity = _entity("medical_fee_men_zhen")
        instance.get_measure = AsyncMock(return_value=entity)
        instance.submit_measure = AsyncMock(return_value=entity)

        with _as_user("metric_owner", "finance"):
            resp = await client.post(
                "/api/v1/measure-catalogs/batch-submit",
                json={"items": [{"code": "medical_fee_men_zhen", "change_reason": "口径已对齐"}]},
            )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    assert body["fail_count"] == 0
    assert body["results"][0]["code"] == "medical_fee_men_zhen"
    assert body["results"][0]["ok"] is True
    instance.submit_measure.assert_awaited_once()


async def test_measure_batch_submit_partial_failure(client):
    """批量提交：单条业务失败不阻断其余（207 语义）。"""
    with patch("app.api.measure_catalog.MeasureCatalogService") as mock_svc:
        instance = mock_svc.return_value
        entity = _entity("medical_fee_men_zhen")
        instance.get_measure = AsyncMock(return_value=entity)

        async def fake_submit(code, request, **kwargs):
            if code == "bad_measure":
                raise BusinessError("逻辑度量不存在: bad_measure", error_code="NOT_FOUND")

        instance.submit_measure = AsyncMock(side_effect=fake_submit)

        with _as_user("metric_owner", "finance"):
            resp = await client.post(
                "/api/v1/measure-catalogs/batch-submit",
                json={
                    "items": [
                        {"code": "medical_fee_men_zhen", "change_reason": "口径已对齐"},
                        {"code": "bad_measure", "change_reason": "口径已对齐"},
                    ]
                },
            )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    assert body["fail_count"] == 1
    by_code = {r["code"]: r for r in body["results"]}
    assert by_code["medical_fee_men_zhen"]["ok"] is True
    assert by_code["bad_measure"]["ok"] is False
    assert "不存在" in by_code["bad_measure"]["message"]


async def test_measure_batch_approve(client):
    """批量通过逻辑度量：评审角色调用 service.approve_measure。"""
    with patch("app.api.measure_catalog.MeasureCatalogService") as mock_svc:
        instance = mock_svc.return_value
        instance.approve_measure = AsyncMock(return_value=_entity("medical_fee_men_zhen"))

        with _as_user("domain_admin"):
            resp = await client.post(
                "/api/v1/measure-catalogs/batch-approve",
                json={"codes": ["medical_fee_men_zhen"]},
            )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    instance.approve_measure.assert_awaited_once()


# ---- 维度（/dimensions）----


async def test_dimension_batch_submit(client):
    """批量提交维度审核：逐条走 service.submit_dimension（含域作用域预检）。"""
    with patch("app.api.dimension.DimensionService") as mock_svc:
        instance = mock_svc.return_value
        entity = _entity("dim_keshi")
        instance.get_dimension = AsyncMock(return_value=entity)
        instance.submit_dimension = AsyncMock(return_value=entity)

        with _as_user("metric_owner", "finance"):
            resp = await client.post(
                "/api/v1/dimensions/batch-submit",
                json={"items": [{"code": "dim_keshi", "change_reason": "科室维度口径已对齐"}]},
            )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    assert body["results"][0]["code"] == "dim_keshi"
    instance.submit_dimension.assert_awaited_once()


async def test_dimension_batch_deprecate(client):
    """批量废弃维度：逐条走 service.deprecate_dimension。"""
    with patch("app.api.dimension.DimensionService") as mock_svc:
        instance = mock_svc.return_value
        entity = _entity("dim_keshi")
        instance.get_dimension = AsyncMock(return_value=entity)
        instance.deprecate_dimension = AsyncMock(return_value=entity)

        with _as_user("metric_owner", "finance"):
            resp = await client.post(
                "/api/v1/dimensions/batch-deprecate",
                json={"codes": ["dim_keshi"]},
            )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    instance.deprecate_dimension.assert_awaited_once_with("dim_keshi")


# ---- 术语（/terms）----


async def test_term_batch_submit(client):
    """批量提交术语审核：逐条走 service.submit_term（审核流提交）。"""
    with patch("app.api.glossary.GlossaryService") as mock_svc:
        instance = mock_svc.return_value
        entity = _entity("term_chufang")
        instance.submit_term = AsyncMock(return_value=entity)

        resp = await client.post(
            "/api/v1/terms/batch-submit",
            json={"items": [{"code": "term_chufang", "change_reason": "术语定义已对齐"}]},
        )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    instance.submit_term.assert_awaited_once()


async def test_term_batch_approve(client):
    """批量通过术语：评审角色调用 service.approve_term。"""
    with patch("app.api.glossary.GlossaryService") as mock_svc:
        instance = mock_svc.return_value
        instance.approve_term = AsyncMock(return_value=_entity("term_chufang"))

        with _as_user("domain_admin"):
            resp = await client.post(
                "/api/v1/terms/batch-approve",
                json={"codes": ["term_chufang"]},
            )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    instance.approve_term.assert_awaited_once()


async def test_term_batch_reject(client):
    """批量驳回术语：评审角色调用 service.reject_term（原因透传）。"""
    with patch("app.api.glossary.GlossaryService") as mock_svc:
        instance = mock_svc.return_value
        instance.reject_term = AsyncMock(return_value=_entity("term_chufang"))

        with _as_user("domain_admin"):
            resp = await client.post(
                "/api/v1/terms/batch-reject",
                json={"codes": ["term_chufang"], "reason": "定义与业务实际不符，请补充"},
            )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    instance.reject_term.assert_awaited_once()
