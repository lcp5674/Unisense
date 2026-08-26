"""consume 层 Service（TD §12.6 / FR-12,13）。

承载：接入方鉴权、限流闸门、dry-run 口径校验、查询执行（OLAP 不可用降级 503）、
结果快照 WORM、用户收藏、口径版本消费方确认回调。
对齐 DEV_GUIDE §2（service 层不含 HTTP）与 §6.3（双视角审查后落地）。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
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
    QueryLog,
    QueryRequesterType,
    SnapshotGeneratedBy,
)
from app.models.data_source import DataSource, DBCatalog
from app.models.dimension import Dimension, MetricDimension
from app.models.metric import Metric
from app.models.metric_template import MetricTemplate
from app.models.metric_version import MetricVersion, PendingVersionConfirmation
from app.models.term import Term
from app.models.user import User
from app.services.consume.masking import mask_rows
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


def _observe_query_result(success: bool) -> None:
    """上报查询结果指标（P0-2 可观测性：unisense_query_success/failure_total）。

    best-effort：埋点失败绝不阻断查询响应。
    """
    try:
        from app.core.metrics import store as _metrics_store

        _metrics_store.observe_query_result(success)
    except Exception:  # noqa: BLE001 - 埋点失败不阻断查询
        logger.warning("query_metrics_observe_failed", exc_info=True)


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
    async def _resolve_mount_table(self, metric: Any) -> str | None:
        """OneData 挂载层权威（界限文档 §2.3）：派生指标挂载可经挂载 API 独立更新，
        definition_json 的 source_table 冗余可能过期——消费 SQL 以 metric_mount 为准。
        查询失败或未挂载时返回 None（回退 definition_json）。
        """
        try:
            from app.services.metric_mount.repository import MetricMountRepository

            mount = await MetricMountRepository(self._db).get_by_metric(metric.id)
            if mount is not None and isinstance(mount.source_table, str) and mount.source_table:
                return mount.source_table
        except Exception:  # noqa: BLE001 - best-effort：mount 查询失败回退 definition_json
            pass
        return None

    def _build_query_sql(
        self,
        req: QueryRequest,
        metric: Any,
        bound_dims: set[str] | None = None,
        mount_table: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """将查询请求编译为参数化的 OLAP SQL。

        基于指标口径来源字段（definition_json.source_table，缺省用指标编码做表名），
        叠加日期区间与维度过滤，杜绝拼串注入（全部参数化）。
        挂载层权威：``mount_table`` 由调用方经 ``_resolve_mount_table`` 解析（挂载优先）。
        """
        table = mount_table or (
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
            # 允许维度集：以 definition_json.dimensions 为主来源，再补充
            # metric_dimension 绑定表（打通维度管理「绑定指标」到消费链路，方案③）。
            allowed_dims = set((metric.definition_json or {}).get("dimensions", []))
            if bound_dims:
                allowed_dims |= bound_dims
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
        # 维度允许集补充 metric_dimension 绑定表来源（打通维度管理绑定到消费链路）。
        bound_dims = await self._get_bound_dimensions(metric.id)
        mount_table = await self._resolve_mount_table(metric)
        sql, sql_params = self._build_query_sql(req, metric, bound_dims, mount_table=mount_table)
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
        # 维度允许集补充 metric_dimension 绑定表来源（打通维度管理绑定到消费链路）。
        bound_dims = await self._get_bound_dimensions(metric.id)
        mount_table = await self._resolve_mount_table(metric)
        sql, params = self._build_query_sql(req, metric, bound_dims, mount_table=mount_table)

        # 引擎选择：OLAP 优先，失败/未配置时降级 MySQL 只读执行器
        result, engine_used = await self._execute_with_fallback(req, sql, params)
        if row_grant is not None:
            result = self._filter_restricted_rows(result, row_grant)
        # PII 数据值脱敏（合规增强 C-3）：PII 指标查询结果中的 PII 维度列
        # 按 masking 策略 hash/mask，防止原始个人数据经消费链路明文外泄。
        if self.is_pii(metric):
            result.rows = self._mask_result_rows(metric, result.rows)
        plan = {
            "metric_code": req.metric_code,
            "dimensions": [d.model_dump() for d in req.dimensions],
            "date_range": req.date_range,
        }
        # P2-6：degraded 此前硬编码 False——MySQL 降级结果仍报非降级，消费方误判
        # 数据质量。现按实际引擎判定：engine=mysql（OLAP 降级）即 degraded=True。
        response = QueryResponse(
            metric_code=req.metric_code,
            degraded=engine_used == "mysql",
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

        # 消费方血缘注册（best-effort，不阻断查询响应）：记录「该接入方消费了此指标」
        if client is not None:
            await self._register_consumer_lineage(req.metric_code, client.client_id)

        return response

    async def _register_consumer_lineage(self, metric_code: str, client_id: str) -> None:
        """消费成功后注册消费方血缘边（CONSUMED_BY，best-effort，不阻断响应）。

        写入 ``metric:{metric_code} → consumer:{client_id}`` 边（粒度 L3，
        edge_type=CONSUMED_BY），供血缘图谱/影响分析展示指标被哪些接入方消费。
        复用血缘团队提供的公共 API ``LineageService.register_metric_consumer``
        （按唯一键幂等去重，重复消费不产生重复边）；该接口仅在本模块做 best-effort
        调用，失败仅告警，不影响指标查询响应。

        Args:
            metric_code: 被消费指标的编码。
            client_id: 接入方 client_id（内部用户查询路径 client 为 None，不注册）。
        """
        from app.services.lineage.service import LineageService

        try:
            await LineageService(self._db).register_metric_consumer(metric_code, client_id)
        except Exception:  # noqa: BLE001 - 血缘注册失败绝不阻断消费响应
            logger.warning(
                "consumer_lineage_register_failed",
                metric_code=metric_code,
                client_id=client_id,
                exc_info=True,
            )

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
        from app.services.consume.olap_executor import DorisSqlError

        olap_tried = False
        if settings.olap_url:
            olap_tried = True
            try:
                executor = _get_olap_executor()
                result = await executor.execute(sql, params)
                _observe_query_result(True)
                return result, "olap"
            except DorisSqlError as exc:
                # R-3：Doris SQL 语义/语法错误是用户 SQL 问题——如实上抛（由 API 层转为
                # 400/422），不降级 MySQL 重跑（掩盖问题且浪费）。区别于引擎故障的降级路径。
                _observe_query_result(False)
                raise BusinessError(
                    f"OLAP 查询 SQL 错误: {exc}",
                    error_code=ErrorCode.QUERY_SQL_ERROR,
                ) from exc
            except Exception as exc:
                logger.warning("olap_execute_failed_fallback_mysql", error=str(exc))

        mysql_executor = _get_mysql_executor()
        if mysql_executor.enabled:
            try:
                result = await mysql_executor.execute(sql, params)
                _observe_query_result(True)
                return result, "mysql"
            except Exception as exc:
                logger.warning("mysql_fallback_failed", error=str(exc))
                fire_degradation_event("OLAP", "olap", "DEGRADED", "engine_unavailable")
                _observe_query_result(False)
                raise BusinessError(
                    "查询执行引擎不可用，查询降级",
                    error_code=ErrorCode.DEPENDENCY_DEGRADED_ENGINE,
                    ctx={"retry_after": 30, "accept_stale": req.accept_stale},
                ) from exc

        # 无任何可用引擎
        reason = "olap_failed" if olap_tried else "olap_not_configured"
        fire_degradation_event("OLAP", "olap", "DEGRADED", reason)
        _observe_query_result(False)
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
        signature = _dims_signature(dims)
        # WORM 去重：同口径（metric/version/date_range/dims 签名）已存在则跳过，
        # 防"同口径重复快照"（模型唯一索引 uk_snapshot_metric_version_range_dims
        # 兜底并发竞态，IntegrityError 视为已存在返回）。
        existing = await self._snapshots.get_by_unique(
            metric_code, version, date_range, signature
        )
        if existing is not None:
            return self._to_snap(existing)
        snap = MetricValueSnapshot(
            metric_code=metric_code,
            version=version,
            dims=dims,
            dims_signature=signature,
            date_range=date_range,
            value_json=value_json,
            quality_flag=quality_flag,
            generated_at=generated_at,
            generated_by=generated_by,
        )
        try:
            await self._snapshots.create(snap)
        except Exception:
            # 唯一键并发竞态：另一请求已写入同口径快照 → 回读返回（WORM 不重复落库）
            logger.info(
                "snapshot_unique_conflict_skip",
                metric_code=metric_code,
                version=version,
                exc_info=True,
            )
            existing = await self._snapshots.get_by_unique(
                metric_code, version, date_range, signature
            )
            if existing is not None:
                return self._to_snap(existing)
            raise
        return self._to_snap(snap)

    async def list_snapshots(
        self, metric_code: str, limit: int, offset: int
    ) -> list[SnapshotResponse]:
        rows = await self._snapshots.list_by_metric(metric_code, limit, offset)
        metric = await self._get_metric(metric_code)
        # PII 数据值脱敏（C-3）：快照含 PII 维度列时同样脱敏，防止历史明文外泄
        if metric is not None and self.is_pii(metric):
            pii_cols = self._metric_pii_columns(metric)
            policy = self._metric_masking_policy(metric)
            for r in rows:
                vj = r.value_json if isinstance(r.value_json, dict) else {}
                if isinstance(vj.get("rows"), list):
                    vj["rows"] = mask_rows(vj["rows"], pii_cols, policy)
                    r.value_json = vj
        return [self._to_snap(r) for r in rows]

    async def list_snapshots_for_internal(
        self, metric_code: str, limit: int, offset: int, user: User
    ) -> list[SnapshotResponse]:
        """内部登录用户读快照：走 PDP 数据权限闸门（对齐 execute_query internal 通道）。

        D-2 修复：此前快照端点无任何 PDP/域校验，任意登录用户可凭 code 跨域读取
        任意指标的历史查询数据值——现接入 ``check_internal_read_permission``，
        platform_admin 直通 / 本域角色按 ROLE_ACTIONS / 跨域须命中 ACTIVE grants。
        """
        decision, _matched = await GovernanceService(self._db).check_internal_read_permission(
            user, metric_code
        )
        if not decision.allow:
            raise BusinessError(
                decision.reason or "无权限查看该指标消费快照",
                error_code=decision.error_code or ErrorCode.FORBIDDEN,
                ctx={"metric_code": metric_code, "actor_id": user.id},
            )
        return await self.list_snapshots(metric_code, limit, offset)

    async def list_snapshots_for_client(
        self, metric_code: str, limit: int, offset: int, client: ApiClient
    ) -> list[SnapshotResponse]:
        """消费方（X-Api-Key / consume Bearer）读快照：走接入方四级鉴权（对齐 execute_query client 通道）。

        D-2 修复：此前快照端点无 scope_domain/白名单/PII 校验，接入方可跨域读取任意
        指标历史查询数据值——现复用 ``_assert_authorized``（域 → 白名单 → PII 四级，
        fail-closed），未经授权一律 FORBIDDEN。
        """
        metric = await self._get_metric(metric_code)
        if metric is None:
            raise NotFoundError("指标不存在", error_code=ErrorCode.NOT_FOUND)
        self._assert_authorized(client, metric)
        return await self.list_snapshots(metric_code, limit, offset)

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
    async def _get_my_confirmation(
        self, metric_id: int, version: int, user_id: int
    ) -> PendingVersionConfirmation | None:
        """按 (指标, 版本, 消费方) 查确认记录——归属校验的依据。

        确认/拒绝 PENDING 版本前必须校验调用者是该版本的确认消费方，
        否则任意用户可确认/拒绝他人的版本（IDOR 越权）。
        """
        stmt = select(PendingVersionConfirmation).where(
            PendingVersionConfirmation.metric_id == metric_id,
            PendingVersionConfirmation.version == version,
            PendingVersionConfirmation.consumer_id == user_id,
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def _get_metric_by_id(self, metric_id: int) -> Metric | None:
        stmt = select(Metric).where(
            Metric.id == metric_id, Metric.deleted_at.is_(None)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def confirm_version(self, version_id: int, user_id: int) -> None:
        """消费方确认版本（完整转正）。

        修复前：仅简易直改版本状态 PUBLISHED（不应用版本口径到主表、不递增
        版本、不清 PENDING、不更新血缘/通知），且不校验消费方归属（任意用户
        可确认任意版本）——确认后新口径仍不生效（双实现不一致 + IDOR 越权）。

        现委托语义模块 MetricService.confirm_version 走完整转正
        （归属校验 + 主表口径同步 + 版本递增 + 血缘注册 + 通知 + 审计）。
        """
        mv = await self._get_version(version_id)
        if mv is None:
            raise NotFoundError(f"版本 {version_id} 不存在")
        # 对齐语义模块 VersionStatusEnum（T002）：待确认状态为 PENDING_CONFIRMATION
        if mv.status != "PENDING_CONFIRMATION":
            raise ConflictError("版本不在待确认状态")
        # 归属校验：仅该版本的确认消费方可确认（防 IDOR——此前 user_id 未使用）
        if await self._get_my_confirmation(mv.metric_id, mv.version, user_id) is None:
            raise ConflictError(
                "您不是该版本的确认消费方，无权确认",
                error_code="NO_PENDING_CONFIRMATION",
            )
        metric = await self._get_metric_by_id(mv.metric_id)
        if metric is None:
            raise NotFoundError(f"指标不存在: id={mv.metric_id}")
        from app.services.semantic.service import MetricService

        await MetricService(self._db).confirm_version(
            metric.metric_code, mv.version, consumer_id=user_id
        )

    async def reject_version(self, version_id: int, user_id: int, reason: str | None) -> None:
        """消费方拒绝版本（完整取消）。

        修复前：仅简易直改版本状态 ARCHIVED（不校验归属、不经完整拒绝流程）——
        任意用户可拒绝任意版本（IDOR），且拒绝后版本滞留非 CANCELLED。

        现委托语义模块 MetricService.reject_version 走完整拒绝流程
        （归属校验 + 版本 CANCELLED + 终结该版本全部确认记录 + 审计）。
        """
        mv = await self._get_version(version_id)
        if mv is None:
            raise NotFoundError(f"版本 {version_id} 不存在")
        # 对齐语义模块 VersionStatusEnum（T002）：待确认状态为 PENDING_CONFIRMATION
        if mv.status != "PENDING_CONFIRMATION":
            raise ConflictError("版本不在待确认状态")
        # 归属校验：仅该版本的确认消费方可拒绝（防 IDOR）
        if await self._get_my_confirmation(mv.metric_id, mv.version, user_id) is None:
            raise ConflictError(
                "您不是该版本的确认消费方，无权拒绝",
                error_code="NO_PENDING_CONFIRMATION",
            )
        metric = await self._get_metric_by_id(mv.metric_id)
        if metric is None:
            raise NotFoundError(f"指标不存在: id={mv.metric_id}")
        from app.services.semantic.service import MetricService

        await MetricService(self._db).reject_version(
            metric.metric_code, mv.version, reason or "", consumer_id=user_id
        )

    # ---- helpers ----
    async def _get_bound_dimensions(self, metric_id: int | None) -> set[str]:
        """查 metric_dimension 绑定表，取该指标已绑定维度编码集合（消费校验补充来源）。

        与 ``definition_json.dimensions`` 取并集，使「维度管理-绑定指标」真正对消费链路
        生效（此前绑定表是信息孤岛）。``metric_id`` 为 None（脱库/异常）时返回空集，
        降级为仅 definition_json 来源，向后兼容。
        """
        if metric_id is None:
            return set()
        stmt = select(MetricDimension.dim_code).where(
            MetricDimension.metric_id == metric_id
        )
        result = await self._db.execute(stmt)
        rows = result.scalars().all()
        return {row for row in rows if row}

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

    @staticmethod
    def _metric_pii_columns(metric: Metric) -> list[str]:
        """指标口径声明的 PII 列名（definition_json.pii_fields，兼容字符串/字典两种形态）。"""
        dj = metric.definition_json or {}
        fields = dj.get("pii_fields") or []
        columns: list[str] = []
        for f in fields:
            if isinstance(f, dict):
                col = f.get("column") or f.get("name")
                if col:
                    columns.append(str(col))
            elif isinstance(f, str):
                columns.append(f)
        return columns

    @staticmethod
    def _metric_masking_policy(metric: Metric) -> str:
        """指标脱敏策略：口径显式声明优先，缺省按 PII 敏感级推导（hash）。"""
        dj = metric.definition_json or {}
        explicit = (dj.get("masking_policy") or "").strip().lower()
        if explicit in ("none", "mask", "hash", "deny"):
            return explicit
        return "hash"

    @classmethod
    def _mask_result_rows(
        cls, metric: Metric, rows: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        """对指标查询结果中的 PII 列应用脱敏（mask/hash/deny，none 原样）。"""
        from app.services.consume.masking import mask_rows

        return mask_rows(
            rows,
            cls._metric_pii_columns(metric),
            cls._metric_masking_policy(metric),
        )

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

    # ---- 响应时效 KPI（P1）----

    async def record_query_log(
        self,
        *,
        metric_code: str,
        requester_type: QueryRequesterType,
        requester_id: str,
        requester_name: str | None,
        duration_ms: int,
        status: str,
        error_code: str | None = None,
    ) -> None:
        """落一条提数查询日志（响应时效 KPI 数据源），best-effort 绝不阻断响应。

        独立 try/except + rollback：插入失败仅告警，不影响查询主链路返回。
        调用方应在业务事务提交后调用，日志独立成事务。
        """
        try:
            self._db.add(
                QueryLog(
                    metric_code=metric_code,
                    requester_type=requester_type,
                    requester_id=requester_id,
                    requester_name=requester_name,
                    duration_ms=duration_ms,
                    status=status,
                    error_code=error_code,
                )
            )
            await self._db.commit()
        except Exception:
            logger.warning(
                "query_log_record_failed",
                metric_code=metric_code,
                exc_info=True,
            )
            await self._db.rollback()

    @staticmethod
    def _percentile(sorted_values: list[int], p: float) -> int:
        """线性插值分位数（p ∈ (0,100]），输入须为升序列表；空列表返回 0。"""
        if not sorted_values:
            return 0
        if len(sorted_values) == 1:
            return sorted_values[0]
        rank = (len(sorted_values) - 1) * p / 100.0
        lo = int(rank)
        hi = min(lo + 1, len(sorted_values) - 1)
        frac = rank - lo
        return int(sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo]))

    async def response_time_stats(self, days: int = 7) -> dict[str, Any]:
        """响应时效统计：按天聚合提数查询 avg/p95/p99/max 与错误数。

        MySQL 8 无窗口分位数函数，故拉取近 N 天明细在内存线性插值分位数
        （查询日志量级可控，避免过度工程）。
        """
        days = max(1, min(int(days), 90))
        since = (datetime.now(UTC) - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        rows = (
            await self._db.execute(
                select(QueryLog.created_at, QueryLog.duration_ms, QueryLog.status)
                .where(QueryLog.created_at >= since)
                .order_by(QueryLog.created_at)
            )
        ).all()
        by_day: dict[str, list[int]] = {}
        errors: dict[str, int] = {}
        for created_at, duration_ms, status in rows:
            day = created_at.astimezone(UTC).date().isoformat()
            by_day.setdefault(day, []).append(int(duration_ms or 0))
            if status == "error":
                errors[day] = errors.get(day, 0) + 1
        items: list[dict[str, Any]] = []
        for i in range(days):
            day = (since + timedelta(days=i)).date().isoformat()
            vals = sorted(by_day.get(day, []))
            items.append(
                {
                    "date": day,
                    "count": len(vals),
                    "avg_ms": int(sum(vals) / len(vals)) if vals else 0,
                    "p95_ms": self._percentile(vals, 95),
                    "p99_ms": self._percentile(vals, 99),
                    "max_ms": vals[-1] if vals else 0,
                    "error_count": errors.get(day, 0),
                }
            )
        return {"days": days, "items": items}


def _dims_signature(dims: dict[str, Any]) -> str:
    """维度组合的确定性签名（同口径唯一键承载）。

    sorted JSON（ensure_ascii=False + 紧凑分隔符）保证「同维度组合不同键序/
    空格」产出相同签名；sha1 摘要前 32 位承载 ``metric_value_snapshot`` 的
    ``dims_signature`` 唯一索引（JSON 列不能直接建 MySQL 唯一索引）。
    """
    canonical = json.dumps(
        dims, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:32]
