"""consume 服务单元测试（TD §12.6 / FR-12,13）。

聚焦纯逻辑：接入方鉴权、dry-run 口径校验、查询 OLAP 降级、限流闸门。
DB / 仓库以 MagicMock 隔离（对齐 DEV_GUIDE §8b 单元标准），不连真实依赖。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.error_codes import ErrorCode
from app.core.exceptions import BusinessError
from app.core.security import hash_password
from app.models.consume import ApiClient, ApiClientStatus
from app.models.metric import Metric
from app.services.consume.rate_limiter import get_rate_limiter
from app.services.consume.schemas import QueryRequest
from app.services.consume.service import ConsumeService


def _client(
    secret: str = "s3cr3t",
    whitelist=None,
    qps: int = 2,
    status=ApiClientStatus.ACTIVE,
    scope_domain: str | None = None,
    daily_quota: int = 100,
) -> ApiClient:
    c = ApiClient()
    c.client_id = "acme"
    c.client_secret_ref = hash_password(secret)
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
    svc = _svc(_client())
    client = await svc.authenticate_client("acme:s3cr3t")
    assert client.client_id == "acme"


async def test_authenticate_bad_secret() -> None:
    svc = _svc(_client())
    with pytest.raises(BusinessError):
        await svc.authenticate_client("acme:wrong")


async def test_authenticate_revoked() -> None:
    svc = _svc(_client(status=ApiClientStatus.REVOKED))
    with pytest.raises(BusinessError):
        await svc.authenticate_client("acme:s3cr3t")


async def test_authenticate_bad_format() -> None:
    svc = _svc(_client())
    with pytest.raises(BusinessError):
        await svc.authenticate_client("acme_no_colon")


# ---- dry-run ----
async def test_dry_run_ok() -> None:
    svc = _svc(_client())
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
    svc = _svc(_client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(status="DEPRECATED"))
    with pytest.raises(BusinessError):
        await svc.dry_run_query(QueryRequest(metric_code="gmv", date_range=""), client)


async def test_dry_run_scope_denied() -> None:
    svc = _svc(_client(whitelist=["other_metric"]))
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric())
    with pytest.raises(BusinessError):
        await svc.dry_run_query(QueryRequest(metric_code="gmv", date_range=""), client)


async def test_dry_run_cross_domain_denied() -> None:
    """scope_domain 限定域外指标必须拒绝（FORBIDDEN_DOMAIN，fail-closed 越权闸门）。"""
    svc = _svc(_client(scope_domain="finance"))
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(domain="sales"))
    with pytest.raises(BusinessError) as exc:
        await svc.dry_run_query(QueryRequest(metric_code="gmv", date_range=""), client)
    assert exc.value.error_code == ErrorCode.FORBIDDEN_DOMAIN


async def test_dry_run_pii_requires_explicit_whitelist() -> None:
    """PII 指标在"域内全量"授权下不可隐式访问（FORBIDDEN_PII）。"""
    svc = _svc(_client(whitelist=None))
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(pii=True))
    with pytest.raises(BusinessError) as exc:
        await svc.dry_run_query(QueryRequest(metric_code="gmv", date_range=""), client)
    assert exc.value.error_code == ErrorCode.FORBIDDEN_PII


async def test_dry_run_pii_allowed_when_whitelisted() -> None:
    """PII 指标被白名单显式列出时放行，且 meta 标注 pii=True 供上层审计分级。"""
    svc = _svc(_client(whitelist=["gmv"]))
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(pii=True))
    res = await svc.dry_run_query(QueryRequest(metric_code="gmv", date_range=""), client)
    assert res.meta["pii"] is True


async def test_dry_run_dimension_violation() -> None:
    svc = _svc(_client())
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
    svc = _svc(_client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric())
    monkeypatch.setattr("app.services.consume.service.settings.olap_url", "")
    with pytest.raises(BusinessError):
        await svc.execute_query(
            QueryRequest(metric_code="gmv", date_range="2026-01~2026-03"), client
        )


# ---- 限流 ----
async def test_rate_limit() -> None:
    svc = _svc(_client(qps=2))
    client = await svc.authenticate_client("acme:s3cr3t")
    await svc.check_rate_limit(client)  # 1
    await svc.check_rate_limit(client)  # 2
    with pytest.raises(BusinessError):
        await svc.check_rate_limit(client)  # 第 3 次超限


async def test_daily_quota_exhausted() -> None:
    """日配额耗尽后拒绝（RATE_LIMITED + retry_after），避免 QPS 内的长时间刷量。"""
    svc = _svc(_client(qps=100, daily_quota=2))
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


def test_build_query_sql_parameterized_no_injection() -> None:
    """SQL 构建必须参数化（杜绝拼串注入），基于 source_table 而非伪表名。"""
    svc = _svc(_client())
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


def test_build_query_sql_falls_back_to_metric_table() -> None:
    """缺省 source_table 时以指标编码推导表名，仍参数化日期与维度。"""
    svc = _svc(_client())
    req = QueryRequest(metric_code="gmv", date_range="2026-01~2026-03")
    sql, params = svc._build_query_sql(req, _metric())
    assert "dws_metric_gmv" in sql
    assert params["date_from"] == "2026-01"
    assert params["date_to"] == "2026-03"


def test_build_query_sql_multi_value_dimension() -> None:
    """多值维度走 IN 绑定，仍参数化。"""
    svc = _svc(_client())
    req = QueryRequest(
        metric_code="gmv",
        date_range="",
        dimensions=[{"name": "region", "value": ["EAST", "WEST"]}],
    )
    sql, params = svc._build_query_sql(req, _metric())
    assert "IN" in sql and ":dim_0" in sql
    assert params["dim_0"] == ["EAST", "WEST"]


def test_build_query_sql_rejects_unauthorized_dimension() -> None:
    """维度标识符收敛口径声明集：未声明维度拒绝 FORBIDDEN_DIMENSION，防越权列 / 标识符注入。

    覆盖 execute_query 曾经漏检的路径：过去仅 dry-run 校验维度，执行路径直接下发 SQL。
    """
    svc = _svc(_client())
    req = QueryRequest(
        metric_code="gmv",
        date_range="",
        dimensions=[{"name": "secret_col", "value": "v"}],
    )
    with pytest.raises(BusinessError) as exc:
        svc._build_query_sql(req, _metric(dims=("region",)))
    assert exc.value.error_code == ErrorCode.FORBIDDEN_DIMENSION


async def test_execute_query_rejects_unauthorized_dimension(monkeypatch) -> None:
    """execute_query 必须加权维度校验（过去唯有 dry-run 校验，执行路径越权缺口）。"""
    svc = _svc(_client())
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
