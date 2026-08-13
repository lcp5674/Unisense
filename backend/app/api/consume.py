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
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import write_audit
from app.core.error_codes import ErrorCode
from app.core.exceptions import BusinessError
from app.core.guard import guard_against_injection
from app.core.security import create_access_token, hash_password
from app.db.mysql import get_db_session
from app.models.consume import ApiClient, ApiClientStatus
from app.models.user import User
from app.services.consume.repository import ApiClientRepo
from app.services.consume.schemas import (
    ClientCreatedResponse,
    ClientCreateRequest,
    ClientResponse,
    DryRunResponse,
    FavoriteRequest,
    FavoriteResponse,
    QueryRequest,
    QueryResponse,
    RejectRequest,
    SnapshotResponse,
)
from app.services.consume.service import ConsumeService

router = APIRouter(tags=["consume"], dependencies=[Depends(guard_against_injection)])


async def get_consume_client(
    api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    db: AsyncSession = Depends(get_db_session),
) -> ApiClient:
    """消费方鉴权依赖：优先 Bearer 消费方 JWT，其次 X-Api-Key（client_id:secret）。

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


@router.post("/consume/query/dry-run", response_model=ApiResponse[DryRunResponse])
async def dry_run(
    req: QueryRequest,
    client: ApiClient = Depends(get_consume_client),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[DryRunResponse]:
    """dry-run：口径校验 + 执行计划 + 元信息标注（不执行/不写/不计费/不缓存）。"""
    svc = ConsumeService(db)
    return ok(data=await svc.dry_run_query(req, client))


@router.post("/consume/query", response_model=ApiResponse[QueryResponse])
async def query(
    req: QueryRequest,
    client: ApiClient = Depends(get_consume_client),
    db: AsyncSession = Depends(get_db_session),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[QueryResponse]:
    """语义查询：OLAP 不可用时降级 503；成功则返回执行计划 + 元信息并写审计。"""
    svc = ConsumeService(db)
    res = await svc.execute_query(req, client)
    # PII 数据分级审计（对齐 TD §15.4：PII 访问必须留痕 data_classification=PII）
    # 执行方为接入方本体（client.id），而非其创建者（created_by），避免审计归属伪造（PLAT-2）。
    is_pii = bool((res.meta or {}).get("pii", False))
    await write_audit(
        db,
        actor_id=client.id,
        action="consume.query",
        entity_type="metric",
        entity_id=req.metric_code,
        detail={
            "client": client.client_id,
            "data_classification": "PII" if is_pii else "INTERNAL",
        },
        trace_id=trace_id,
        pii_access=is_pii,
    )
    await db.commit()
    return ok(data=res)


async def _generate_client_id(repo: ApiClientRepo) -> str:
    """自动生成接入方 ID：``app_`` + 随机 hex，冲突重试（上限 10 次）。"""
    for _ in range(10):
        candidate = f"app_{secrets.token_hex(4)}"
        if await repo.get_by_client_id(candidate) is None:
            return candidate
    from fastapi import HTTPException

    raise HTTPException(status_code=409, detail="无法生成唯一接入方 ID，请重试")


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
        client_secret_ref=hash_password(req.secret),
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
        action="consume.api_client.create",
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


@router.post("/consume/api-clients/{client_id}/token")
async def issue_token(
    client_id: str,
    user: User = Depends(require_roles("platform_admin", "domain_admin")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[dict[str, str]]:
    """X-Api-Key 换短效 JWT（后续调用可持 Bearer 使用，对齐 TD §5.1）。"""
    repo = ApiClientRepo(db)
    client = await repo.get_by_client_id(client_id)
    if client is None or client.status != ApiClientStatus.ACTIVE:
        raise BusinessError("接入方不存在或已吊销", error_code=ErrorCode.AUTH_APIKEY_INVALID)
    token = create_access_token(
        sub=client_id, role="consume", org_id=client.created_by, expire_minutes=60
    )
    await db.commit()
    return ok(data={"access_token": token})


@router.get("/consume/metrics/{code}/semantic", response_model=ApiResponse[DryRunResponse])
async def get_semantic(
    code: str,
    client: ApiClient = Depends(get_consume_client),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[DryRunResponse]:
    """只读语义拉取（接入方用，受 scope 约束）。"""
    svc = ConsumeService(db)
    return ok(data=await svc.dry_run_query(QueryRequest(metric_code=code, date_range=""), client))


@router.get("/consume/metrics/{code}/snapshots", response_model=ApiResponse[list[SnapshotResponse]])
async def list_metric_snapshots(
    code: str,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    client: ApiClient = Depends(get_consume_client),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[list[SnapshotResponse]]:
    svc = ConsumeService(db)
    return ok(data=await svc.list_snapshots(code, limit, offset))


@router.get("/consume/me/favorites", response_model=ApiResponse[list[str]])
async def get_favorites(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[list[str]]:
    svc = ConsumeService(db)
    return ok(data=await svc.list_favorites(user.id))


@router.post("/consume/me/favorites", response_model=ApiResponse[FavoriteResponse])
async def add_favorite(
    req: FavoriteRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[FavoriteResponse]:
    svc = ConsumeService(db)
    res = await svc.add_favorite(user.id, req.metric_code)
    await db.commit()
    return ok(data=res)


@router.delete("/consume/me/favorites/{code}", response_model=ApiResponse[FavoriteResponse])
async def del_favorite(
    code: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[FavoriteResponse]:
    svc = ConsumeService(db)
    res = await svc.remove_favorite(user.id, code)
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
        action="consume.version.confirm",
        entity_type="metric_version",
        entity_id=str(version_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"ok": True})


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
        action="consume.version.reject",
        entity_type="metric_version",
        entity_id=str(version_id),
        detail={"reason": req.reason},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"ok": True})
