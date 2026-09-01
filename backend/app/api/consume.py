"""consume 层 API 路由（TD §12.6 / FR-12,13）。

路由前缀约定：与已注册路由一致，在 main.py 以 prefix="/api/v1" 挂载。
鉴权两类：
- 消费方 X-Api-Key：get_consume_client 依赖（接入方鉴权 + 限流闸门）。
- 平台/域管理员：require_roles 守卫接入方 CRUD、版本确认回调。
- 普通用户：get_current_user 守卫收藏（me/favorites）。
对齐 DEV_GUIDE §3 与非功能性（审计 write_audit、RBAC require_roles、注入守卫）。
统一响应信封 ok()（对齐 DEV_GUIDE §8）。
"""

from __future__ import annotations

import secrets
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import write_audit
from app.core.error_codes import ErrorCode
from app.core.exceptions import BusinessError, ConflictError, ValidationError
from app.core.guard import guard_against_injection
from app.core.security import create_access_token, hash_password
from app.db.mysql import get_db_session
from app.models.consume import (
    ApiClient,
    ApiClientStatus,
    FavoriteAssetType,
    QueryRequesterType,
)
from app.models.user import User
from app.services.consume.repository import ApiClientRepo
from app.services.consume.schemas import (
    ClientBatchRequest,
    ClientCreatedResponse,
    ClientCreateRequest,
    ClientResponse,
    ClientStatusRequest,
    ClientUpdateRequest,
    DryRunResponse,
    FavoriteRequest,
    FavoriteResponse,
    QueryRequest,
    QueryResponse,
    RejectRequest,
    SnapshotResponse,
    TokenIssueRequest,
)
from app.services.consume.service import ConsumeService

router = APIRouter(tags=["consume"], dependencies=[Depends(guard_against_injection)])


async def _authenticate_consume(
    db: AsyncSession,
    api_key: str | None = None,
    authorization: str | None = None,
) -> ApiClient:
    """消费方鉴权核心：优先 Bearer 消费方 JWT，其次 X-Api-Key（client_id:secret）。

    两种方式均走接入方校验 + 限流闸门。Bearer 令牌由平台/域管理员
    经 ``POST /consume/api-clients/{id}/token`` 换发（TD §5.1），
    供平台内 QueryWorkspace 调试使用；外部消费方沿用 X-Api-Key。
    """
    svc = ConsumeService(db)
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if not token:
            raise BusinessError("缺少消费令牌", error_code=ErrorCode.AUTH_APIKEY_MISSING)
        client = await svc.authenticate_consume_token(token)
        await svc.check_rate_limit(client)
        return client
    if not api_key:
        raise BusinessError("缺少 X-Api-Key 头", error_code=ErrorCode.AUTH_APIKEY_MISSING)
    client = await svc.authenticate_client(api_key)
    await svc.check_rate_limit(client)
    return client


async def get_consume_client(
    api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    db: AsyncSession = Depends(get_db_session),
) -> ApiClient:
    """消费方鉴权依赖（严格通道：仅 X-Api-Key / consume Bearer）。"""
    return await _authenticate_consume(db=db, api_key=api_key, authorization=authorization)


async def get_consume_or_internal_user(
    api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    db: AsyncSession = Depends(get_db_session),
) -> ApiClient | User:
    """消费数据只读双通道鉴权依赖（任一通过即可）。

    FastAPI 的 ``Depends`` 为“且”关系，故自建“或”逻辑：
    - 消费方：X-Api-Key（client_id:secret）或 consume Bearer token（QueryWorkspace / 外部接入方）。
    - 内部登录用户：标准用户 JWT（指标详情 UI 展示消费快照，只读，不经过接入方限流）。

    任一通道失败不阻断另一通道：前端会携带全局 X-Api-Key 默认头（semantic 域密钥），
    对 consume 域无效时需回落到用户 JWT 通道；用户 JWT 本身即为有效凭证。
    """
    credentials: HTTPAuthorizationCredentials | None = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token:
            credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    # 优先消费方通道
    if api_key or credentials is not None:
        try:
            return await _authenticate_consume(db=db, api_key=api_key, authorization=authorization)
        except BusinessError as exc:
            if exc.error_code == ErrorCode.RATE_LIMITED:
                # 限流/配额耗尽：fail-closed，禁止回落用户通道（否则限流保护被绕过）。
                raise
            if credentials is None:
                # 无用户 JWT 兜底：消费方凭证明确但无效/吊销 → fail-closed 抛出。
                raise
            # 存在用户 JWT：consume 凭证对 consume 域无效（如前端全局 X-Api-Key 默认头）
            # 或用户 JWT 非 consume 角色 → 回落登录用户通道。
    # 回落到内部登录用户只读通道
    if credentials is not None:
        return await get_current_user(db, credentials)
    raise BusinessError("缺少认证凭证", error_code=ErrorCode.AUTH_APIKEY_MISSING)


