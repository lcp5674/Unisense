"""consume 服务单元测试（TD §12.6 / FR-12,13）。

聚焦纯逻辑：接入方鉴权、dry-run 口径校验、查询 OLAP 降级、限流闸门。
DB / 仓库以 MagicMock 隔离（对齐 DEV_GUIDE §8b 单元标准），不连真实依赖。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest

import app.services.consume.service as consume_module
from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import BusinessError, ConflictError, NotFoundError
from app.core.security import hash_password
from app.models.consume import (
    ApiClient,
    ApiClientStatus,
    MetricValueSnapshot,
    SnapshotGeneratedBy,
)
from app.models.metric import Metric
from app.models.metric_version import MetricVersion
from app.services.consume.rate_limiter import get_rate_limiter
from app.services.consume.schemas import QueryRequest
from app.services.consume.service import ConsumeService


async def _client(
    secret: str = "s3cr3t",
    whitelist=None,
    qps: int = 2,
    status=ApiClientStatus.ACTIVE,
    scope_domain: str | None = None,
    daily_quota: int = 100,
) -> ApiClient:
    c = ApiClient()
    c.client_id = "acme"
    c.client_secret_ref = await hash_password(secret)
    c.status = status
    c.metric_whitelist = whitelist
    c.scope_domain = scope_domain
    c.qps = qps
    c.daily_quota = daily_quota
    c.created_by = 1
    return c


def _metric(
    status="PUBLISHED",
    code: str = "gmv",
    dims=("region",),
    expr="SUM(x)",
    grain="day",
    domain: str | None = "sales",
    pii: bool = False,
) -> Metric:
    m = Metric()
    m.metric_code = code
    m.status = status
    m.owner_org = 1
    m.domain = domain
    m.definition_json = {
        "expression": expr,
        "dependencies": ["fct_order"],
        "dimensions": list(dims),
        "grain": grain,
        "unit": "yuan",
        "pii": pii,
    }
    return m


@pytest.fixture(autouse=True)
def reset_limiter() -> None:
    limiter = get_rate_limiter()
    # Only InMemoryRateLimiter has _buckets/_daily; RedisRateLimiter uses Redis storage.
    # Tests mock db/session, so we expect InMemory in unit tests.
    if hasattr(limiter, "_buckets") and hasattr(limiter, "_daily"):
        limiter._buckets.clear()  # type: ignore[union-attr]
        limiter._daily.clear()  # type: ignore[union-attr]
    yield


def _svc(client: ApiClient) -> ConsumeService:
    svc = ConsumeService(MagicMock())
    svc._clients = MagicMock()
    svc._clients.get_by_client_id = AsyncMock(return_value=client)
    return svc


# ---- 接入方鉴权 ----
async def test_authenticate_ok() -> None:
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    assert client.client_id == "acme"


async def test_authenticate_bad_secret() -> None:
    svc = _svc(await _client())
    with pytest.raises(BusinessError):
        await svc.authenticate_client("acme:wrong")


async def test_authenticate_revoked() -> None:
    svc = _svc(await _client(status=ApiClientStatus.REVOKED))
    with pytest.raises(BusinessError):
        await svc.authenticate_client("acme:s3cr3t")


async def test_authenticate_bad_format() -> None:
    svc = _svc(await _client())
    with pytest.raises(BusinessError):
        await svc.authenticate_client("acme_no_colon")


# ---- dry-run ----
async def test_dry_run_ok() -> None:
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric())
    res = await svc.dry_run_query(
        QueryRequest(metric_code="gmv", date_range="2026-01~2026-03"), client
    )
    assert res.status == "ok"
    assert res.meta["grain"] == "day"
    assert res.meta["unit"] == "yuan"
    assert res.execution_plan["expression_ast"]["raw"] == "SUM(x)"
    # dry-run 必须下发真实物理口径 SQL（而非占位注释），并附回参数化参数
    assert "dws_metric_gmv" in res.execution_plan["dialect_sql"]
    assert "placeholder" not in res.execution_plan["dialect_sql"]
    assert res.execution_plan["sql_params"]["metric_code"] == "gmv"


async def test_dry_run_deprecated() -> None:
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(status="DEPRECATED"))
    with pytest.raises(BusinessError):
        await svc.dry_run_query(QueryRequest(metric_code="gmv", date_range=""), client)


async def test_dry_run_scope_denied() -> None:
    svc = _svc(await _client(whitelist=["other_metric"]))
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric())
    with pytest.raises(BusinessError):
        await svc.dry_run_query(QueryRequest(metric_code="gmv", date_range=""), client)


async def test_dry_run_cross_domain_denied() -> None:
    """scope_domain 限定域外指标必须拒绝（FORBIDDEN_DOMAIN，fail-closed 越权闸门）。"""
    svc = _svc(await _client(scope_domain="finance"))
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(domain="sales"))
    with pytest.raises(BusinessError) as exc:
        await svc.dry_run_query(QueryRequest(metric_code="gmv", date_range=""), client)
    assert exc.value.error_code == ErrorCode.FORBIDDEN_DOMAIN


async def test_dry_run_pii_requires_explicit_whitelist() -> None:
    """PII 指标在"域内全量"授权下不可隐式访问（FORBIDDEN_PII）。"""
    svc = _svc(await _client(whitelist=None))
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(pii=True))
    with pytest.raises(BusinessError) as exc:
        await svc.dry_run_query(QueryRequest(metric_code="gmv", date_range=""), client)
    assert exc.value.error_code == ErrorCode.FORBIDDEN_PII


async def test_dry_run_pii_allowed_when_whitelisted() -> None:
    """PII 指标被白名单显式列出时放行，且 meta 标注 pii=True 供上层审计分级。"""
    svc = _svc(await _client(whitelist=["gmv"]))
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(pii=True))
    res = await svc.dry_run_query(QueryRequest(metric_code="gmv", date_range=""), client)
    assert res.meta["pii"] is True


async def test_dry_run_dimension_violation() -> None:
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(dims=("region",)))
    with pytest.raises(BusinessError) as exc:
        await svc.dry_run_query(
            QueryRequest(
                metric_code="gmv", date_range="", dimensions=[{"name": "city", "value": "BJ"}]
            ),
            client,
        )
    # 维度未声明于口径 → FORBIDDEN_DIMENSION
    assert exc.value.error_code == ErrorCode.FORBIDDEN_DIMENSION


# ---- 查询降级 ----
async def test_execute_degraded(monkeypatch) -> None:
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric())
    monkeypatch.setattr("app.services.consume.service.settings.olap_url", "")
    with pytest.raises(BusinessError):
        await svc.execute_query(
            QueryRequest(metric_code="gmv", date_range="2026-01~2026-03"), client
        )


# ---- 限流 ----
async def test_rate_limit() -> None:
    svc = _svc(await _client(qps=2))
    client = await svc.authenticate_client("acme:s3cr3t")
    await svc.check_rate_limit(client)  # 1
    await svc.check_rate_limit(client)  # 2
    with pytest.raises(BusinessError):
        await svc.check_rate_limit(client)  # 第 3 次超限


async def test_daily_quota_exhausted() -> None:
    """日配额耗尽后拒绝（RATE_LIMITED + retry_after），避免 QPS 内的长时间刷量。"""
    svc = _svc(await _client(qps=100, daily_quota=2))
    client = await svc.authenticate_client("acme:s3cr3t")
    await svc.check_rate_limit(client)
    await svc.check_rate_limit(client)
    with pytest.raises(BusinessError) as exc:
        await svc.check_rate_limit(client)
    assert exc.value.error_code == ErrorCode.RATE_LIMITED
    assert exc.value.ctx["daily_quota"] == 2


# ---- FR-06 执行引擎复审回归：真实物理口径 SQL 构建 ----
def _metric_with_source() -> Metric:
    m = _metric(dims=("region", "city"))
    m.definition_json = {
        **m.definition_json,
        "source_table": "dws_gmv_daily",
    }
    return m


async def test_build_query_sql_parameterized_no_injection() -> None:
    """SQL 构建必须参数化（杜绝拼串注入），基于 source_table 而非伪表名。"""
    svc = _svc(await _client())
    req = QueryRequest(
        metric_code="gmv",
        date_range="2026-01~2026-03",
        dimensions=[{"name": "city", "value": "BJ' OR 1=1 --"}],
    )
    sql, params = svc._build_query_sql(req, _metric_with_source())
    assert "dws_gmv_daily" in sql  # 使用口径来源表
    assert "unified_metric" not in sql  # 不再是占位伪表
    # 维度值进入参数，绝不拼进 SQL
    assert "OR 1=1" not in sql
    assert "city" in sql and params["dim_0"] == "BJ' OR 1=1 --"


async def test_build_query_sql_falls_back_to_metric_table() -> None:
    """缺省 source_table 时以指标编码推导表名，仍参数化日期与维度。"""
    svc = _svc(await _client())
    req = QueryRequest(metric_code="gmv", date_range="2026-01~2026-03")
    sql, params = svc._build_query_sql(req, _metric())
    assert "dws_metric_gmv" in sql
    assert params["date_from"] == "2026-01"
    assert params["date_to"] == "2026-03"


async def test_build_query_sql_multi_value_dimension() -> None:
    """多值维度走 IN 绑定，仍参数化。"""
    svc = _svc(await _client())
    req = QueryRequest(
        metric_code="gmv",
        date_range="",
        dimensions=[{"name": "region", "value": ["EAST", "WEST"]}],
    )
    sql, params = svc._build_query_sql(req, _metric())
    assert "IN" in sql and ":dim_0" in sql
    assert params["dim_0"] == ["EAST", "WEST"]


async def test_build_query_sql_rejects_unauthorized_dimension() -> None:
    """维度标识符收敛口径声明集：未声明维度拒绝 FORBIDDEN_DIMENSION，防越权列 / 标识符注入。

    覆盖 execute_query 曾经漏检的路径：过去仅 dry-run 校验维度，执行路径直接下发 SQL。
    """
    svc = _svc(await _client())
    req = QueryRequest(
        metric_code="gmv",
        date_range="",
        dimensions=[{"name": "secret_col", "value": "v"}],
    )
    with pytest.raises(BusinessError) as exc:
        svc._build_query_sql(req, _metric(dims=("region",)))
    assert exc.value.error_code == ErrorCode.FORBIDDEN_DIMENSION


async def test_build_query_sql_rejects_metric_code_identifier_injection() -> None:
    """metric_code 经 dws_metric_{code} 拼接为表标识符，必须拒绝非法字符（标识符注入）。

    表名无法参数化，过去的写法 ``f"dws_metric_{req.metric_code}"`` 直接插值用户入参，
    反引号 / 空格 / 分号均可逃逸 `` `...` `` 包裹。现收敛到安全字符集。
    """
    svc = _svc(await _client())
    for malicious in ["gmv` WHERE 1=1; DROP TABLE x; --", "gmv net", "gmv\tselect"]:
        req = QueryRequest(metric_code=malicious, date_range="")
        with pytest.raises(BusinessError) as exc:
            svc._build_query_sql(req, _metric())  # 无 source_table → 走 metric_code 兜底
        assert exc.value.error_code == ErrorCode.INJECTION_DETECTED


async def test_build_query_sql_rejects_source_table_identifier_injection() -> None:
    """source_table（指标 Owner 声明）同样不可信，含非法标识符字符须拒绝。"""
    svc = _svc(await _client())
    m = _metric()
    m.definition_json = {**m.definition_json, "source_table": "dws_gmv`-- a"}
    req = QueryRequest(metric_code="gmv", date_range="")
    with pytest.raises(BusinessError) as exc:
        svc._build_query_sql(req, m)
    assert exc.value.error_code == ErrorCode.INJECTION_DETECTED


async def test_build_query_sql_rejects_malformed_date_range() -> None:
    """date_range 必须为空 / 单值 / 区间，且每段为 YYYY-MM 或 YYYY-MM-DD。"""
    svc = _svc(await _client())
    for bad in ["2026-01~~2026-03", "2026-1~2026-03", "abc", "2026-01~", "~2026-03", "2026-13"]:
        req = QueryRequest(metric_code="gmv", date_range=bad)
        with pytest.raises(BusinessError) as exc:
            svc._build_query_sql(req, _metric())
        assert exc.value.error_code == ErrorCode.VALIDATION_ERROR


async def test_build_query_sql_accepts_valid_date_range() -> None:
    """合法单值与区间日期均可正常参数化（回归：不被误伤）。"""
    svc = _svc(await _client())
    sql_single, params_single = svc._build_query_sql(
        QueryRequest(metric_code="gmv", date_range="2026-01"), _metric()
    )
    assert params_single["date_from"] == params_single["date_to"] == "2026-01"
    sql_range, params_range = svc._build_query_sql(
        QueryRequest(metric_code="gmv", date_range="2026-01-05~2026-03-20"), _metric()
    )
    assert params_range["date_from"] == "2026-01-05" and params_range["date_to"] == "2026-03-20"


async def test_execute_query_rejects_unauthorized_dimension(monkeypatch) -> None:
    """execute_query 必须加权维度校验（过去唯有 dry-run 校验，执行路径越权缺口）。"""
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(dims=("region",)))
    # 绕过 OLAP 不可用 503，直达 SQL 构建层的维度授权校验
    monkeypatch.setattr(
        "app.services.consume.service.settings.olap_url", "http://doris:8030/api/query"
    )
    with pytest.raises(BusinessError) as exc:
        await svc.execute_query(
            QueryRequest(
                metric_code="gmv",
                date_range="2026-01~2026-03",
                dimensions=[{"name": "secret_col", "value": "v"}],
            ),
            client,
        )
    assert exc.value.error_code == ErrorCode.FORBIDDEN_DIMENSION


# ---- 日期语义校验：非法日 ----
async def test_build_query_sql_rejects_invalid_day() -> None:
    """日期段格式合法但语义非法（日越界）仍须拒绝 VALIDATION_ERROR。"""
    svc = _svc(await _client())
    for bad in ["2026-01-32", "2026-01-00"]:
        req = QueryRequest(metric_code="gmv", date_range=bad)
        with pytest.raises(BusinessError) as exc:
            svc._build_query_sql(req, _metric())
        assert exc.value.error_code == ErrorCode.VALIDATION_ERROR


# ---- OLAP 执行器单例 ----
def test_get_olap_executor_creates_singleton(monkeypatch) -> None:
    from app.services.consume.olap_executor import OLAPExecutor

    monkeypatch.setattr("app.services.consume.service._executor", None)
    try:
        e1 = consume_module._get_olap_executor()
        e2 = consume_module._get_olap_executor()
        assert isinstance(e1, OLAPExecutor)
        assert e1 is e2
    finally:
        monkeypatch.setattr("app.services.consume.service._executor", None)


# ---- 消费方短效令牌鉴权 ----
def _token(payload: dict, *, exp_seconds: int | None = None) -> str:
    data = dict(payload)
    if exp_seconds is not None:
        data["exp"] = int(time.time()) + exp_seconds
    return jwt.encode(data, settings.jwt_secret, algorithm=settings.jwt_algorithm)


async def test_authenticate_consume_token_ok() -> None:
    svc = _svc(await _client())
    token = _token({"role": "consume", "sub": "acme"})
    client = await svc.authenticate_consume_token(token)
    assert client.client_id == "acme"


async def test_authenticate_consume_token_expired() -> None:
    svc = _svc(await _client())
    token = _token({"role": "consume", "sub": "acme"}, exp_seconds=-60)
    with pytest.raises(BusinessError) as exc:
        await svc.authenticate_consume_token(token)
    assert exc.value.error_code == ErrorCode.AUTH_APIKEY_INVALID


async def test_authenticate_consume_token_invalid() -> None:
    svc = _svc(await _client())
    with pytest.raises(BusinessError):
        await svc.authenticate_consume_token("not.a.jwt")


async def test_authenticate_consume_token_wrong_role() -> None:
    svc = _svc(await _client())
    token = _token({"role": "admin", "sub": "acme"})
    with pytest.raises(BusinessError):
        await svc.authenticate_consume_token(token)


async def test_authenticate_consume_token_revoked() -> None:
    svc = _svc(await _client(status=ApiClientStatus.REVOKED))
    token = _token({"role": "consume", "sub": "acme"})
    with pytest.raises(BusinessError):
        await svc.authenticate_consume_token(token)


async def test_authenticate_consume_token_client_missing() -> None:
    svc = _svc(await _client())
    svc._clients.get_by_client_id = AsyncMock(return_value=None)
    token = _token({"role": "consume", "sub": "ghost"})
    with pytest.raises(BusinessError):
        await svc.authenticate_consume_token(token)


# ---- dry-run / execute：指标不存在与状态闸门 ----
async def test_dry_run_metric_not_found() -> None:
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.dry_run_query(QueryRequest(metric_code="missing", date_range=""), client)


async def test_execute_metric_not_found() -> None:
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.execute_query(QueryRequest(metric_code="missing", date_range=""), client)


async def test_execute_draft_metric_forbidden() -> None:
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(status="DRAFT"))
    with pytest.raises(BusinessError) as exc:
        await svc.execute_query(QueryRequest(metric_code="gmv", date_range=""), client)
    assert exc.value.error_code == ErrorCode.FORBIDDEN_METRIC


async def test_execute_deprecated_metric_forbidden() -> None:
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(status="DEPRECATED"))
    with pytest.raises(BusinessError) as exc:
        await svc.execute_query(QueryRequest(metric_code="gmv", date_range=""), client)
    assert exc.value.error_code == ErrorCode.FORBIDDEN_DEPRECATED


# ---- execute：OLAP 执行器成功 / 异常 ----
def _set_olap_ready(monkeypatch, executor: MagicMock) -> None:
    monkeypatch.setattr(
        "app.services.consume.service.settings.olap_url", "http://doris:8030/api/query"
    )
    monkeypatch.setattr("app.services.consume.service._get_olap_executor", lambda: executor)


async def test_execute_query_success(monkeypatch) -> None:
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric())
    result = SimpleNamespace(rows=[{"region": "EAST"}], total=1, elapsed_ms=3.5, from_cache=False)
    executor = MagicMock()
    executor.execute = AsyncMock(return_value=result)
    _set_olap_ready(monkeypatch, executor)
    res = await svc.execute_query(
        QueryRequest(metric_code="gmv", date_range="2026-01~2026-03"), client
    )
    assert res.degraded is False
    assert res.data["rows"] == [{"region": "EAST"}]
    assert res.data["total"] == 1
    assert res.data["elapsed_ms"] == 3.5
    assert res.data["from_cache"] is False
    assert res.meta["domain"] == "sales"
    executor.execute.assert_awaited_once()


async def test_execute_query_executor_exception(monkeypatch) -> None:
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric())
    executor = MagicMock()
    executor.execute = AsyncMock(side_effect=RuntimeError("boom"))
    _set_olap_ready(monkeypatch, executor)
    with pytest.raises(BusinessError) as exc:
        await svc.execute_query(QueryRequest(metric_code="gmv", date_range=""), client)
    assert exc.value.error_code == ErrorCode.DEPENDENCY_DEGRADED_ENGINE


async def test_execute_query_reraises_business_error(monkeypatch) -> None:
    """OLAP 执行器抛出的业务降级错误原样透传。"""
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric())
    executor = MagicMock()
    executor.execute = AsyncMock(
        side_effect=BusinessError("引擎降级", error_code=ErrorCode.DEPENDENCY_DEGRADED_ENGINE)
    )
    _set_olap_ready(monkeypatch, executor)
    with pytest.raises(BusinessError) as exc:
        await svc.execute_query(QueryRequest(metric_code="gmv", date_range=""), client)
    assert exc.value.error_code == ErrorCode.DEPENDENCY_DEGRADED_ENGINE


# ---- 快照 WORM ----
def _snap() -> MetricValueSnapshot:
    return MetricValueSnapshot(
        id=1, metric_code="gmv", version=2, dims={"region": "EAST"}, date_range="2026-01",
        value_json={"total": 100}, quality_flag="GOOD", generated_at=datetime.now(UTC),
        generated_by=SnapshotGeneratedBy.MATERIALIZE,
    )


async def test_save_snapshot() -> None:
    svc = _svc(await _client())

    def _persist_snap(s: MetricValueSnapshot) -> MetricValueSnapshot:
        s.id = 1
        return s

    svc._snapshots.create = AsyncMock(side_effect=_persist_snap)
    resp = await svc.save_snapshot(
        "gmv", 2, {"region": "EAST"}, "2026-01", {"total": 100},
        "GOOD", datetime.now(UTC), SnapshotGeneratedBy.QUERY,
    )
    assert resp.metric_code == "gmv"
    assert resp.version == 2
    assert resp.value_json == {"total": 100}
    assert resp.quality_flag == "GOOD"
    assert resp.generated_by == SnapshotGeneratedBy.QUERY
    svc._snapshots.create.assert_awaited_once()


async def test_list_snapshots() -> None:
    svc = _svc(await _client())
    svc._snapshots.list_by_metric = AsyncMock(return_value=[_snap()])
    out = await svc.list_snapshots("gmv", 10, 0)
    assert len(out) == 1
    assert out[0].generated_by == SnapshotGeneratedBy.MATERIALIZE
    assert out[0].date_range == "2026-01"
    svc._snapshots.list_by_metric.assert_awaited_once_with("gmv", 10, 0)


# ---- 收藏 ----
async def test_add_favorite_new() -> None:
    svc = _svc(await _client())
    svc._fav.list_pinned = AsyncMock(return_value=[])
    svc._fav.upsert_pinned = AsyncMock()
    resp = await svc.add_favorite(1, "gmv")
    assert resp.pinned is True
    svc._fav.upsert_pinned.assert_awaited_once_with(1, ["gmv"])


async def test_add_favorite_already_pinned() -> None:
    svc = _svc(await _client())
    svc._fav.list_pinned = AsyncMock(return_value=["gmv"])
    svc._fav.upsert_pinned = AsyncMock()
    resp = await svc.add_favorite(1, "gmv")
    assert resp.pinned is True
    svc._fav.upsert_pinned.assert_not_awaited()


async def test_remove_favorite() -> None:
    svc = _svc(await _client())
    svc._fav.list_pinned = AsyncMock(return_value=["gmv", "revenue"])
    svc._fav.upsert_pinned = AsyncMock()
    resp = await svc.remove_favorite(1, "gmv")
    assert resp.pinned is False
    svc._fav.upsert_pinned.assert_awaited_once_with(1, ["revenue"])


async def test_list_favorites() -> None:
    svc = _svc(await _client())
    svc._fav.list_pinned = AsyncMock(return_value=["gmv", "revenue"])
    assert await svc.list_favorites(1) == ["gmv", "revenue"]


# ---- 版本消费方确认回调 ----
async def test_confirm_version_success() -> None:
    svc = _svc(await _client())
    mv = MetricVersion(id=1, status="PENDING_CONFIRMATION")
    svc._get_version = AsyncMock(return_value=mv)
    svc._db.flush = AsyncMock()
    await svc.confirm_version(1, 7)
    assert mv.status == "PUBLISHED"
    svc._db.flush.assert_awaited_once()


async def test_confirm_version_not_found() -> None:
    svc = _svc(await _client())
    svc._get_version = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.confirm_version(99, 7)


async def test_confirm_version_conflict() -> None:
    svc = _svc(await _client())
    svc._get_version = AsyncMock(return_value=MetricVersion(id=1, status="PUBLISHED"))
    with pytest.raises(ConflictError):
        await svc.confirm_version(1, 7)


async def test_reject_version_success() -> None:
    svc = _svc(await _client())
    mv = MetricVersion(id=1, status="PENDING_CONFIRMATION")
    svc._get_version = AsyncMock(return_value=mv)
    svc._db.flush = AsyncMock()
    await svc.reject_version(1, 7, "口径有误")
    assert mv.status == "ARCHIVED"
    svc._db.flush.assert_awaited_once()


async def test_reject_version_not_found() -> None:
    svc = _svc(await _client())
    svc._get_version = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.reject_version(99, 7, None)


async def test_reject_version_conflict() -> None:
    svc = _svc(await _client())
    svc._get_version = AsyncMock(return_value=MetricVersion(id=1, status="ARCHIVED"))
    with pytest.raises(ConflictError):
        await svc.reject_version(1, 7, "no")


# ---- helpers 直接覆盖（DB 以 MagicMock 隔离）----
async def test_get_metric_builds_query() -> None:
    db = MagicMock()
    result = MagicMock()
    metric = _metric()
    result.scalar_one_or_none = MagicMock(return_value=metric)
    db.execute = AsyncMock(return_value=result)
    svc = ConsumeService(db)
    out = await svc._get_metric("gmv")
    assert out is metric
    assert "metric_code" in str(db.execute.await_args.args[0])


async def test_get_version_builds_query() -> None:
    db = MagicMock()
    result = MagicMock()
    mv = MetricVersion(id=3, status="PENDING_CONFIRMATION")
    result.scalar_one_or_none = MagicMock(return_value=mv)
    db.execute = AsyncMock(return_value=result)
    svc = ConsumeService(db)
    out = await svc._get_version(3)
    assert out is mv
    assert "metric_version" in str(db.execute.await_args.args[0])
