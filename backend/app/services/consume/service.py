"""consume 层 Service（TD §12.6 / FR-12,13）。

承载：接入方鉴权、限流闸门、dry-run 口径校验、查询执行（OLAP 不可用降级 503）、
结果快照 WORM、用户收藏、口径版本消费方确认回调。
对齐 DEV_GUIDE §2（service 层不含 HTTP）与 §6.3（双视角审查后落地）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.config import settings
from app.core.degradation import fire_degradation_event
from app.core.error_codes import ErrorCode
from app.core.exceptions import BusinessError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.security import verify_password
from app.models.consume import (
    ApiClient,
    ApiClientStatus,
    FavoriteAssetType,
    MetricValueSnapshot,
    SnapshotGeneratedBy,
)
from app.models.data_source import DataSource, DBCatalog
from app.models.dimension import Dimension
from app.models.metric import Metric
from app.models.metric_template import MetricTemplate
from app.models.metric_version import MetricVersion
from app.models.term import Term
from app.models.user import User
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
from app.services.governance.service import GovernanceService  # noqa: F401 供测试 patch 定位

# 限流器在 lifespan 中通过 init_rate_limiter 动态初始化（Redis/InMemory 热切换）；
# 运行期统经 get_rate_limiter() 查阅，避免在 import 期冻结失效的快照（C6）。
_executor: Any | None = None
logger = get_logger("unisense.consume")

# 标识符白名单：表名 / 列名无法参数化，必须收敛到安全字符集（长度对齐 DB 列 String(64)），
# 杜绝标识符注入（反引号 / 空格 / 分号等皆可逃逸 `` `...` `` 包裹）。维度名另有口径声明集二次校验。
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
# 日期区间每段的格式（YYYY-MM 或 YYYY-MM-DD），覆盖单值 / 区间两种写法。
_DATE_PART_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
# 单次查询结果行数硬上限：防止 SELECT * 全表拖回导致 OOM/超时（FR-06 生产护栏）。
_MAX_QUERY_ROWS = 1000


def _validate_date_part(part: str) -> None:
    """校验单段日期格式与语义合法性（年月 / 年月日），非法抛 VALIDATION_ERROR。"""
    if not _DATE_PART_RE.match(part):
        raise BusinessError("日期区间格式非法", error_code=ErrorCode.VALIDATION_ERROR)
    y, m, *rest = part.split("-")
    if not (1 <= int(m) <= 12):
        raise BusinessError("日期区间格式非法", error_code=ErrorCode.VALIDATION_ERROR)
    if rest and not (1 <= int(rest[0]) <= 31):
        raise BusinessError("日期区间格式非法", error_code=ErrorCode.VALIDATION_ERROR)


def _get_olap_executor() -> Any:
    """返回进程内共享的 OLAPExecutor 单例（复用连接池，避免每请求新建客户端泄漏）。"""
    global _executor
    if _executor is None:
        from app.services.consume.olap_executor import OLAPExecutor

        _executor = OLAPExecutor()
    return _executor


_mysql_executor: Any | None = None


def _get_mysql_executor() -> Any:
    """返回进程内共享的 MysqlExecutor 单例（OLAP 不可用时的只读降级引擎）。"""
    global _mysql_executor
    if _mysql_executor is None:
        from app.services.consume.mysql_executor import MysqlExecutor

        _mysql_executor = MysqlExecutor()
    return _mysql_executor


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
        # 标识符（表名）无法参数化，须收敛到安全字符集，杜绝标识符注入。
        # source_table 由指标 Owner 声明、metric_code 由调用方传入，二者均不可信。
        if not _IDENTIFIER_RE.match(table):
            raise BusinessError(
                "指标来源表标识非法",
                error_code=ErrorCode.INJECTION_DETECTED,
            )

        where: list[str] = ["metric_code = :metric_code"]
        params: dict[str, Any] = {"metric_code": req.metric_code}

        if req.date_range:
            parts = req.date_range.split("~")
            if len(parts) > 2:
                raise BusinessError("日期区间格式非法", error_code=ErrorCode.VALIDATION_ERROR)
            for part in parts:
                _validate_date_part(part.strip())
            where.append("dt >= :date_from AND dt <= :date_to")
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

        projection = self._projection_columns(metric)
        sql = f"SELECT {projection} FROM `{table}` WHERE {' AND '.join(where)} LIMIT :__max_rows"
        params["__max_rows"] = _MAX_QUERY_ROWS
        return sql, params

    def _projection_columns(self, metric: Any) -> str:
        """收敛 SELECT 投影列到口径声明的维度+度量列。

        FR-06 生产护栏：口径未声明任何列时退化为 ``*``（兼容存量数据），
        但仍受外层 ``LIMIT`` 硬上限约束；声明了 ``measures`` 时严格收敛投影，
        既防越权列读取，也避免 ``SELECT *`` 拖回全表造成 OOM/超时。
        投影列均须过标识符白名单（列名无法参数化，杜绝标识符注入）。
        """
        defn = metric.definition_json or {}
        dims = defn.get("dimensions") or []
        measures = defn.get("measures") or defn.get("columns") or []
        # measures 支持两种形态：字符串数组（["gmv"]）或对象数组
        # （[{"name":"gmv","aggregation":"SUM"}]），对象形态取 name 字段
        # （既存口径的合法结构，旧路径因 OLAP 未配置降级从未执行到此处）。
        cols = list(dims)
        for m in measures:
            if isinstance(m, dict):
                name = m.get("name") or m.get("column")
                if name:
                    cols.append(name)
            else:
                cols.append(m)
        if not cols:
            return "*"
        for col in cols:
            if not isinstance(col, str) or not _IDENTIFIER_RE.match(col):
                raise BusinessError(
                    "指标投影列标识非法",
                    error_code=ErrorCode.INJECTION_DETECTED,
                )
        return ", ".join(f"`{c}`" for c in cols)

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

    async def authenticate_consume_token(self, token: str) -> ApiClient:
        """校验短效消费方 JWT（issue_token 签发，role=consume, sub=client_id）。

        供平台内调试/前端 QueryWorkspace 使用：先由平台管理员经
        ``POST /consume/api-clients/{id}/token`` 换发，再持 Bearer 调用查询端点。
        校验失败抛出 AUTH_APIKEY_INVALID，不区分具体原因（防探测）。
        """
        import jwt

        try:
            payload = jwt.decode(
                token,
                settings.jwt_secret,
                algorithms=[settings.jwt_algorithm],
            )
        except jwt.ExpiredSignatureError:
            raise BusinessError(
                "消费令牌已过期，请重新签发", error_code=ErrorCode.AUTH_APIKEY_INVALID
            ) from None
        except jwt.InvalidTokenError:
            raise BusinessError("消费令牌无效", error_code=ErrorCode.AUTH_APIKEY_INVALID) from None
        if payload.get("role") != "consume":
            raise BusinessError(
                "令牌角色不符，请使用消费方令牌", error_code=ErrorCode.AUTH_APIKEY_INVALID
            )
        cid = str(payload.get("sub", ""))
        client = await self._clients.get_by_client_id(cid)
        if client is None or client.status != ApiClientStatus.ACTIVE:
            raise BusinessError("接入方不存在或已吊销", error_code=ErrorCode.AUTH_APIKEY_INVALID)
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
    async def execute_query(
        self,
        req: QueryRequest,
        client: ApiClient | None = None,
        internal_user: User | None = None,
    ) -> QueryResponse:
        """执行指标真实查询（OLAP 优先，不可用/失败时降级 MySQL 只读引擎）。

        Args:
            req: 查询请求。
            client: 接入方（internal_user 为空时走接入方鉴权 + 限流闸门）。
            internal_user: 内部登录用户（资产地图/指标详情「查询最新数据」用）。
                提供时接入 PDP 数据权限决策（platform_admin 直通 / 本域角色 /
                跨域 ACTIVE 未过期 grants，含 metric_whitelist / row_level），
                并保留指标状态与 PII 合规复核闸门（COMPL-1）。

        Returns:
            QueryResponse（含真实查询行）。成功后自动保存 WORM 快照（写入失败不阻塞响应）。
        """
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
        row_grant: dict[str, Any] | None = None
        if internal_user is None:
            if client is None:
                raise BusinessError("缺少消费凭证", error_code=ErrorCode.AUTH_APIKEY_MISSING)
            self._assert_authorized(client, metric)
        else:
            # PII 合规复核闸门（COMPL-1）保留
            if self.is_pii(metric) and not metric.compliance_reviewed:
                raise BusinessError(
                    "PII 指标未通过合规复核，禁止消费",
                    error_code=ErrorCode.FORBIDDEN_PII,
                )
            # PDP 数据权限闸门（P0 修复）：内部用户此前跳过一切鉴权，可对任意域任意
            # 指标执行真实查询。现接入 PDP：platform_admin 直通 / 本域角色按
            # ROLE_ACTIONS / 跨域须命中 ACTIVE 未过期 grants（含 metric_whitelist）。
            decision, matched_grant = await GovernanceService(
                self._db
            ).check_internal_read_permission(internal_user, req.metric_code)
            if not decision.allow:
                raise BusinessError(
                    decision.reason or "无权限查询该指标",
                    error_code=decision.error_code or ErrorCode.FORBIDDEN,
                    ctx={"metric_code": req.metric_code, "actor_id": internal_user.id},
                )
            if decision.restricted and matched_grant is not None:
                row_grant = matched_grant

        # 构建执行 SQL（真实物理口径，而非占位查询）
        sql, params = self._build_query_sql(req, metric)

        # 引擎选择：OLAP 优先，失败/未配置时降级 MySQL 只读执行器
        result, engine_used = await self._execute_with_fallback(req, sql, params)
        if row_grant is not None:
            result = self._filter_restricted_rows(result, row_grant)
        plan = {
            "metric_code": req.metric_code,
            "dimensions": [d.model_dump() for d in req.dimensions],
            "date_range": req.date_range,
        }
        response = QueryResponse(
            metric_code=req.metric_code,
            degraded=False,
            data={
                "rows": result.rows,
                "total": result.total,
                "elapsed_ms": result.elapsed_ms,
                "from_cache": result.from_cache,
                "engine": engine_used,
            },
            execution_plan=plan,
            meta=self._build_meta(metric),
        )
        # 查询成功即自动保存 WORM 快照（留痕；写入失败仅告警，不阻塞查询响应）
        await self._maybe_save_snapshot(metric, req, result, engine_used)
        return response

    async def _execute_with_fallback(
        self,
        req: QueryRequest,
        sql: str,
        params: dict[str, Any],
    ) -> tuple[Any, str]:
        """执行查询并返回 (OLAPResult, 引擎标识)，OLAP 不可用时降级 MySQL。

        - OLAP 配置且执行成功 → ("olap")；
        - OLAP 失败或未配置 → 尝试 MySQL 降级（配置了 mysql_fallback_url 时）→ ("mysql")；
        - 两者均不可用 → 抛 DEPENDENCY_DEGRADED_ENGINE。
        """
        olap_tried = False
        if settings.olap_url:
            olap_tried = True
            try:
                executor = _get_olap_executor()
                return await executor.execute(sql, params), "olap"
            except Exception as exc:
                logger.warning("olap_execute_failed_fallback_mysql", error=str(exc))

        mysql_executor = _get_mysql_executor()
        if mysql_executor.enabled:
            try:
                return await mysql_executor.execute(sql, params), "mysql"
            except Exception as exc:
                logger.warning("mysql_fallback_failed", error=str(exc))
                fire_degradation_event("OLAP", "olap", "DEGRADED", "engine_unavailable")
                raise BusinessError(
                    "查询执行引擎不可用，查询降级",
                    error_code=ErrorCode.DEPENDENCY_DEGRADED_ENGINE,
                    ctx={"retry_after": 30, "accept_stale": req.accept_stale},
                ) from exc

        # 无任何可用引擎
        reason = "olap_failed" if olap_tried else "olap_not_configured"
        fire_degradation_event("OLAP", "olap", "DEGRADED", reason)
        raise BusinessError(
            "OLAP 执行引擎不可用，查询降级",
            error_code=ErrorCode.DEPENDENCY_DEGRADED_ENGINE,
            ctx={"retry_after": 30, "accept_stale": req.accept_stale},
        )

    @staticmethod
    def _filter_restricted_rows(result: Any, grant: dict[str, Any]) -> Any:
        """行级授权安全兜底：restricted 授权命中时仅保留白名单内指标的行。

        查询 SQL 已按 ``metric_code = :metric_code`` 收敛到单指标（该码命中白名单
        由 PDP ``_match_grant`` 保证），此处对结果行做二次防御性过滤：行内含
        ``metric_code`` 字段时须在白名单内，否则剔除；行内无该字段的按查询约束
        视为白名单内放行。完整 RLS（维度值级过滤 + 脱敏）为 TD §12.5 二期范围。
        """
        whitelist = {str(x) for x in (grant.get("metric_whitelist") or [])}
        if not whitelist:
            return result
        filtered = [
            row
            for row in result.rows
            if "metric_code" not in row or str(row.get("metric_code")) in whitelist
        ]
        result.rows = filtered
        result.total = len(filtered)
        return result

    async def _maybe_save_snapshot(
        self,
        metric: Metric,
        req: QueryRequest,
        result: Any,
        engine_used: str,
    ) -> None:
        """查询成功后自动保存 WORM 快照（写入失败仅告警，不阻塞查询响应）。"""
        try:
            dims: dict[str, Any] = {}
            for d in req.dimensions:
                if isinstance(d.value, (list, tuple)):
                    dims[d.name] = list(d.value)
                else:
                    dims[d.name] = d.value
            await self.save_snapshot(
                metric_code=req.metric_code,
                version=getattr(metric, "version", 1),
                dims=dims,
                date_range=req.date_range or "",
                value_json={
                    "rows": result.rows,
                    "total": result.total,
                    "engine": engine_used,
                },
                quality_flag=None,
                generated_at=datetime.now(UTC),
                generated_by=SnapshotGeneratedBy.QUERY,
            )
        except Exception:
            logger.warning(
                "snapshot_save_failed",
                metric_code=req.metric_code,
                engine=engine_used,
                exc_info=True,
            )

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

    # ---- 收藏（通用多资产，TD §5.4 favorite）----
    # 各资产类型的 ORM 模型 + 业务编码列（asset_id 统一为业务编码，非数据库 id）
    _ASSET_MODEL: dict[FavoriteAssetType, tuple[Any, str]] = {
        FavoriteAssetType.METRIC: (Metric, "metric_code"),
        FavoriteAssetType.TABLE: (DBCatalog, "entity_name"),
        FavoriteAssetType.TERM: (Term, "term_code"),
        FavoriteAssetType.DIMENSION: (Dimension, "dim_code"),
        FavoriteAssetType.TEMPLATE: (MetricTemplate, "code"),
    }

    async def add_favorite(
        self, user_id: int, asset_type: FavoriteAssetType, asset_id: str
    ) -> FavoriteResponse:
        # 校验资产存在（含软删除过滤），避免收藏死码/死链
        await self._ensure_asset(asset_type, asset_id)
        existing = await self._fav.get(user_id, asset_type.value, asset_id)
        if existing is None:
            await self._fav.add(user_id, asset_type.value, asset_id)
        return FavoriteResponse(asset_type=asset_type.value, asset_id=asset_id, pinned=True)

    async def remove_favorite(
        self, user_id: int, asset_type: FavoriteAssetType, asset_id: str
    ) -> FavoriteResponse:
        await self._fav.remove(user_id, asset_type.value, asset_id)
        return FavoriteResponse(asset_type=asset_type.value, asset_id=asset_id, pinned=False)

    async def list_favorites(self, user_id: int) -> list[dict[str, str]]:
        """返回用户收藏（通用结构，供各页判断收藏状态）。"""
        favs = await self._fav.list(user_id)
        return [
            {"asset_type": f.asset_type.value, "asset_id": f.asset_id} for f in favs
        ]

    async def list_favorite_details(self, user_id: int) -> list[dict[str, Any]]:
        """多资产收藏详情聚合（按类型分组批量查询，消除逐条取名 N+1）。

        - 含收藏时间（created_at），按最近收藏排序；
        - 软删除/已不存在的资产保留条目并标记 dead=True（前端灰显），
          修复原实现未过滤 deleted_at 导致软删除指标显示为有效的 bug。
        """
        favs = await self._fav.list(user_id)
        if not favs:
            return []
        by_type: dict[str, list[str]] = {}
        for f in favs:
            by_type.setdefault(f.asset_type.value, []).append(f.asset_id)
        lookup = await self._load_asset_details(by_type)
        result: list[dict[str, Any]] = []
        for f in favs:
            key = (f.asset_type.value, f.asset_id)
            detail = lookup.get(key)
            result.append(
                {
                    "asset_type": f.asset_type.value,
                    "asset_id": f.asset_id,
                    "name": detail["name"] if detail else f.asset_id,
                    "description": detail["description"] if detail else None,
                    "domain": detail["domain"] if detail else None,
                    "status": detail["status"] if detail else "UNKNOWN",
                    "tier": detail["tier"] if detail else None,
                    "is_pii": detail["is_pii"] if detail else False,
                    "created_at": f.created_at.isoformat(),
                    "dead": detail is None,
                }
            )
        return result

    async def _load_asset_details(
        self, by_type: dict[str, list[str]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """按资产类型批量加载摘要（各类型一次 IN 查询；TABLE 关联数据源取域）。"""
        lookup: dict[tuple[str, str], dict[str, Any]] = {}
        source_domains: dict[str, str] = {}
        for type_value, ids in by_type.items():
            asset_type = FavoriteAssetType(type_value)
            model, id_attr = self._ASSET_MODEL[asset_type]
            stmt = select(model).where(
                getattr(model, id_attr).in_(ids), model.deleted_at.is_(None)
            )
            rows = (await self._db.execute(stmt)).scalars().all()
            for row in rows:
                code = getattr(row, id_attr)
                if asset_type == FavoriteAssetType.TABLE and getattr(row, "source_id", None):
                    source_domains.setdefault(row.source_id, "")
                lookup[(type_value, code)] = self._asset_summary(asset_type, row)
        # TABLE 域：一次批量关联数据源
        if source_domains:
            srcs = (
                await self._db.execute(
                    select(DataSource).where(DataSource.source_id.in_(source_domains.keys()))
                )
            ).scalars().all()
            for s in srcs:
                source_domains[s.source_id] = s.domain
            for key, detail in lookup.items():
                if key[0] == FavoriteAssetType.TABLE.value and not detail["domain"]:
                    detail["domain"] = source_domains.get(detail["source_id"])
        return lookup

    async def _ensure_asset(
        self, asset_type: FavoriteAssetType, asset_id: str
    ) -> None:
        model, id_attr = self._ASSET_MODEL[asset_type]
        stmt = select(model).where(
            getattr(model, id_attr) == asset_id, model.deleted_at.is_(None)
        )
        exists = (await self._db.execute(stmt)).scalar_one_or_none() is not None
        if not exists:
            raise NotFoundError(f"资产不存在: {asset_id}")

    def _asset_summary(
        self, asset_type: FavoriteAssetType, row: Any
    ) -> dict[str, Any]:
        """统一抽取各资产摘要字段（name/description/domain/status/tier/is_pii）。"""
        if asset_type == FavoriteAssetType.METRIC:
            return {
                "name": row.name,
                "description": getattr(row, "description", None),
                "domain": row.domain,
                "status": row.status,
                "tier": getattr(row, "metric_tier", None),
                "is_pii": bool(getattr(row, "pii_flag", False)),
            }
        if asset_type == FavoriteAssetType.TABLE:
            return {
                "name": row.entity_name,
                "description": getattr(row, "description", None),
                "domain": None,
                "status": row.sensitivity_level,
                "tier": None,
                "is_pii": False,
                "source_id": getattr(row, "source_id", None),
            }
        if asset_type == FavoriteAssetType.TERM:
            return {
                "name": row.name,
                "description": row.definition,
                "domain": row.domain,
                "status": row.status,
                "tier": None,
                "is_pii": False,
            }
        if asset_type == FavoriteAssetType.DIMENSION:
            return {
                "name": row.name,
                "description": getattr(row, "description", None),
                "domain": row.domain,
                "status": row.status,
                "tier": None,
                "is_pii": False,
            }
        # TEMPLATE
        return {
            "name": row.name,
            "description": getattr(row, "description", None),
            "domain": row.domain,
            "status": "ACTIVE" if getattr(row, "is_active", False) else "INACTIVE",
            "tier": None,
            "is_pii": False,
        }

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
        """指标是否承载 PII（definition_json.pii 或 pii_flag，任一为真即为 PII）。

        修复前：仅检查 definition_json.pii，pii_flag 由合规复核写入，
        导致通过 PII 复核但 definition_json 未同步的指标被误判为非 PII。
        """
        dj = metric.definition_json or {}
        return bool(dj.get("pii", False)) or bool(metric.pii_flag)

    @classmethod
    def _assert_authorized(cls, client: ApiClient, metric: Metric) -> None:
        """接入方授权闸门（fail-closed）：域 → 白名单 → PII 合规四级校验。

        - scope_domain 非空时，跨域访问一律 FORBIDDEN_DOMAIN（此前未校验，属越权缺口）。
        - metric_whitelist 非空时，仅白名单内指标可访问。
        - PII 指标必须被白名单**显式列出**才可消费；"域内全量"授权不隐式覆盖 PII。
        - PII 指标须已完成合规复核（compliance_reviewed=True），
          未经复核的 PII 指标对所有接入方禁止消费（TD §12.5 COMPL-1）。
        """
        if client.scope_domain and metric.domain != client.scope_domain:
            raise BusinessError("指标不在接入方授权域内", error_code=ErrorCode.FORBIDDEN_DOMAIN)
        if client.metric_whitelist and metric.metric_code not in client.metric_whitelist:
            raise BusinessError("超出接入方授权范围", error_code=ErrorCode.FORBIDDEN_METRIC)
        if cls.is_pii(metric) and metric.metric_code not in (client.metric_whitelist or []):
            raise BusinessError("PII 指标需显式白名单授权", error_code=ErrorCode.FORBIDDEN_PII)
        # PII 合规复核闸门（COMPL-1）：修复前 consume 不校验 compliance_reviewed，
        # PII=1 且未复核的指标仍可被消费，造成合规风险
        if cls.is_pii(metric) and not metric.compliance_reviewed:
            raise BusinessError(
                "PII 指标未通过合规复核，禁止消费",
                error_code=ErrorCode.FORBIDDEN_PII,
            )

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
