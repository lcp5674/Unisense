"""指标批量治理端点测试（ASGI：batch-submit/approve/reject/deprecate）。

覆盖逐条收集结果（不整体失败）、成功/失败混合、错误信封透传。
对齐 TD §13 批量治理闭环。
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.conftest import make_metric

from app.api import deps
from app.core.exceptions import BusinessError
from app.main import app


def _as_reviewer(role: str = "domain_admin"):
    """上下文：把当前用户角色覆盖为评审角色（client fixture 默认 metric_owner）。"""

    class _Ctx:
        def __enter__(self) -> None:
            app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
                id=1,
                role=role,
                domain="sales",
                roles_all=lambda: [role],
                has_role=lambda r: r == role,
            )

        def __exit__(self, *exc: object) -> None:
            app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
                id=1,
                role="metric_owner",
                roles_all=lambda: ["metric_owner"],
                has_role=lambda r: r == "metric_owner",
            )

    return _Ctx()


async def test_batch_submit_mixed_results(client):
    """批量提交：逐条收集，成功与失败并存（单条失败不阻断其余）。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.run_lineage_post_commit = AsyncMock()
        submitted = make_metric(status="REVIEW")

        async def fake_submit(code, request, **kwargs):
            if code == "bad_metric":
                raise BusinessError("指标不存在: bad_metric", error_code="NOT_FOUND")
            return submitted

        instance.submit_metric = AsyncMock(side_effect=fake_submit)

        resp = await client.post(
            "/api/v1/metric-definitions/batch-submit",
            json={
                "items": [
                    {"code": "sales_gmv_daily", "change_reason": "首次提交审核"},
                    {"code": "bad_metric", "change_reason": "首次提交审核"},
                ]
            },
        )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    assert body["fail_count"] == 1
    by_code = {r["code"]: r for r in body["results"]}
    assert by_code["sales_gmv_daily"]["ok"] is True
    assert by_code["bad_metric"]["ok"] is False
    assert "指标不存在" in by_code["bad_metric"]["message"]


