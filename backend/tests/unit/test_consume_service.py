"""consume 服务单元测试（TD §12.6 / FR-12,13）。

聚焦纯逻辑：接入方鉴权、dry-run 口径校验、查询 OLAP 降级、限流闸门。
DB / 仓库以 MagicMock 隔离（对齐 DEV_GUIDE §8b 单元标准），不连真实依赖。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
    FavoriteAssetType,
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
    name: str | None = None,
    pii: bool = False,
    pii_flag: bool = False,
    compliance_reviewed: bool = False,
) -> Metric:
    m = Metric()
    m.metric_code = code
    m.status = status
    m.owner_org = 1
    m.domain = domain
    m.name = name or code
    m.pii_flag = pii_flag
    m.compliance_reviewed = compliance_reviewed
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
    # 默认 DB 查询（覆盖 _get_bound_dimensions 等）返回空集，避免裸 MagicMock 迭代挂死；
    # 需要真实行集的测试自行覆盖 svc._db.execute。
    defn_res = MagicMock()
    defn_res.scalars.return_value.all.return_value = []
    svc._db.execute = AsyncMock(return_value=defn_res)
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
    """PII 指标在"域内全量"授权下不可隐式访问（FORBIDDEN_PII）。

    修复后：PII 合规闸门独立于白名单——即使指标被显式白名单授权，
    未通过合规复核（compliance_reviewed=False）时仍阻断消费。
    """
    svc = _svc(await _client(whitelist=None))
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(pii=True, pii_flag=True))
    with pytest.raises(BusinessError) as exc:
        await svc.dry_run_query(QueryRequest(metric_code="gmv", date_range=""), client)
    assert exc.value.error_code == ErrorCode.FORBIDDEN_PII


async def test_dry_run_pii_allowed_when_whitelisted_and_reviewed() -> None:
    """PII 指标需同时满足：白名单显式授权 + 合规复核通过，才可消费。

    修复后：合规闸门（compliance_reviewed=True）独立于白名单。
    仅白名单授权而未复核 → FORBIDDEN_PII（而非旧语义下的放行）。
    """
    svc = _svc(await _client(whitelist=["gmv"]))
    client = await svc.authenticate_client("acme:s3cr3t")
    # 已复核的 PII 指标，白名单授权下放行
    svc._get_metric = AsyncMock(
        return_value=_metric(pii=True, pii_flag=True, compliance_reviewed=True)
    )
    res = await svc.dry_run_query(QueryRequest(metric_code="gmv", date_range=""), client)
    assert res.meta["pii"] is True


async def test_dry_run_pii_whitelisted_but_not_reviewed_blocked() -> None:
    """白名单授权不足以绕过 PII 合规闸门——未复核时阻断（FORBIDDEN_PII）。

    这是新安全语义的核心体现：合规闸门是独立的安全基线，不可被白名单绕过。
    """
    svc = _svc(await _client(whitelist=["gmv"]))
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(pii=True, pii_flag=True))
    with pytest.raises(BusinessError) as exc:
        await svc.dry_run_query(QueryRequest(metric_code="gmv", date_range=""), client)
    assert exc.value.error_code == ErrorCode.FORBIDDEN_PII


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


async def test_dry_run_allows_dimension_from_binding_table() -> None:
    """跨服务打通（方案③）：维度不在 definition_json.dimensions，但已通过 metric_dimension
    绑定表绑定该指标 → 消费校验放行（绑定即生效，不再是信息孤岛）。"""
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    metric = _metric(dims=("region",))  # 口径未声明 city
    metric.id = 1
    svc._get_metric = AsyncMock(return_value=metric)
    # 模拟 metric_dimension 绑定表查询：该指标已绑定 city
    bound_res = MagicMock()
    bound_res.scalars.return_value.all.return_value = ["city"]
    svc._db.execute = AsyncMock(return_value=bound_res)

    res = await svc.dry_run_query(
        QueryRequest(
            metric_code="gmv", date_range="", dimensions=[{"name": "city", "value": "BJ"}]
        ),
        client,
    )
    assert res.status == "ok"
    # 真实物理口径 SQL 含 city 维度过滤（绑定来源被纳入允许集）
    assert "city" in res.execution_plan["dialect_sql"]


async def test_dry_run_rejects_dimension_neither_declared_nor_bound() -> None:
    """既未在口径声明、也未绑定表的维度 → 仍拒绝 FORBIDDEN_DIMENSION（向后兼容）。"""
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    metric = _metric(dims=("region",))  # 口径只有 region
    metric.id = 1
    svc._get_metric = AsyncMock(return_value=metric)
    # 绑定表为空（默认 _svc mock 返回 []），secret_col 既未声明也未绑定
    with pytest.raises(BusinessError) as exc:
        await svc.dry_run_query(
            QueryRequest(
                metric_code="gmv", date_range="", dimensions=[{"name": "secret_col", "value": "v"}]
            ),
            client,
        )
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


async def test_build_query_sql_mount_table_authority() -> None:
    """OneData 挂载层权威：mount_table 优先于 definition_json 冗余 source_table。"""
    svc = _svc(await _client())
    req = QueryRequest(metric_code="gmv", date_range="2026-01~2026-03")
    m = _metric_with_source()
    m.definition_json = {**m.definition_json, "source_table": "dws_gmv_old"}
    sql, _ = svc._build_query_sql(req, m, mount_table="dws_gmv_new")
    assert "dws_gmv_new" in sql
    assert "dws_gmv_old" not in sql


async def test_resolve_mount_table_returns_mount_source() -> None:
    """_resolve_mount_table 查挂载实体返回 source_table（挂载独立更新后消费 SQL 基于最新物理表）。"""
    svc = _svc(await _client())
    m = _metric_with_source()
    m.id = 7
    mount = MagicMock()
    mount.source_table = "dwd.sales_detail"
    with patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls:
        mrepo_cls.return_value.get_by_metric = AsyncMock(return_value=mount)
        table = await svc._resolve_mount_table(m)
    assert table == "dwd.sales_detail"
    mrepo_cls.return_value.get_by_metric.assert_awaited_once_with(7)


async def test_resolve_mount_table_falls_back_when_mount_query_fails() -> None:
    """挂载查询异常/未挂载 → 返回 None（回退 definition_json，不阻断消费 SQL）。"""
    svc = _svc(await _client())
    m = _metric_with_source()
    m.id = 7
    with patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls:
        mrepo_cls.return_value.get_by_metric = AsyncMock(return_value=None)
        table = await svc._resolve_mount_table(m)
    assert table is None


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
        id=1,
        metric_code="gmv",
        version=2,
        dims={"region": "EAST"},
        date_range="2026-01",
        value_json={"total": 100},
        quality_flag="GOOD",
        generated_at=datetime.now(UTC),
        generated_by=SnapshotGeneratedBy.MATERIALIZE,
    )


async def test_save_snapshot() -> None:
    svc = _svc(await _client())

    def _persist_snap(s: MetricValueSnapshot) -> MetricValueSnapshot:
        s.id = 1
        return s

    svc._snapshots.create = AsyncMock(side_effect=_persist_snap)
    resp = await svc.save_snapshot(
        "gmv",
        2,
        {"region": "EAST"},
        "2026-01",
        {"total": 100},
        "GOOD",
        datetime.now(UTC),
        SnapshotGeneratedBy.QUERY,
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


# ---- 收藏（通用多资产，C 层）----
def _fav_row(asset_type: str, asset_id: str, created: datetime | None = None) -> SimpleNamespace:
    """构造 Favorite 行的简化对象（service 层仅读 asset_type/asset_id/created_at）。"""
    return SimpleNamespace(
        asset_type=SimpleNamespace(value=asset_type),
        asset_id=asset_id,
        created_at=created or datetime(2026, 8, 15, tzinfo=UTC),
    )


def _exec_scalars(rows) -> MagicMock:
    """构造 db.execute(...).scalars().all() 返回 rows 的 mock。"""
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = rows
    result.scalars.return_value = scalars
    return result


async def test_add_favorite_metric() -> None:
    svc = _svc(await _client())
    svc._ensure_asset = AsyncMock()
    svc._fav.get = AsyncMock(return_value=None)
    svc._fav.add = AsyncMock()
    resp = await svc.add_favorite(1, FavoriteAssetType.METRIC, "gmv")
    assert resp.pinned is True
    assert resp.asset_type == "METRIC"
    assert resp.asset_id == "gmv"
    svc._ensure_asset.assert_awaited_once_with(FavoriteAssetType.METRIC, "gmv")
    svc._fav.add.assert_awaited_once_with(1, "METRIC", "gmv")


async def test_add_favorite_already_pinned() -> None:
    svc = _svc(await _client())
    svc._ensure_asset = AsyncMock()
    svc._fav.get = AsyncMock(return_value=_fav_row("METRIC", "gmv"))
    svc._fav.add = AsyncMock()
    resp = await svc.add_favorite(1, FavoriteAssetType.METRIC, "gmv")
    assert resp.pinned is True
    svc._fav.add.assert_not_awaited()


async def test_add_favorite_asset_not_found() -> None:
    svc = _svc(await _client())
    svc._ensure_asset = AsyncMock(side_effect=NotFoundError("资产不存在: ghost"))
    svc._fav.add = AsyncMock()
    with pytest.raises(NotFoundError):
        await svc.add_favorite(1, FavoriteAssetType.METRIC, "ghost")
    svc._fav.add.assert_not_awaited()


async def test_remove_favorite() -> None:
    svc = _svc(await _client())
    svc._fav.remove = AsyncMock(return_value=True)
    resp = await svc.remove_favorite(1, FavoriteAssetType.METRIC, "gmv")
    assert resp.pinned is False
    assert resp.asset_id == "gmv"
    svc._fav.remove.assert_awaited_once_with(1, "METRIC", "gmv")


async def test_list_favorites_returns_generic_structure() -> None:
    svc = _svc(await _client())
    svc._fav.list = AsyncMock(
        return_value=[_fav_row("METRIC", "gmv"), _fav_row("TABLE", "dw.sales")]
    )
    out = await svc.list_favorites(1)
    assert out == [
        {"asset_type": "METRIC", "asset_id": "gmv"},
        {"asset_type": "TABLE", "asset_id": "dw.sales"},
    ]


async def test_list_favorite_details_multi_asset() -> None:
    """多资产聚合：按收藏时间倒序，带详情 + created_at + dead 标记。"""
    svc = _svc(await _client())
    # repo.list 已按收藏时间倒序：TABLE(8-15) 在 METRIC(8-14) 之前
    favs = [
        _fav_row("TABLE", "dw.sales", datetime(2026, 8, 15, tzinfo=UTC)),
        _fav_row("METRIC", "gmv", datetime(2026, 8, 14, tzinfo=UTC)),
    ]
    svc._fav.list = AsyncMock(return_value=favs)
    svc._load_asset_details = AsyncMock(
        return_value={
            ("METRIC", "gmv"): {
                "name": "成交总额", "description": "订单总额", "domain": "sales",
                "status": "PUBLISHED", "tier": "T1", "is_pii": False,
            },
            ("TABLE", "dw.sales"): {
                "name": "dw.sales", "description": "销售明细", "domain": "sales",
                "status": "PUBLISHED", "tier": None, "is_pii": False,
            },
        }
    )
    out = await svc.list_favorite_details(1)
    assert out[0]["asset_type"] == "TABLE"  # 最近收藏在前
    assert out[0]["asset_id"] == "dw.sales"
    assert out[1]["name"] == "成交总额"
    assert out[1]["tier"] == "T1"
    assert out[1]["dead"] is False
    assert "created_at" in out[0]


async def test_list_favorite_details_soft_deleted_marks_dead() -> None:
    """软删除 bug 修复：资产查不到（含 deleted_at 过滤）→ 保留条目 + dead=True + status UNKNOWN。"""
    svc = _svc(await _client())
    svc._fav.list = AsyncMock(return_value=[_fav_row("METRIC", "gone")])
    svc._load_asset_details = AsyncMock(return_value={})  # 查不到 → 软删除/已不存在
    out = await svc.list_favorite_details(1)
    assert out[0]["asset_id"] == "gone"
    assert out[0]["dead"] is True
    assert out[0]["status"] == "UNKNOWN"


async def test_list_favorite_details_empty() -> None:
    svc = _svc(await _client())
    svc._fav.list = AsyncMock(return_value=[])
    assert await svc.list_favorite_details(1) == []


async def test_ensure_asset_metric_exists() -> None:
    svc = _svc(await _client())
    exists = SimpleNamespace(scalar_one_or_none=lambda: _metric(code="gmv"))
    svc._db.execute = AsyncMock(return_value=exists)
    await svc._ensure_asset(FavoriteAssetType.METRIC, "gmv")  # 不抛异常


async def test_ensure_asset_missing_raises() -> None:
    svc = _svc(await _client())
    svc._db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    with pytest.raises(NotFoundError):
        await svc._ensure_asset(FavoriteAssetType.METRIC, "ghost")


async def test_load_asset_details_filters_soft_deleted() -> None:
    """_load_asset_details 查询带 deleted_at 过滤：软删除资产不进入 lookup → 上层标记 dead。"""
    svc = _svc(await _client())
    svc._db.execute = AsyncMock(return_value=_exec_scalars([]))  # 无结果（模拟软删除被过滤）
    lookup = await svc._load_asset_details({"METRIC": ["gmv", "gone"]})
    assert ("METRIC", "gmv") not in lookup
    assert ("METRIC", "gone") not in lookup


async def test_load_asset_details_table_joins_source_domain() -> None:
    """TABLE 详情关联数据源取域（一次批量查询消除 N+1）。"""
    svc = _svc(await _client())
    table = SimpleNamespace(entity_name="dw.sales", description="销售", source_id="mysql-1",
                            sensitivity_level="CONFIDENTIAL")
    ds = SimpleNamespace(source_id="mysql-1", domain="sales")
    svc._db.execute = AsyncMock(
        side_effect=[
            _exec_scalars([table]),  # DBCatalog 查询
            _exec_scalars([ds]),  # DataSource 查询
        ]
    )
    lookup = await svc._load_asset_details({"TABLE": ["dw.sales"]})
    detail = lookup[("TABLE", "dw.sales")]
    assert detail["name"] == "dw.sales"
    assert detail["status"] == "CONFIDENTIAL"
    assert detail["domain"] == "sales"


# ---- 版本消费方确认回调 ----
async def test_confirm_version_success() -> None:
    svc = _svc(await _client())
    mv = MetricVersion(id=1, metric_id=5, version=2, status="PENDING_CONFIRMATION")
    svc._get_version = AsyncMock(return_value=mv)
    svc._get_my_confirmation = AsyncMock(return_value=MagicMock(id=9, consumer_id=7))
    svc._get_metric_by_id = AsyncMock(return_value=MagicMock(metric_code="sales_gmv_daily"))
    svc._db.flush = AsyncMock()
    with patch("app.services.semantic.service.MetricService") as ms:
        instance = ms.return_value
        instance.confirm_version = AsyncMock()
        await svc.confirm_version(1, 7)
    # 委托语义模块完整转正（主表口径同步 + 版本递增 + 血缘 + 通知 + 审计）
    instance.confirm_version.assert_awaited_once_with("sales_gmv_daily", 2, consumer_id=7)


async def test_confirm_version_not_confirmation_consumer_forbidden() -> None:
    """非该版本的确认消费方确认被拒（IDOR 越权防护——此前 user_id 未使用）。"""
    svc = _svc(await _client())
    mv = MetricVersion(id=1, metric_id=5, version=2, status="PENDING_CONFIRMATION")
    svc._get_version = AsyncMock(return_value=mv)
    svc._get_my_confirmation = AsyncMock(return_value=None)  # 调用者不是确认消费方
    with pytest.raises(ConflictError):
        await svc.confirm_version(1, 7)


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
    mv = MetricVersion(id=1, metric_id=5, version=2, status="PENDING_CONFIRMATION")
    svc._get_version = AsyncMock(return_value=mv)
    svc._get_my_confirmation = AsyncMock(return_value=MagicMock(id=9, consumer_id=7))
    svc._get_metric_by_id = AsyncMock(return_value=MagicMock(metric_code="sales_gmv_daily"))
    svc._db.flush = AsyncMock()
    with patch("app.services.semantic.service.MetricService") as ms:
        instance = ms.return_value
        instance.reject_version = AsyncMock()
        await svc.reject_version(1, 7, "口径有误")
    # 委托语义模块完整拒绝（版本 CANCELLED + 终结该版本全部确认记录 + 审计）
    instance.reject_version.assert_awaited_once_with(
        "sales_gmv_daily", 2, "口径有误", consumer_id=7
    )


async def test_reject_version_not_confirmation_consumer_forbidden() -> None:
    """非该版本的确认消费方拒绝被拒（IDOR 越权防护）。"""
    svc = _svc(await _client())
    mv = MetricVersion(id=1, metric_id=5, version=2, status="PENDING_CONFIRMATION")
    svc._get_version = AsyncMock(return_value=mv)
    svc._get_my_confirmation = AsyncMock(return_value=None)
    with pytest.raises(ConflictError):
        await svc.reject_version(1, 7, "no")


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


# ---- OLAP 参数化绑定修复（P1）：SQLAlchemy :name → Doris ${name} ----
def test_to_doris_sql_scalar_params() -> None:
    """字符串标量以 '${name}' 包裹进入 variables；数值以 ${name}。"""
    from app.services.consume.olap_executor import _to_doris_sql

    sql, variables = _to_doris_sql(
        "SELECT `city` FROM `dws_gmv_daily` WHERE metric_code = :metric_code AND dt >= :date_from",
        {"metric_code": "gmv", "date_from": "2026-01"},
    )
    assert ":metric_code" not in sql
    assert ":date_from" not in sql
    assert "'${metric_code}'" in sql and "'${date_from}'" in sql
    assert variables == {"metric_code": "gmv", "date_from": "2026-01"}


def test_to_doris_sql_scalar_string_injection_escaped() -> None:
    """字符串标量单引号翻倍，防 Doris 文本替换注入。"""
    from app.services.consume.olap_executor import _to_doris_sql

    sql, variables = _to_doris_sql(
        "SELECT * FROM `t` WHERE metric_code = :metric_code",
        {"metric_code": "BJ' OR 1=1 --"},
    )
    assert sql == "SELECT * FROM `t` WHERE metric_code = '${metric_code}'"
    # variables 值单引号翻倍：文本替换后落在引号内，不可逃逸
    assert variables["metric_code"] == "BJ'' OR 1=1 --"


def test_to_doris_sql_list_inline_expansion() -> None:
    """IN 列表参数原地展开为内联字面量，单引号转义防注入。"""
    from app.services.consume.olap_executor import _to_doris_sql

    sql, variables = _to_doris_sql(
        "SELECT * FROM `t` WHERE region IN :dim_0",
        {"dim_0": ["EAST", "WEST"]},
    )
    assert sql == "SELECT * FROM `t` WHERE region IN ('EAST', 'WEST')"
    assert variables == {}


def test_to_doris_sql_list_injection_escaped() -> None:
    """列表值含单引号 → 翻倍转义，防注入。"""
    from app.services.consume.olap_executor import _to_doris_sql

    sql, _ = _to_doris_sql(
        "SELECT * FROM `t` WHERE name IN :dim_0",
        {"dim_0": ["O'Reilly", "x; DROP TABLE t; --"]},
    )
    # 单引号翻倍：O'Reilly → O''Reilly
    assert "O''Reilly" in sql
    # 分号注入值被完整包裹在字符串字面量内（成对引号闭合），无法逃逸
    assert sql == "SELECT * FROM `t` WHERE name IN ('O''Reilly', 'x; DROP TABLE t; --')"
    # 引号必须成对闭合，否则可逃逸
    assert sql.count("'") % 2 == 0


def test_to_doris_sql_numeric_and_none() -> None:
    """数字不加引号、None 转 NULL。"""
    from app.services.consume.olap_executor import _to_doris_sql

    sql, variables = _to_doris_sql(
        "SELECT * FROM `t` WHERE id IN :ids AND flag = :flag",
        {"ids": [1, 2, None], "flag": True},
    )
    assert "IN (1, 2, NULL)" in sql
    assert variables == {"flag": True}


def test_to_doris_sql_unknown_placeholder_preserved() -> None:
    """params 中不存在的占位符保持原样（交由 Doris 报错暴露）。"""
    from app.services.consume.olap_executor import _to_doris_sql

    sql, variables = _to_doris_sql("SELECT * FROM t WHERE a = :missing", {"b": 1})
    assert ":missing" in sql
    assert variables == {}


# ---- 投影列收敛（P1）：SELECT * → 口径声明列 + LIMIT 硬上限 ----
def test_build_query_sql_select_projection_columns() -> None:
    """口径声明 measures 时 SELECT 收敛到维度+度量列，不再 SELECT *。"""
    svc = _svc(MagicMock())
    m = _metric(dims=("region", "city"))
    m.definition_json = {**m.definition_json, "measures": ["gmv", "order_cnt"]}
    req = QueryRequest(metric_code="gmv", date_range="")
    sql, params = svc._build_query_sql(req, m)
    assert "SELECT `region`, `city`, `gmv`, `order_cnt`" in sql
    assert "SELECT *" not in sql
    assert "LIMIT :__max_rows" in sql
    assert params["__max_rows"] == 1000


def test_build_query_sql_fallback_star_still_limited() -> None:
    """口径未声明任何投影列时退化为 *，但仍强制 LIMIT 防全表拖回。"""
    svc = _svc(MagicMock())
    m = _metric(dims=())
    m.definition_json = {**m.definition_json, "dimensions": []}
    req = QueryRequest(metric_code="gmv", date_range="")
    sql, params = svc._build_query_sql(req, m)
    assert sql.startswith("SELECT *")
    assert "LIMIT :__max_rows" in sql
    assert params["__max_rows"] == 1000


def test_build_query_sql_rejects_illegal_projection_column() -> None:
    """投影列非合法标识符 → INJECTION_DETECTED。"""
    svc = _svc(MagicMock())
    m = _metric(dims=("region",))
    m.definition_json = {**m.definition_json, "measures": ["gmv`; DROP TABLE t; --"]}
    req = QueryRequest(metric_code="gmv", date_range="")
    with pytest.raises(BusinessError) as exc:
        svc._build_query_sql(req, m)
    assert exc.value.error_code == ErrorCode.INJECTION_DETECTED


# ---- MySQL 降级执行 + 内部用户查询 + 自动保存快照 ----
class _FakeMysqlExecutor:
    """模拟 MySQL 降级执行器（enabled=True，execute 返回固定行）。"""

    enabled = True

    def __init__(self, rows=None):
        self.rows = rows or [{"region": "east", "gmv": 100.5}]
        self.executed_sql = None

    async def execute(self, sql, params=None, timeout=None):
        self.executed_sql = sql
        from app.services.consume.olap_executor import OLAPResult

        return OLAPResult(rows=self.rows, total=len(self.rows), elapsed_ms=5.0)


async def test_execute_mysql_fallback_when_olap_unconfigured(monkeypatch) -> None:
    """OLAP 未配置时降级 MySQL 执行器，并自动保存快照。"""
    svc = _svc(await _client())
    client = await svc.authenticate_client("acme:s3cr3t")
    svc._get_metric = AsyncMock(return_value=_metric(dims=("region",)))
    svc._build_query_sql = MagicMock(
        return_value=("SELECT region, gmv FROM t WHERE metric_code=:m", {"m": "gmv"})
    )
    svc._snapshots = MagicMock()
    svc._snapshots.create = AsyncMock()
    fake = _FakeMysqlExecutor()
    monkeypatch.setattr("app.services.consume.service.settings.olap_url", "")
    monkeypatch.setattr("app.services.consume.service.settings.mysql_fallback_url", "mysql+aiomysql://u:p@h:3306/db")
    monkeypatch.setattr("app.services.consume.service._get_mysql_executor", lambda: fake)

    res = await svc.execute_query(QueryRequest(metric_code="gmv", date_range=""), client)

    assert res.degraded is False
    assert res.data["engine"] == "mysql"
    assert res.data["rows"] == fake.rows
    assert fake.executed_sql.startswith("SELECT")
    # 自动保存快照（WORM）
    svc._snapshots.create.assert_awaited_once()
    snap = svc._snapshots.create.await_args.args[0]
    assert snap.metric_code == "gmv"
    assert snap.generated_by == SnapshotGeneratedBy.QUERY
    assert snap.value_json["engine"] == "mysql"


async def test_execute_internal_user_skips_whitelist_but_keeps_pii_gate(monkeypatch) -> None:
    """内部用户查询：跳过接入方白名单，但 PII 未复核仍被拒。"""
    svc = _svc(await _client())
    svc._get_metric = AsyncMock(
        return_value=_metric(pii=True, pii_flag=True, compliance_reviewed=False)
    )
    svc._build_query_sql = MagicMock(return_value=("SELECT 1", {}))
    svc._snapshots = MagicMock()
    svc._snapshots.create = AsyncMock()
    monkeypatch.setattr("app.services.consume.service.settings.olap_url", "")
    monkeypatch.setattr("app.services.consume.service.settings.mysql_fallback_url", "")
    from app.models.user import User

    user = User(id=1, username="alice")
    with pytest.raises(BusinessError) as exc:
        await svc.execute_query(QueryRequest(metric_code="gmv", date_range=""), internal_user=user)
    assert exc.value.error_code == ErrorCode.FORBIDDEN_PII


async def test_execute_internal_user_pii_reviewed_ok(monkeypatch) -> None:
    """内部用户查询：PII 已复核则放行并走 MySQL 降级。"""
    svc = _svc(await _client())
    svc._get_metric = AsyncMock(
        return_value=_metric(pii=True, pii_flag=True, compliance_reviewed=True)
    )
    svc._build_query_sql = MagicMock(return_value=("SELECT 1", {}))
    svc._snapshots = MagicMock()
    svc._snapshots.create = AsyncMock()
    fake = _FakeMysqlExecutor()
    monkeypatch.setattr("app.services.consume.service.settings.olap_url", "")
    monkeypatch.setattr("app.services.consume.service.settings.mysql_fallback_url", "mysql+aiomysql://u:p@h/db")
    monkeypatch.setattr("app.services.consume.service._get_mysql_executor", lambda: fake)
    # 内部用户查询已接入 PDP 闸门：放行（allow=True，无行级授权命中）
    from app.services.governance.policy import Decision

    async def _allow(*args, **kwargs):
        return (Decision(allow=True, reason="allowed"), None)

    monkeypatch.setattr(
        "app.services.consume.service.GovernanceService.check_internal_read_permission",
        _allow,
    )
    from app.models.user import User

    user = User(id=1, username="alice")
    res = await svc.execute_query(
        QueryRequest(metric_code="gmv", date_range=""), internal_user=user
    )
    assert res.data["engine"] == "mysql"
    svc._snapshots.create.assert_awaited_once()


async def test_execute_internal_user_denied_without_permission(monkeypatch) -> None:
    """内部用户查询 PDP 闸门：无跨域授权/非本域角色 → 拒绝（P0 数据权限修复）。"""
    svc = _svc(await _client())
    svc._get_metric = AsyncMock(return_value=_metric(pii=False, pii_flag=False))
    svc._snapshots = MagicMock()
    svc._snapshots.create = AsyncMock()
    from app.services.governance.policy import Decision

    async def _deny(*args, **kwargs):
        return (Decision(allow=False, reason="no grant", error_code="FORBIDDEN"), None)

    monkeypatch.setattr(
        "app.services.consume.service.GovernanceService.check_internal_read_permission",
        _deny,
    )
    from app.models.user import User

    user = User(id=1, username="alice")
    with pytest.raises(BusinessError) as exc:
        await svc.execute_query(
            QueryRequest(metric_code="gmv", date_range=""), internal_user=user
        )
    assert exc.value.error_code == ErrorCode.FORBIDDEN
    # 拒绝后不得落快照
    svc._snapshots.create.assert_not_awaited()


async def test_projection_columns_supports_measures_object_array() -> None:
    """投影列兼容 measures 对象数组（[{name, aggregation}]）——既存口径合法结构。

    回归：旧路径因 OLAP 未配置直接降级，_projection_columns 从未执行到对象数组；
    MySQL 降级打通后暴露「指标投影列标识非法」INJECTION_DETECTED。
    """
    svc = _svc(await _client())
    m = _metric(dims=("channel", "store"))
    m.definition_json = {
        **m.definition_json,
        "source_table": "dws_metric_outp_e2e_fee_day",
        "measures": [{"name": "gmv", "aggregation": "SUM"}],
    }
    req = QueryRequest(metric_code="gmv", date_range="")
    sql, params = svc._build_query_sql(req, m)
    assert "`channel`, `store`, `gmv`" in sql
    assert params["metric_code"] == "gmv"
