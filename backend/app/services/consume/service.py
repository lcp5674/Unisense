"""consume 层 Service（TD §12.6 / FR-12,13）。

承载：接入方鉴权、限流闸门、dry-run 口径校验、查询执行（OLAP 不可用降级 503）、
结果快照 WORM、用户收藏、口径版本消费方确认回调。
对齐 DEV_GUIDE §2（service 层不含 HTTP）与 §6.3（双视角审查后落地）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.config import settings
from app.core.degradation import fire_degradation_event
from app.core.error_codes import ErrorCode
from app.core.exceptions import BusinessError, ConflictError, NotFoundError
from app.core.security import verify_password
from app.models.consume import (
    ApiClient,
    ApiClientStatus,
    MetricValueSnapshot,
    SnapshotGeneratedBy,
)
from app.models.metric import Metric
from app.models.metric_version import MetricVersion
from app.services.consume.rate_limiter import (
    get_rate_limiter,
)
from app.services.consume.repository import ApiClientRepo, FavoriteRepo, SnapshotRepo
from app.services.consume.schemas import (
    DryRunResponse,
    FavoriteResponse,
    QueryRequest,
    QueryResponse,
    SnapshotResponse,
)

# 限流器在 lifespan 中通过 init_rate_limiter 动态初始化（Redis/InMemory 热切换）；
# 运行期统经 get_rate_limiter() 查阅，避免在 import 期冻结失效的快照（C6）。
_executor: Any | None = None


def _get_olap_executor() -> Any:
    """返回进程内共享的 OLAPExecutor 单例（复用连接池，避免每请求新建客户端泄漏）。"""
    global _executor
    if _executor is None:
        from app.services.consume.olap_executor import OLAPExecutor

        _executor = OLAPExecutor()
    return _executor


class ConsumeService(BaseService):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)
        self._db = db
        self._clients = ApiClientRepo(db)
        self._snapshots = SnapshotRepo(db)
        self._fav = FavoriteRepo(db)

    # ---- 真实物理口径 SQL 构建 ----
    def _build_query_sql(self, req: QueryRequest, metric: Any) -> tuple[str, dict[str, Any]]:
        """将查询请求编译为参数化的 OLAP SQL。

        基于指标口径来源字段（definition_json.source_table，缺省用指标编码做表名），
        叠加日期区间与维度过滤，杜绝拼串注入（全部参数化）。
        """
        table = (
            metric.definition_json.get("source_table")
            if getattr(metric, "definition_json", None)
            else None
        )
        if not table:
            table = f"dws_metric_{req.metric_code}"

        where: list[str] = ["metric_code = :metric_code"]
        params: dict[str, Any] = {"metric_code": req.metric_code}

        if req.date_range:
            where.append("dt >= :date_from AND dt <= :date_to")
            parts = req.date_range.split("~")
            params["date_from"] = parts[0].strip()
            params["date_to"] = parts[1].strip() if len(parts) > 1 else parts[0].strip()

        for i, dim in enumerate(req.dimensions):
            # 维度名属 SQL 标识符（无法参数化），故必须收敛到口径声明的维度集。
            # 过去仅 dry-run 校验，execute_query 路径未校验 → 越权列访问 / 标识符注入缺口。
            # guard 仅扫描字符串 *值*，不防御标识符；此处为纵深防御的最内层。
            allowed_dims = set((metric.definition_json or {}).get("dimensions", []))
            if dim.name not in allowed_dims:
                raise BusinessError(
                    f"维度 {dim.name} 不在指标可用维度内",
                    error_code=ErrorCode.FORBIDDEN_DIMENSION,
                )
            key = f"dim_{i}"
            if isinstance(dim.value, (list, tuple)):
                where.append(f"{dim.name} IN :{key}")
                params[key] = list(dim.value)
            else:
                where.append(f"{dim.name} = :{key}")
                params[key] = dim.value

        sql = f"SELECT * FROM `{table}` WHERE {' AND '.join(where)}"
        return sql, params

    # ---- 接入方鉴权 ----
    async def authenticate_client(self, api_key: str) -> ApiClient:
        if ":" not in api_key:
            raise BusinessError(
                "X-Api-Key 格式应为 client_id:secret", error_code=ErrorCode.AUTH_APIKEY_INVALID
            )
        cid, secret = api_key.split(":", 1)
        client = await self._clients.get_by_client_id(cid)
        if client is None or client.status != ApiClientStatus.ACTIVE:
            raise BusinessError("接入方不存在或已吊销", error_code=ErrorCode.AUTH_APIKEY_INVALID)
        if not await verify_password(secret, client.client_secret_ref):
            raise BusinessError("密钥校验失败", error_code=ErrorCode.AUTH_APIKEY_INVALID)
        return client

    async def check_rate_limit(self, client: ApiClient) -> None:
        limiter = get_rate_limiter()  # 动态获取：lifespan 初始化后 Redis 优先
        if not await limiter.allow(client.client_id, client.qps):
            raise BusinessError(
                "QPS 超限", error_code=ErrorCode.RATE_LIMITED, ctx={"retry_after": 1}
            )
        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        if not await limiter.allow_daily(client.client_id, client.daily_quota, today):
            raise BusinessError(
                "日查询配额已耗尽",
                error_code=ErrorCode.RATE_LIMITED,
                ctx={"retry_after": 3600, "daily_quota": client.daily_quota},
            )

    # ---- dry-run ----
    async def dry_run_query(self, req: QueryRequest, client: ApiClient) -> DryRunResponse:
        checks: list[dict[str, Any]] = []
        metric = await self._get_metric(req.metric_code)
        if metric is None:
            raise NotFoundError(f"指标 {req.metric_code} 不存在")
        if metric.status != "PUBLISHED":
            code = (
                ErrorCode.FORBIDDEN_DEPRECATED
                if metric.status == "DEPRECATED"
                else ErrorCode.FORBIDDEN_METRIC
            )
            raise BusinessError(f"指标状态 {metric.status} 不可消费", error_code=code)
        self._assert_authorized(client, metric)
        grain = (metric.definition_json or {}).get("grain")
        checks.append({"check": "granularity", "ok": True, "detail": f"指标粒度 {grain}"})
        expr = (metric.definition_json or {}).get("expression", "")
        # 构建真实物理口径 SQL（参数化），而非占位注释；维度授权收敛在 _build_query_sql 内。
        sql, sql_params = self._build_query_sql(req, metric)
        plan = {
            "metric_code": req.metric_code,
            "expression_ast": {"raw": expr},
            "dialect_sql": sql,
            "sql_params": sql_params,
            "dimensions": [d.model_dump() for d in req.dimensions],
            "date_range": req.date_range,
            "granularity": req.granularity,
        }
        return DryRunResponse(
            metric_code=req.metric_code,
            status="ok",
            checks=checks,
            execution_plan=plan,
            meta=self._build_meta(metric),
        )

    # ---- execute ----
    async def execute_query(self, req: QueryRequest, client: ApiClient) -> QueryResponse:
        metric = await self._get_metric(req.metric_code)
        if metric is None:
            raise NotFoundError(f"指标 {req.metric_code} 不存在")
        if metric.status != "PUBLISHED":
            code = (
                ErrorCode.FORBIDDEN_DEPRECATED
                if metric.status == "DEPRECATED"
                else ErrorCode.FORBIDDEN_METRIC
            )
            raise BusinessError(f"指标状态 {metric.status} 不可消费", error_code=code)
        self._assert_authorized(client, metric)
        if not settings.olap_url:
            # 配置级降级（熔断器无法感知），上报降级事件供看板/审计（TD §5.2.5）
            fire_degradation_event("OLAP", "olap", "DEGRADED", "olap_not_configured")
            raise BusinessError(
                "OLAP 执行引擎不可用，查询降级",
                error_code=ErrorCode.DEPENDENCY_DEGRADED_ENGINE,
                ctx={"retry_after": 30, "accept_stale": req.accept_stale},
            )

        # 构建执行 SQL（真实物理口径，而非占位查询）
        sql, params = self._build_query_sql(req, metric)

        # 通过共享 OLAPExecutor 执行真实查询（复用连接池，避免每请求新建客户端泄漏）
        executor = _get_olap_executor()
        try:
            olap_result = await executor.execute(sql, params)
            plan = {
                "metric_code": req.metric_code,
                "dimensions": [d.model_dump() for d in req.dimensions],
                "date_range": req.date_range,
            }
            return QueryResponse(
                metric_code=req.metric_code,
                degraded=False,
                data={
                    "rows": olap_result.rows,
                    "total": olap_result.total,
                    "elapsed_ms": olap_result.elapsed_ms,
                    "from_cache": olap_result.from_cache,
                },
                execution_plan=plan,
                meta=self._build_meta(metric),
            )
        except BusinessError:
            raise  # 降级错误直接抛出
        except Exception as exc:
            raise BusinessError(
                f"OLAP 查询执行失败: {exc}",
                error_code=ErrorCode.DEPENDENCY_DEGRADED_ENGINE,
                ctx={"retry_after": 30},
            ) from exc

    # ---- 快照 WORM ----
    async def save_snapshot(
        self,
        metric_code: str,
        version: int,
        dims: dict[str, Any],
        date_range: str,
        value_json: dict[str, Any],
        quality_flag: str | None,
        generated_at: Any,
        generated_by: SnapshotGeneratedBy = SnapshotGeneratedBy.QUERY,
    ) -> SnapshotResponse:
        snap = MetricValueSnapshot(
            metric_code=metric_code,
            version=version,
            dims=dims,
            date_range=date_range,
            value_json=value_json,
            quality_flag=quality_flag,
            generated_at=generated_at,
            generated_by=generated_by,
        )
        await self._snapshots.create(snap)
        return self._to_snap(snap)

    async def list_snapshots(
        self, metric_code: str, limit: int, offset: int
    ) -> list[SnapshotResponse]:
        rows = await self._snapshots.list_by_metric(metric_code, limit, offset)
        return [self._to_snap(r) for r in rows]

    # ---- 收藏 ----
    async def add_favorite(self, user_id: int, metric_code: str) -> FavoriteResponse:
        codes = await self._fav.list_pinned(user_id)
        if metric_code not in codes:
            codes.append(metric_code)
            await self._fav.upsert_pinned(user_id, codes)
        return FavoriteResponse(metric_code=metric_code, pinned=True)

    async def remove_favorite(self, user_id: int, metric_code: str) -> FavoriteResponse:
        codes = await self._fav.list_pinned(user_id)
        codes = [c for c in codes if c != metric_code]
        await self._fav.upsert_pinned(user_id, codes)
        return FavoriteResponse(metric_code=metric_code, pinned=False)

    async def list_favorites(self, user_id: int) -> list[str]:
        return await self._fav.list_pinned(user_id)

    # ---- 版本消费方确认回调 ----
    async def confirm_version(self, version_id: int, user_id: int) -> None:
        mv = await self._get_version(version_id)
        if mv is None:
            raise NotFoundError(f"版本 {version_id} 不存在")
        # 对齐语义模块 VersionStatusEnum（T002）：待确认状态为 PENDING_CONFIRMATION
        if mv.status != "PENDING_CONFIRMATION":
            raise ConflictError("版本不在待确认状态")
        mv.status = "PUBLISHED"
        await self._db.flush()

    async def reject_version(self, version_id: int, user_id: int, reason: str | None) -> None:
        mv = await self._get_version(version_id)
        if mv is None:
            raise NotFoundError(f"版本 {version_id} 不存在")
        # 对齐语义模块 VersionStatusEnum（T002）：待确认状态为 PENDING_CONFIRMATION
        if mv.status != "PENDING_CONFIRMATION":
            raise ConflictError("版本不在待确认状态")
        mv.status = "ARCHIVED"
        await self._db.flush()

    # ---- helpers ----
    async def _get_metric(self, code: str) -> Metric | None:
        stmt = select(Metric).where(Metric.metric_code == code, Metric.deleted_at.is_(None))
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def _get_version(self, vid: int) -> MetricVersion | None:
        stmt = select(MetricVersion).where(
            MetricVersion.id == vid, MetricVersion.deleted_at.is_(None)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def is_pii(metric: Metric) -> bool:
        """指标是否承载 PII（definition_json.pii，对齐 TD §15.4 数据分级）。"""
        return bool((metric.definition_json or {}).get("pii", False))

    @classmethod
    def _assert_authorized(cls, client: ApiClient, metric: Metric) -> None:
        """接入方授权闸门（fail-closed）：域 → 白名单 → PII 三级校验。

        - scope_domain 非空时，跨域访问一律 FORBIDDEN_DOMAIN（此前未校验，属越权缺口）。
        - metric_whitelist 非空时，仅白名单内指标可访问。
        - PII 指标必须被白名单**显式列出**才可消费；"域内全量"授权不隐式覆盖 PII。
        """
        if client.scope_domain and metric.domain != client.scope_domain:
            raise BusinessError("指标不在接入方授权域内", error_code=ErrorCode.FORBIDDEN_DOMAIN)
        if client.metric_whitelist and metric.metric_code not in client.metric_whitelist:
            raise BusinessError("超出接入方授权范围", error_code=ErrorCode.FORBIDDEN_METRIC)
        if cls.is_pii(metric) and metric.metric_code not in (client.metric_whitelist or []):
            raise BusinessError("PII 指标需显式白名单授权", error_code=ErrorCode.FORBIDDEN_PII)

    @staticmethod
    def _build_meta(metric: Metric) -> dict[str, Any]:
        dj = metric.definition_json or {}
        return {
            "grain": dj.get("grain"),
            "unit": dj.get("unit"),
            "pii": dj.get("pii", False),
            "lineage": dj.get("dependencies", []),
            "domain": metric.domain,
            "status": metric.status,
        }

    @staticmethod
    def _to_snap(r: MetricValueSnapshot) -> SnapshotResponse:
        return SnapshotResponse(
            id=r.id,
            metric_code=r.metric_code,
            version=r.version,
            dims=r.dims,
            date_range=r.date_range,
            value_json=r.value_json,
            quality_flag=r.quality_flag,
            generated_at=r.generated_at,
            generated_by=r.generated_by,
        )