async def test_batch_submit_sanitizes_unknown_exception(client):
    """P0-3: 未知异常（内部细节）→ 脱敏为通用提示，不泄漏连接串/路径等内部信息。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.run_lineage_post_commit = AsyncMock()
        instance.submit_metric = AsyncMock(
            side_effect=RuntimeError(
                "Connection to mysql://root:secret@db.internal:3306/universe failed (file:///etc/config)"
            )
        )

        resp = await client.post(
            "/api/v1/metric-definitions/batch-submit",
            json={"items": [{"code": "sales_gmv_daily", "change_reason": "首次提交审核"}]},
        )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["fail_count"] == 1
    message = body["results"][0]["message"]
    # 内部细节不泄漏
    assert "root:secret" not in message
    assert "db.internal" not in message
    assert "/etc/config" not in message
    # 业务失败仍带可读通用提示
    assert "操作失败" in message


async def test_batch_submit_passes_reviewer_assignment(client):
    """批量提交透传评审指派字段（reviewer_id/reviewer_type）。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.run_lineage_post_commit = AsyncMock()
        instance.submit_metric = AsyncMock(return_value=make_metric(status="REVIEW"))

        resp = await client.post(
            "/api/v1/metric-definitions/batch-submit",
            json={
                "items": [
                    {
                        "code": "sales_gmv_daily",
                        "change_reason": "提交审核",
                        "reviewer_id": 7,
                        "reviewer_type": "user",
                    }
                ]
            },
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["ok_count"] == 1
    req = instance.submit_metric.call_args.args[1]
    assert req.reviewer_id == 7
    assert req.reviewer_type == "user"


async def test_batch_approve_mixed_results(client):
    """批量通过：逐条收集，成功与失败并存。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.run_lineage_post_commit = AsyncMock()
        published = make_metric(status="PUBLISHED")

        async def fake_approve(code, request, **kwargs):
            if code == "pii_metric":
                raise BusinessError("PII 指标须先通过合规审核", error_code="COMPLIANCE_BLOCKED")
            return published

        instance.approve_metric = AsyncMock(side_effect=fake_approve)

        with _as_reviewer():
            resp = await client.post(
                "/api/v1/metric-definitions/batch-approve",
                json={"metric_codes": ["sales_gmv_daily", "pii_metric"], "mode": "standard"},
            )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    assert body["fail_count"] == 1
    by_code = {r["code"]: r for r in body["results"]}
    assert by_code["pii_metric"]["ok"] is False
    assert "合规" in by_code["pii_metric"]["message"]


async def test_batch_approve_audit_includes_failed_codes(client):
    """批量审计 detail 含失败明细（编码+原因），合规可逐条追溯。"""
    with (
        patch("app.api.metrics.MetricService") as mock_svc,
        patch("app.api.metrics.write_audit") as mock_audit,
    ):
        instance = mock_svc.return_value
        instance.run_lineage_post_commit = AsyncMock()
        published = make_metric(status="PUBLISHED")

        async def fake_approve(code, request, **kwargs):
            if code == "pii_metric":
                raise BusinessError("PII 指标须先通过合规审核", error_code="COMPLIANCE_BLOCKED")
            return published

        instance.approve_metric = AsyncMock(side_effect=fake_approve)

        with _as_reviewer():
            resp = await client.post(
                "/api/v1/metric-definitions/batch-approve",
                json={"metric_codes": ["sales_gmv_daily", "pii_metric"], "mode": "standard"},
            )

    assert resp.status_code == 200
    assert mock_audit.called
    detail = mock_audit.call_args.kwargs["detail"]
    assert "failed_codes" in detail
    assert any("pii_metric" in fc and "合规" in fc for fc in detail["failed_codes"])
    assert detail["ok"] == 1


async def test_batch_reject_mixed_results(client):
    """批量打回：逐条收集，成功与失败并存。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.run_lineage_post_commit = AsyncMock()

        async def fake_reject(code, request, **kwargs):
            if code == "x":
                raise BusinessError("指标不存在: x", error_code="NOT_FOUND")
            return make_metric(status="DRAFT")

        instance.reject_metric = AsyncMock(side_effect=fake_reject)

        with _as_reviewer():
            resp = await client.post(
                "/api/v1/metric-definitions/batch-reject",
                json={"codes": ["sales_gmv_daily", "x"], "reason": "口径需调整"},
            )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    assert body["fail_count"] == 1


async def test_batch_deprecate_mixed_results(client):
    """批量下线：逐条收集，successor 未发布失败不影响其余。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.run_lineage_post_commit = AsyncMock()

        async def fake_deprecate(code, successor_code, **kwargs):
            if code == "no_successor":
                raise BusinessError(
                    f"替代指标 {successor_code} 未发布，无法作为替代",
                    error_code="VALIDATION_ERROR",
                )
            return make_metric(status="DEPRECATED")

        instance.deprecate_metric = AsyncMock(side_effect=fake_deprecate)

        resp = await client.post(
            "/api/v1/metric-definitions/batch-deprecate",
            json={
                "items": [
                    {"metric_code": "sales_gmv_daily", "successor_code": "sales_gmv_weekly"},
                    {"metric_code": "no_successor", "successor_code": "ghost_metric"},
                ]
            },
        )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    assert body["fail_count"] == 1
    by_code = {r["code"]: r for r in body["results"]}
    assert by_code["no_successor"]["ok"] is False
    assert "未发布" in by_code["no_successor"]["message"]


async def test_batch_submit_empty_items_422(client):
    """空 items 列表 → 422（schema 约束）。"""
    resp = await client.post("/api/v1/metric-definitions/batch-submit", json={"items": []})
    assert resp.status_code == 422


# ---- P3-18: _run_batch 内部助手（DB 级异常回滚 + 逐条容错）----


async def test_run_batch_all_success():
    """全部成功 → 逐条 ok，不触发回滚。"""
    from app.api.batch_common import run_batch

    db = MagicMock()
    db.rollback = AsyncMock()
    units = [{"code": "a"}, {"code": "b"}]
    run = AsyncMock()
    results = await run_batch(
        db, units=units, code_of=lambda u: u["code"], run=run, abort_message="aborted"
    )
    assert [r.code for r in results] == ["a", "b"]
    assert all(r.ok for r in results)
    assert run.await_count == 2
    db.rollback.assert_not_awaited()


async def test_run_batch_business_error_continues():
    """业务异常（非 SQLAlchemy）→ 单条失败，其余继续，不整体回滚。"""
    from app.api.batch_common import run_batch
    from app.core.exceptions import BusinessError

    db = MagicMock()
    db.rollback = AsyncMock()
    units = [{"code": "a"}, {"code": "b"}, {"code": "c"}]

    async def run(unit):
        if unit["code"] == "b":
            raise BusinessError("指标不存在", error_code="NOT_FOUND")

    results = await run_batch(
        db, units=units, code_of=lambda u: u["code"], run=run, abort_message="aborted"
    )
    assert [r.ok for r in results] == [True, False, True]
    assert results[1].message == "指标不存在"
    # 业务失败不污染会话：不回滚
    db.rollback.assert_not_awaited()


async def test_run_batch_db_error_rolls_back_and_marks_rest():
    """SQLAlchemyError → 回滚会话 + **全部已执行项改标失败**（失真修复）+ 中止。

    结果失真修复：DB 级异常整会话回滚会把此前已 flush 未 commit 的成功项一并丢弃，
    若不改标失败，响应/审计会宣称「N 项成功」而库中实际未落。修复后此前 ok 的项
    也统一标记 abort_message 失败。
    """
    from sqlalchemy.exc import SQLAlchemyError

    from app.api.batch_common import run_batch

    db = MagicMock()
    db.rollback = AsyncMock()
    units = [{"code": "a"}, {"code": "b"}, {"code": "c"}]

    async def run(unit):
        if unit["code"] == "b":
            raise SQLAlchemyError("connection lost")

    results = await run_batch(
        db, units=units, code_of=lambda u: u["code"], run=run, abort_message="DB 异常中止批量"
    )
    # a 此前成功但被整会话回滚（未落库）→ 改标失败；b 触发 DB 异常 → 自身失败；
    # c 未处理 → 统一 abort_message 失败后中止
    assert [r.code for r in results] == ["a", "b", "c"]
    assert all(r.ok is False for r in results)
    assert all(r.message == "DB 异常中止批量" for r in results)
    db.rollback.assert_awaited_once()


def test_batch_audit_action_levels():
    """审计动作名区分全成功/部分失败/全失败。"""
    from app.api.batch_common import BatchItemResult, batch_audit_action

    ok = BatchItemResult(code="a", ok=True)
    fail = BatchItemResult(code="b", ok=False, message="x")
    assert (
        batch_audit_action("metric_definition.batch_submit", [ok, ok])
        == "metric_definition.batch_submit"
    )
    assert (
        batch_audit_action("metric_definition.batch_submit", [fail, fail])
        == "metric_definition.batch_submit_failed"
    )
    assert (
        batch_audit_action("metric_definition.batch_submit", [ok, fail])
        == "metric_definition.batch_submit_partial"
    )


async def test_downstream_check_returns_per_metric(client):
    """批量下线下游审查端点：一次查询返回每指标引用者数量与明细。

    覆盖为 platform_admin——下游引用者中可能含私有（DRAFT/REVIEW）派生指标，
    非管理角色的 P0-3 行级过滤会剔除不可见引用者（该过滤逻辑由
    test_semantic_service/test_semantic_repository 独立覆盖）。
    """
    from app.api import deps

    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    with patch(
        "app.services.lineage.repository.LineageRepository.metric_referrers_batch"
    ) as mock_batch:
        mock_batch.return_value = {
            "sales_gmv_daily": [
                {"node": "metric:sales_gmv_derived", "edge_type": "DERIVED_FROM"},
                {"node": "consumer:bi_report", "edge_type": "CONSUMED_BY"},
            ],
            "sales_uv_daily": [],
        }
        resp = await client.post(
            "/api/v1/metric-definitions/downstream-check",
            json={"metric_codes": ["sales_gmv_daily", "sales_uv_daily"]},
        )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body[0]["metric_code"] == "sales_gmv_daily"
    assert body[0]["referrer_count"] == 2
    assert body[0]["referrers"][0]["node"] == "metric:sales_gmv_derived"
    assert body[1]["metric_code"] == "sales_uv_daily"
    assert body[1]["referrer_count"] == 0
    assert body[1]["referrers"] == []


# ---- 通用批量导入（batch-import / import-csv / template）----


async def test_batch_import_auto_fills_code_and_name(client):
    """batch-import：编码/名称缺省时自动补全，构造的创建端候选携带自动生成的 4 段式编码。"""
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.batch_register_from_sql = AsyncMock(
            return_value={
                "batch_id": "sqlbatch_ab12",
                "candidates": [
                    {"metric_code": "outp_doctor_active_cnt_month", "status": "DRAFT"},
                ],
            }
        )
        resp = await client.post(
            "/api/v1/metric-definitions/batch-import",
            json={
                "domain": "outp",
                "source": "agent",
                "candidates": [
                    {
                        # metric_code/name 缺省——应自动补全
                        "type": "atomic",
                        "source_table": "wedw_dws.doctor_active_month_di",
                        "measure_column": "current_month_active_doctor_cnt",
                        "aggregation": "COUNT_DISTINCT",
                        "period": "month",
                        "expression": "COUNT(DISTINCT doctor_code)",
                    }
                ],
            },
        )
    assert resp.status_code == 200
    inner: MetricSqlBatchRegisterRequest = instance.batch_register_from_sql.call_args.args[0]
    cand = inner.candidates[0]
    # 自动补全：outp_{源表末段去通用后缀 _di}_{度量列}_{周期}
    assert cand.metric_code == "outp_doctor_active_month_current_month_active_doctor_cnt_month"
    assert cand.name  # 自动生成非空中文名
    assert cand.definition_json.get("expression") == "COUNT(DISTINCT doctor_code)"


async def test_batch_import_reuses_explicit_code_and_name(client):
    """batch-import：显式编码/名称原样保留，不覆盖。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.batch_register_from_sql = AsyncMock(
            return_value={
                "batch_id": "b",
                "candidates": [{"metric_code": "outp_my_metric_day", "status": "DRAFT"}],
            }
        )
        resp = await client.post(
            "/api/v1/metric-definitions/batch-import",
            json={
                "domain": "outp",
                "candidates": [
                    {
                        "metric_code": "outp_my_metric_day",
                        "name": "我的指标",
                        "type": "atomic",
                        "expression": "SUM(amount)",
                    }
                ],
            },
        )
    assert resp.status_code == 200
    cand = instance.batch_register_from_sql.call_args.args[0].candidates[0]
    assert cand.metric_code == "outp_my_metric_day"
    assert cand.name == "我的指标"


async def test_import_csv_parses_and_creates(client):
    """import-csv：上传 CSV 逐行解析为候选，调批量注册创建 DRAFT。"""
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    csv_text = (
        "metric_code,name,type,source_table,measure_column,aggregation,unit,period,"
        "granularity,measure_id,expression,dependencies,raw_sql\n"
        "outp_doctor_active_cnt_month,月活医生数,atomic,wedw_dws.doctor_active_month_di,"
        "current_month_active_doctor_cnt,COUNT_DISTINCT,人,month,,,COUNT(DISTINCT doctor_code),,\n"
        ",上月活跃医生数,atomic,wedw_dws.doctor_active_month_di,last_month_active_doctor_cnt,"
        "COUNT_DISTINCT,人,month,,,COALESCE(COUNT(DISTINCT CASE WHEN x THEN 1 END), 0),,\n"
    )
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.batch_register_from_sql = AsyncMock(
            return_value={
                "batch_id": "csv_b1",
                "candidates": [
                    {"metric_code": "outp_doctor_active_cnt_month", "status": "DRAFT"},
                    {"metric_code": "outp_lastmonth_doctor_active_cnt_month", "status": "DRAFT"},
                ],
            }
        )
        resp = await client.post(
            "/api/v1/metric-definitions/imports/csv",
            files={"file": ("metrics.csv", csv_text.encode("utf-8-sig"), "text/csv")},
            data={"domain": "outp"},
        )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["batch_id"] == "csv_b1"
    inner: MetricSqlBatchRegisterRequest = instance.batch_register_from_sql.call_args.args[0]
    assert len(inner.candidates) == 2
    # 第二行 metric_code 缺省 → 自动补全（源表末段去后缀 + 度量列 + 周期）
    assert inner.candidates[1].metric_code.endswith("_last_month_active_doctor_cnt_month")
    assert inner.candidates[1].name == "上月活跃医生数"


async def test_import_csv_chinese_headers_parsed(client):
    """import-csv：中文表头 CSV 归一化解析为候选（与英文表头等价）。"""
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    csv_text = (
        "指标编码(可空),指标名称(可空),指标类型,来源表,度量列,聚合方式,单位,统计周期,粒度,"
        "逻辑度量ID(可空),口径表达式,依赖指标(可空,|分隔),原始SQL(可空)\n"
        "outp_gmv_day,门诊金额,derived,dwd.outp_fee_di,gmv,SUM,CNY,day,,,SUM(amount),,\n"
        ",上月活跃医生数,atomic,wedw_dws.doctor_active_month_di,last_month_active_doctor_cnt,"
        "COUNT_DISTINCT,PERSON,month,,,COALESCE(COUNT(DISTINCT CASE WHEN x THEN 1 END), 0),,\n"
    )
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.batch_register_from_sql = AsyncMock(
            return_value={"batch_id": "cn_b1", "candidates": []}
        )
        resp = await client.post(
            "/api/v1/metric-definitions/imports/csv",
            files={"file": ("m.csv", csv_text.encode("utf-8"), "text/csv")},
            data={"domain": "outp"},
        )
    assert resp.status_code == 200
    inner: MetricSqlBatchRegisterRequest = instance.batch_register_from_sql.call_args.args[0]
    by_code = {c.metric_code: c for c in inner.candidates}
    assert by_code["outp_gmv_day"].name == "门诊金额"
    assert by_code["outp_gmv_day"].type == "derived"
    assert by_code["outp_gmv_day"].unit == "CNY"
    auto = by_code["outp_doctor_active_month_last_month_active_doctor_cnt_month"]
    assert auto.name == "上月活跃医生数"
    assert auto.unit == "PERSON"


async def test_import_csv_skips_template_comment_lines(client):
    """import-csv：模板注释行（-- 开头）被跳过，不产生 row_errors（此前会误报 expression 缺失）。"""
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    csv_text = (
        "指标编码(可空),指标名称(可空),指标类型,来源表,度量列,聚合方式,单位,统计周期,粒度,"
        "逻辑度量ID(可空),口径表达式,依赖指标(可空,|分隔),原始SQL(可空)\n"
        "outp_gmv_day,门诊金额,derived,dwd.outp_fee_di,gmv,SUM,CNY,day,,,SUM(amount),,\n"
        "-- 示例行：指标编码/指标名称可空；口径表达式必填；依赖指标用 | 分隔\n"
        "# 另一条注释（# 开头同样跳过）\n"
    )
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.batch_register_from_sql = AsyncMock(
            return_value={"batch_id": "cmt_b1", "candidates": []}
        )
        resp = await client.post(
            "/api/v1/metric-definitions/imports/csv",
            files={"file": ("m.csv", csv_text.encode("utf-8"), "text/csv")},
            data={"domain": "outp"},
        )
    assert resp.status_code == 200
    inner: MetricSqlBatchRegisterRequest = instance.batch_register_from_sql.call_args.args[0]
    assert len(inner.candidates) == 1
    assert resp.json()["data"].get("row_errors") is None


async def test_import_xlsx_chinese_headers_parsed(client):
    """import-csv：中文表头 xlsx 归一化解析为候选（与英文表头等价）。"""
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(
        ["指标编码(可空)", "指标名称(可空)", "指标类型", "来源表", "度量列", "聚合方式",
         "单位", "统计周期", "粒度", "逻辑度量ID(可空)", "口径表达式",
         "依赖指标(可空,|分隔)", "原始SQL(可空)"]
    )
    ws.append(
        ["outp_gmv_day", "门诊金额", "derived", "dwd.outp_fee_di", "gmv", "SUM",
         "CNY", "day", "", "", "SUM(amount)", "", ""]
    )
    buf = io.BytesIO()
    wb.save(buf)
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.batch_register_from_sql = AsyncMock(
            return_value={"batch_id": "cnx_b1", "candidates": []}
        )
        resp = await client.post(
            "/api/v1/metric-definitions/imports/csv",
            files={
                "file": (
                    "m.xlsx",
                    buf.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            data={"domain": "outp"},
        )
    assert resp.status_code == 200
    inner: MetricSqlBatchRegisterRequest = instance.batch_register_from_sql.call_args.args[0]
    assert len(inner.candidates) == 1
    assert inner.candidates[0].metric_code == "outp_gmv_day"
    assert inner.candidates[0].unit == "CNY"


async def test_import_xlsx_parses_and_creates(client):
    """import-csv：上传 xlsx（openpyxl 构造）→ 逐行解析为候选，调批量注册创建 DRAFT。"""
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(
        ["metric_code", "name", "type", "source_table", "measure_column", "aggregation",
         "unit", "period", "granularity", "measure_id", "expression", "dependencies", "raw_sql"]
    )
    ws.append(
        ["outp_gmv_day", "门诊金额", "derived", "dwd.outp_fee_di", "gmv", "SUM",
         "元", "day", "", "", "SUM(amount)", "", ""]
    )
    ws.append([None] * 13)  # 空行应被跳过
    ws.append(
        ["", "第二行", "atomic", "dwd.t2", "cnt", "COUNT",
         "", "", "", "", "COUNT(1)", "", ""]
    )
    buf = io.BytesIO()
    wb.save(buf)
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.batch_register_from_sql = AsyncMock(
            return_value={
                "batch_id": "xlsx_b1",
                "candidates": [
                    {"metric_code": "outp_gmv_day", "status": "DRAFT"},
                    {"metric_code": "dwd_t2_cnt_day", "status": "DRAFT"},
                ],
            }
        )
        resp = await client.post(
            "/api/v1/metric-definitions/imports/csv",
            files={
                "file": (
                    "metrics.xlsx",
                    buf.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            data={"domain": "outp"},
        )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["batch_id"] == "xlsx_b1"
    inner: MetricSqlBatchRegisterRequest = instance.batch_register_from_sql.call_args.args[0]
    assert len(inner.candidates) == 2
    by_code = {c.metric_code: c for c in inner.candidates}
    assert "outp_gmv_day" in by_code
    assert by_code["outp_gmv_day"].name == "门诊金额"
    # 第二行 metric_code 缺省 → 自动补全（原子先行排序，按编码查找断言）
    assert by_code["outp_t2_cnt_day"].name == "第二行"


async def test_import_xlsx_template_download(client):
    """imports/template?format=xlsx：返回 xlsx 模板（中文表头 + 示例行 + 枚举下拉 + 选项字典）。"""
    openpyxl = pytest.importorskip("openpyxl")
    resp = await client.get(
        "/api/v1/metric-definitions/imports/template", params={"format": "xlsx"}
    )
    assert resp.status_code == 200
    assert "spreadsheetml" in resp.headers["content-type"]
    assert resp.content[:2] == b"PK"
    wb = openpyxl.load_workbook(io.BytesIO(resp.content), read_only=False)
    ws = wb["指标导入模板"]
    rows = list(ws.iter_rows(values_only=True))
    assert list(rows[0]) == [
        "指标编码(可空)", "指标名称(可空)", "指标类型", "来源表", "度量列",
        "聚合方式", "单位", "统计周期", "粒度", "逻辑度量ID(可空)", "口径表达式",
        "依赖指标(可空,|分隔)", "原始SQL(可空)",
    ]
    assert len(rows) >= 2  # 表头 + 示例行
    # 示例行单位用字典 code（PERSON，非中文「人」）
    assert rows[1][6] == "PERSON"
    # 枚举下拉已绑定：指标类型列 C 数据行范围 + 单位列 G
    dvs = ws.data_validations.dataValidation
    assert any(dv.formula1 == '"atomic,derived,composite"' for dv in dvs)
    assert any("C2:C1000" in str(dv.sqref) for dv in dvs)
    unit_dv = '"CNY_WAN,CNY_YI,CNY,USD,EUR,PERCENT,ORDER,PERSON,TIMES,DAY,HOUR,MINUTE"'
    assert any(dv.formula1 == unit_dv for dv in dvs)
    assert any("G2:G1000" in str(dv.sqref) for dv in dvs)
    # 选项字典工作表（字段 → 可选 code）
    assert "选项字典" in wb.sheetnames
    hint_rows = list(wb["选项字典"].iter_rows(values_only=True))
    assert hint_rows[0] == ("字段", "可选值（导入须用 code，勿填中文）")
    assert any(r[0] == "granularity" for r in hint_rows)
    assert any(r[0] == "aggregation" and "COUNT_DISTINCT" in r[1] for r in hint_rows)


async def test_import_csv_invalid_rows_reported(client):
    """import-csv：非法行（expression 必填缺失）记 row_errors，不阻断有效行。"""
    csv_text = (
        "metric_code,name,type,source_table,measure_column,aggregation,expression\n"
        "outp_good_day,好指标,atomic,,col,SUM,SUM(col)\n"
        "outp_bad_day,坏指标,atomic,,col,SUM,\n"  # expression 空 → 行解析失败
    )
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.batch_register_from_sql = AsyncMock(
            return_value={
                "batch_id": "csv_b2",
                "candidates": [{"metric_code": "outp_good_day", "status": "DRAFT"}],
            }
        )
        resp = await client.post(
            "/api/v1/metric-definitions/imports/csv",
            files={"file": ("m.csv", csv_text.encode("utf-8"), "text/csv")},
            data={"domain": "outp"},
        )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["row_errors"] and body["row_errors"][0]["row"] == 3
    assert "expression" in body["row_errors"][0]["error"]
    # 有效行仍创建
    assert len(instance.batch_register_from_sql.call_args.args[0].candidates) == 1


async def test_import_csv_no_valid_rows_400(client):
    """import-csv：全行非法/空 → 400 INVALID_CSV。"""
    csv_text = "metric_code,name,type,expression\n"
    resp = await client.post(
        "/api/v1/metric-definitions/imports/csv",
        files={"file": ("empty.csv", csv_text.encode("utf-8"), "text/csv")},
        data={"domain": "outp"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_CSV"


async def test_import_template_download(client):
    """imports/template：返回 CSV 模板（中文表头 + 示例行 + 选项说明注释行）。"""
    resp = await client.get("/api/v1/metric-definitions/imports/template")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "指标编码" in resp.text
    assert "口径表达式" in resp.text
    assert "-- 示例行" in resp.text


async def test_batch_purge_mixed_results(client):
    """回收站批量彻底删除：逐条收集，未删/非管理员项失败不影响其余（仅平台管理员）。"""
    with _as_reviewer("platform_admin"), patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value

        async def fake_purge(code, **kwargs):
            if code == "not_deleted":
                raise BusinessError(
                    f"指标 {code} 未处于已删除状态，无需彻底删除",
                    error_code="INVALID_STATE",
                )
            return None

        instance.purge_metric = AsyncMock(side_effect=fake_purge)

        resp = await client.post(
            "/api/v1/metric-definitions/batch-purge",
            json={"metric_codes": ["sales_gmv_daily", "not_deleted"]},
        )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    assert body["fail_count"] == 1
    by_code = {r["code"]: r for r in body["results"]}
    assert by_code["not_deleted"]["ok"] is False
    assert "无需彻底删除" in by_code["not_deleted"]["message"]


async def test_batch_purge_forbidden_for_non_admin(client):
    """非平台管理员调用 batch-purge → 403（require_roles 门禁）。"""
    with _as_reviewer("domain_admin"), patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.purge_metric = AsyncMock(return_value=None)
        resp = await client.post(
            "/api/v1/metric-definitions/batch-purge",
            json={"metric_codes": ["sales_gmv_daily"]},
        )
    assert resp.status_code == 403


async def test_batch_purge_empty_codes_422(client):
    """空 metric_codes → 422（schema 约束）。"""
    with _as_reviewer("platform_admin"):
        resp = await client.post(
            "/api/v1/metric-definitions/batch-purge", json={"metric_codes": []}
        )
    assert resp.status_code == 422