@router.post("/consume/query/dry-run", response_model=ApiResponse[DryRunResponse])
async def dry_run(
    req: QueryRequest,
    _auth: ApiClient | User = Depends(get_consume_or_internal_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[DryRunResponse]:
    """dry-run：口径校验 + 执行计划 + 元信息标注（不执行/不写/不计费/不缓存）。

    双通道：接入方（X-Api-Key / consume Bearer）或内部登录用户（用户 JWT）。
    """
    svc = ConsumeService(db)
    if hasattr(_auth, "client_id"):
        data = await svc.dry_run_query(req, client=_auth)
    else:
        data = await svc.dry_run_query(req, internal_user=_auth)
    return ok(data=data)


@router.post("/consume/query", response_model=ApiResponse[QueryResponse])
async def query(
    req: QueryRequest,
    _auth: ApiClient | User = Depends(get_consume_or_internal_user),
    db: AsyncSession = Depends(get_db_session),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[QueryResponse]:
    """语义查询（双通道）：OLAP 不可用时降级 503；成功则返回执行计划 + 元信息并写审计。

    - 接入方（X-Api-Key / consume Bearer）：走接入方四级闸门 + 限流。
    - 内部登录用户（用户 JWT）：走 PDP 数据权限 + PII 合规复核闸门。

    响应时效 KPI：真实查询耗时在 API 层计时，成功/失败均 best-effort 落
    ``query_log``（独立事务，失败不阻断响应）。
    """
    svc = ConsumeService(db)
    is_client = hasattr(_auth, "client_id")
    requester_type = QueryRequesterType.API_CLIENT if is_client else QueryRequesterType.INTERNAL
    requester_id = _auth.client_id if is_client else str(_auth.id)
    requester_name = _auth.client_id if is_client else _auth.username
    action = "metric.query" if is_client else "metric.query_internal"
    start = time.perf_counter()
    try:
        if is_client:
            res = await svc.execute_query(req, client=_auth)
        else:
            res = await svc.execute_query(req, internal_user=_auth)
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        await svc.record_query_log(
            metric_code=req.metric_code,
            requester_type=requester_type,
            requester_id=requester_id,
            requester_name=requester_name,
            duration_ms=duration_ms,
            status="error",
            error_code=getattr(exc, "error_code", None) or type(exc).__name__,
        )
        raise
    duration_ms = int((time.perf_counter() - start) * 1000)
    # PII 数据分级审计（对齐 TD §15.4：PII 访问必须留痕 data_classification=PII）
    is_pii = bool((res.meta or {}).get("pii", False))
    detail: dict[str, Any] = {"data_classification": "PII" if is_pii else "INTERNAL"}
    if is_client:
        # 执行方为接入方本体（client.id），而非其创建者（created_by），避免审计归属伪造（PLAT-2）。
        detail["client"] = _auth.client_id
    await write_audit(
        db,
        actor_id=_auth.id,
        action=action,
        entity_type="metric",
        entity_id=req.metric_code,
        detail=detail,
        trace_id=trace_id,
        pii_access=is_pii,
    )
    await db.commit()
    await svc.record_query_log(
        metric_code=req.metric_code,
        requester_type=requester_type,
        requester_id=requester_id,
        requester_name=requester_name,
        duration_ms=duration_ms,
        status="ok",
    )
    return ok(data=res)


@router.post("/consume/metrics/{code}/query", response_model=ApiResponse[QueryResponse])
async def query_metric_internal(
    code: str,
    req: QueryRequest,
    user: User = Depends(require_roles("platform_admin", "domain_admin", "metric_owner")),
    db: AsyncSession = Depends(get_db_session),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[QueryResponse]:
    """内部用户查询（资产地图/指标详情「查询最新数据」专用）。

    真实执行指标口径（OLAP 优先、MySQL 降级），成功后自动落 WORM 快照；
    保留指标状态与 PII 合规复核闸门，跳过接入方白名单/域校验（平台内读操作）。

    RBAC 与前端 ``query:execute`` 权限点对齐（platform_admin/domain_admin/metric_owner）：
    「执行查询」是特权动作（真实 OLAP 执行 + 快照写副作用），viewer/analyst 需通过
    consume 客户端令牌通道（POST /consume/query）消费数据，而非内部查询端点。
    """
    merged = req.model_copy(update={"metric_code": code})
    svc = ConsumeService(db)
    start = time.perf_counter()
    try:
        res = await svc.execute_query(merged, internal_user=user)
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        await svc.record_query_log(
            metric_code=code,
            requester_type=QueryRequesterType.INTERNAL,
            requester_id=str(user.id),
            requester_name=user.username,
            duration_ms=duration_ms,
            status="error",
            error_code=getattr(exc, "error_code", None) or type(exc).__name__,
        )
        raise
    duration_ms = int((time.perf_counter() - start) * 1000)
    # PII 数据分级审计（对齐 TD §15.4：PII 访问必须留痕 data_classification=PII）
    is_pii = bool((res.meta or {}).get("pii", False))
    await write_audit(
        db,
        actor_id=user.id,
        action="metric.query_internal",
        entity_type="metric",
        entity_id=code,
        detail={"data_classification": "PII" if is_pii else "INTERNAL"},
        trace_id=trace_id,
        pii_access=is_pii,
    )
    await db.commit()
    await svc.record_query_log(
        metric_code=code,
        requester_type=QueryRequesterType.INTERNAL,
        requester_id=str(user.id),
        requester_name=user.username,
        duration_ms=duration_ms,
        status="ok",
    )
    return ok(data=res)


async def _generate_client_id(repo: ApiClientRepo) -> str:
    """自动生成接入方 ID：``app_`` + 随机 hex，冲突重试（上限 10 次）。"""
    for _ in range(10):
        candidate = f"app_{secrets.token_hex(4)}"
        if await repo.get_by_client_id(candidate) is None:
            return candidate
    raise ConflictError("无法生成唯一接入方 ID，请重试")


@router.post("/consume/api-clients", response_model=ApiResponse[ClientCreatedResponse])
async def create_client(
    req: ClientCreateRequest,
    user: User = Depends(require_roles("platform_admin", "domain_admin")),
    db: AsyncSession = Depends(get_db_session),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[ClientCreatedResponse]:
    """平台管理员创建接入方（secret 仅此一次明文返回）。"""
    repo = ApiClientRepo(db)
    # 编码自动生成（FR-010：缺省时由系统生成 app_ 前缀，避免人为创造）
    if not req.client_id:
        req.client_id = await _generate_client_id(repo)
    client = ApiClient(
        client_id=req.client_id,
        client_secret_ref=await hash_password(req.secret),
        scope_domain=req.scope_domain,
        metric_whitelist=req.metric_whitelist,
        qps=req.qps,
        daily_quota=req.daily_quota,
        status=ApiClientStatus.ACTIVE,
        created_by=user.id,
    )
    created = await repo.create(client)
    await write_audit(
        db,
        actor_id=user.id,
        action="api_client.create",
        entity_type="api_client",
        entity_id=created.client_id,
        detail={"scope_domain": created.scope_domain},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=ClientCreatedResponse(
            client_id=created.client_id,
            scope_domain=created.scope_domain,
            metric_whitelist=created.metric_whitelist,
            qps=created.qps,
            daily_quota=created.daily_quota,
            status=created.status,
            secret=req.secret,
        )
    )


@router.get("/consume/api-clients", response_model=ApiResponse[list[ClientResponse]])
async def list_clients(
    domain: str | None = Query(default=None),
    user: User = Depends(require_roles("platform_admin", "domain_admin")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[list[ClientResponse]]:
    repo = ApiClientRepo(db)
    rows = await repo.list(domain, 100, 0)
    return ok(
        data=[
            ClientResponse(
                client_id=r.client_id,
                scope_domain=r.scope_domain,
                metric_whitelist=r.metric_whitelist,
                qps=r.qps,
                daily_quota=r.daily_quota,
                status=r.status,
            )
            for r in rows
        ]
    )


@router.put("/consume/api-clients/{client_id}", response_model=ApiResponse[ClientResponse])
async def update_client(
    client_id: str,
    req: ClientUpdateRequest,
    user: User = Depends(require_roles("platform_admin", "domain_admin")),
    db: AsyncSession = Depends(get_db_session),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[ClientResponse]:
    """编辑接入方授权配置（secret 不可改；None 字段表示不修改）。

    修复 403 痛点：授权域/白名单创建时填错可在此修正，无需重建客户端（重建会换密钥）。
    """
    repo = ApiClientRepo(db)
    row = await repo.get_by_client_id(client_id)
    if row is None:
        raise BusinessError("接入方不存在或已删除", error_code=ErrorCode.NOT_FOUND)
    # 部分更新语义：None 不修改；scope_domain 空串 → 清空为不限域；白名单空数组 → 清空为域内全量
    if req.scope_domain is not None:
        row.scope_domain = req.scope_domain or None
    if req.metric_whitelist is not None:
        row.metric_whitelist = req.metric_whitelist or None
    if req.qps is not None:
        row.qps = req.qps
    if req.daily_quota is not None:
        row.daily_quota = req.daily_quota
    await db.flush()
    await write_audit(
        db,
        actor_id=user.id,
        action="api_client.update",
        entity_type="api_client",
        entity_id=client_id,
        detail={
            "scope_domain": row.scope_domain,
            "metric_whitelist_count": len(row.metric_whitelist or []),
            "qps": row.qps,
            "daily_quota": row.daily_quota,
        },
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=_client_response(row))


@router.patch("/consume/api-clients/{client_id}/status", response_model=ApiResponse[ClientResponse])
async def set_client_status(
    client_id: str,
    req: ClientStatusRequest,
    user: User = Depends(require_roles("platform_admin", "domain_admin")),
    db: AsyncSession = Depends(get_db_session),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[ClientResponse]:
    """停用/启用接入方（REVOKED 后已签短效令牌随鉴权失效、X-Api-Key 拒绝）。"""
    repo = ApiClientRepo(db)
    row = await repo.update_status(client_id, req.status)
    if row is None:
        raise BusinessError("接入方不存在或已删除", error_code=ErrorCode.NOT_FOUND)
    await write_audit(
        db,
        actor_id=user.id,
        action="api_client.status",
        entity_type="api_client",
        entity_id=client_id,
        detail={"status": req.status.value},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=_client_response(row))


@router.delete("/consume/api-clients/{client_id}", response_model=ApiResponse[dict[str, bool]])
async def delete_client(
    client_id: str,
    user: User = Depends(require_roles("platform_admin", "domain_admin")),
    db: AsyncSession = Depends(get_db_session),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict[str, bool]]:
    """软删接入方（置 deleted_at + REVOKED，保留审计追溯；不物理删除）。"""
    repo = ApiClientRepo(db)
    row = await repo.soft_delete(client_id)
    if row is None:
        raise BusinessError("接入方不存在或已删除", error_code=ErrorCode.NOT_FOUND)
    await write_audit(
        db,
        actor_id=user.id,
        action="api_client.delete",
        entity_type="api_client",
        entity_id=client_id,
        detail={"soft_delete": True},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"deleted": True})


@router.post("/consume/api-clients/batch", response_model=ApiResponse[dict])
async def batch_client_action(
    req: ClientBatchRequest,
    user: User = Depends(require_roles("platform_admin", "domain_admin")),
    db: AsyncSession = Depends(get_db_session),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict]:
    """批量操作接入方（enable/disable/delete），逐条容错返回 BatchResponse 语义。"""
    repo = ApiClientRepo(db)
    rows = await repo.get_many(req.client_ids)
    found = {r.client_id: r for r in rows}
    results: list[dict[str, object]] = []
    ok_count = fail_count = 0
    for cid in req.client_ids:
        row = found.get(cid)
        if row is None:
            results.append(
                {
                    "client_id": cid,
                    "ok": False,
                    "code": "NOT_FOUND",
                    "message": "接入方不存在或已删除",
                }
            )
            fail_count += 1
            continue
        try:
            if req.action.value == "delete":
                row.deleted_at = func.now()
                row.status = ApiClientStatus.REVOKED
            elif req.action.value == "disable":
                row.status = ApiClientStatus.REVOKED
            else:
                row.status = ApiClientStatus.ACTIVE
            await db.flush()
            results.append({"client_id": cid, "ok": True, "status": row.status.value})
            ok_count += 1
        except Exception as exc:  # 逐条容错：单条失败不阻断其余
            results.append(
                {"client_id": cid, "ok": False, "code": "INTERNAL_ERROR", "message": str(exc)}
            )
            fail_count += 1
            await db.rollback()
    await write_audit(
        db,
        actor_id=user.id,
        action=f"api_client.batch_{req.action.value}",
        entity_type="api_client",
        entity_id=",".join(req.client_ids),
        detail={"ok_count": ok_count, "fail_count": fail_count},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data={
            "action": req.action.value,
            "ok_count": ok_count,
            "fail_count": fail_count,
            "results": results,
        }
    )


def _client_response(r: ApiClient) -> ClientResponse:
    """接入方视图序列化（避免端点重复组装）。"""
    return ClientResponse(
        client_id=r.client_id,
        scope_domain=r.scope_domain,
        metric_whitelist=r.metric_whitelist,
        qps=r.qps,
        daily_quota=r.daily_quota,
        status=r.status,
    )


@router.post("/consume/api-clients/{client_id}/token")
async def issue_token(
    client_id: str,
    request: Request,
    user: User = Depends(require_roles("platform_admin", "domain_admin")),
    db: AsyncSession = Depends(get_db_session),
    req: TokenIssueRequest | None = None,
) -> ApiResponse[dict[str, str]]:
    """X-Api-Key 换短效 JWT（后续调用可持 Bearer 使用，对齐 TD §5.1）。

    有效期由调用方指定（5~1440 分钟，默认 60，缺省 body 时用默认值向后兼容）；
    平台内 QueryWorkspace 调试用，外部消费方长期接入推荐 X-Api-Key（无过期）。
    """
    expire_minutes = req.expire_minutes if req is not None else 60
    repo = ApiClientRepo(db)
    client = await repo.get_by_client_id(client_id)
    if client is None or client.status != ApiClientStatus.ACTIVE:
        raise BusinessError("接入方不存在或已吊销", error_code=ErrorCode.AUTH_APIKEY_INVALID)
    token = create_access_token(
        sub=client_id, role="consume", org_id=client.created_by, expire_minutes=expire_minutes
    )
    # S19（审查修复）：接入方凭证签发落审计（此前零审计——凭据签发是敏感操作）
    from app.api.responses import get_trace_id
    from app.core.audit import client_ip, write_audit

    await write_audit(
        db,
        actor_id=user.id,
        action="consume.issue_token",
        entity_type="api_client",
        entity_id=client_id,
        detail={"expire_minutes": expire_minutes},
        ip=client_ip(request),
        trace_id=get_trace_id(request),
    )
    await db.commit()
    return ok(data={"access_token": token})


@router.get("/consume/metrics/{code}/semantic", response_model=ApiResponse[DryRunResponse])
async def get_semantic(
    code: str,
    _auth: ApiClient | User = Depends(get_consume_or_internal_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[DryRunResponse]:
    """只读语义拉取（双通道）：接入方受 scope 约束；内部用户走 PDP 权限。"""
    svc = ConsumeService(db)
    req = QueryRequest(metric_code=code, date_range="")
    if hasattr(_auth, "client_id"):
        data = await svc.dry_run_query(req, client=_auth)
    else:
        data = await svc.dry_run_query(req, internal_user=_auth)
    return ok(data=data)


@router.get("/consume/metrics/{code}/snapshots", response_model=ApiResponse[list[SnapshotResponse]])
async def list_metric_snapshots(
    code: str,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    _auth: ApiClient | User = Depends(get_consume_or_internal_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[list[SnapshotResponse]]:
    """消费快照只读（WORM）：消费方（X-Api-Key / consume Bearer）或内部登录用户均可读。

    D-2 鉴权修复：此前快照端点无 PDP/scope 校验，任意接入方可跨域读取任意指标
    历史查询数据值——现按通道鉴权：
    - 消费方（ApiClient）：走接入方四级闸门（scope_domain → 白名单 → PII → 合规复核）；
    - 内部登录用户（User）：走 PDP 数据权限（平台直通 / 本域 ROLE_ACTIONS / 跨域 grants）。
    """
    svc = ConsumeService(db)
    if hasattr(_auth, "client_id"):
        data = await svc.list_snapshots_for_client(code, limit, offset, _auth)
    else:
        data = await svc.list_snapshots_for_internal(code, limit, offset, _auth)
    return ok(data=data)


@router.get("/consume/me/favorites", response_model=ApiResponse[list[dict[str, str]]])
async def get_favorites(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[list[dict[str, str]]]:
    svc = ConsumeService(db)
    return ok(data=await svc.list_favorites(user.id))


@router.get("/consume/me/favorites/detail", response_model=ApiResponse[list[dict]])
async def get_favorite_details(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[list[dict]]:
    """收藏指标详情聚合（一次查询，消除前端逐条取名 N+1）。"""
    svc = ConsumeService(db)
    return ok(data=await svc.list_favorite_details(user.id))


@router.post("/consume/me/favorites", response_model=ApiResponse[FavoriteResponse])
async def add_favorite(
    req: FavoriteRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[FavoriteResponse]:
    svc = ConsumeService(db)
    res = await svc.add_favorite(user.id, req.asset_type, req.asset_id)
    await db.commit()
    return ok(data=res)


@router.delete(
    "/consume/me/favorites/{asset_type}/{asset_id}",
    response_model=ApiResponse[FavoriteResponse],
)
async def del_favorite(
    asset_type: str,
    asset_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[FavoriteResponse]:
    svc = ConsumeService(db)
    try:
        asset_type_enum = FavoriteAssetType(asset_type)
    except ValueError as exc:
        raise ValidationError(f"不支持的资产类型: {asset_type}") from exc
    res = await svc.remove_favorite(user.id, asset_type_enum, asset_id)
    await db.commit()
    return ok(data=res)


@router.post("/consume/versions/{version_id}/confirm")
async def confirm_version(
    version_id: int,
    user: User = Depends(require_roles("metric_owner", "domain_admin", "platform_admin")),
    db: AsyncSession = Depends(get_db_session),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict[str, bool]]:
    svc = ConsumeService(db)
    await svc.confirm_version(version_id, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="metric_version.confirm",
        entity_type="metric_version",
        entity_id=str(version_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"ok": True})


@router.get("/consume/stats/response-time", response_model=ApiResponse[dict])
async def response_time_stats(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles("platform_admin", "domain_admin")),
    days: int = Query(7, ge=1, le=90),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict]:
    """提数响应时效 KPI：近 N 天查询量/avg/p95/p99/最大耗时/错误数。

    数据源为 ``query_log``（每次真实查询 best-effort 落库）；管理员视角，
    供可观测中心「提数响应时效」卡片消费。
    """
    return ok(data=await ConsumeService(db).response_time_stats(days), trace_id=trace_id)


@router.post("/consume/versions/{version_id}/reject")
async def reject_version(
    version_id: int,
    req: RejectRequest,
    user: User = Depends(require_roles("metric_owner", "domain_admin", "platform_admin")),
    db: AsyncSession = Depends(get_db_session),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict[str, bool]]:
    svc = ConsumeService(db)
    await svc.reject_version(version_id, user.id, req.reason)
    await write_audit(
        db,
        actor_id=user.id,
        action="metric_version.reject",
        entity_type="metric_version",
        entity_id=str(version_id),
        detail={"reason": req.reason},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"ok": True})
