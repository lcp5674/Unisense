"""通知服务 API（TD §12.9 / FR-16 / FR-17）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import write_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.notify.schemas import EventPublish, SubscriptionUpsert
from app.services.notify.service import NotifyService

router = APIRouter(prefix="/notify", tags=["notify"])

_WRITE_ROLES = ("metric_owner", "domain_admin", "platform_admin", "system")
_READ_ROLES = ("metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer", "system")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]


@router.post("/events", status_code=201, dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def publish_event(
    payload: EventPublish,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await NotifyService(db).publish_event(payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="notify.publish",
        entity_type="event_log",
        entity_id=str(resp["event_id"]),
        detail={},
        trace_id=trace_id,
    )
    # PLAT-3: 审计与业务写入同一事务原子提交，避免审计丢失/不一致
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.get("/notifications", dependencies=_READ_DEPS)
async def list_notifications(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    status: str | None = Query(None),
) -> Any:
    # PLAT-2: 以认证身份 user.id 作为 subscriber，禁止 client 伪造 subscriber_id 越权读取
    items = await NotifyService(db).list_notifications(user.id, status)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


@router.post(
    "/notifications/{notif_id}/sent",
    dependencies=[Depends(require_roles(*_WRITE_ROLES))],
)
async def mark_sent(
    notif_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await NotifyService(db).mark_sent(notif_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="notify.mark_sent",
        entity_type="notification",
        entity_id=str(notif_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post(
    "/notifications/{notif_id}/failed",
    dependencies=[Depends(require_roles(*_WRITE_ROLES))],
)
async def mark_failed(
    notif_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await NotifyService(db).mark_failed(notif_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="notify.mark_failed",
        entity_type="notification",
        entity_id=str(notif_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.put("/subscriptions", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def upsert_subscription(
    payload: SubscriptionUpsert,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # PLAT-2: 以认证身份 user.id 覆盖 client 传入的 user_id，杜绝越权绑定
    resp = await NotifyService(db).upsert_subscription(payload, actor_id=user.id)
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.get("/subscriptions", dependencies=_READ_DEPS)
async def list_subscriptions(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # PLAT-2: 以认证身份 user.id 查询，禁止 client 伪造 user_id 越权读取
    items = await NotifyService(db).list_subscriptions(user.id)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


@router.get("/events", dependencies=_READ_DEPS)
async def list_event_logs(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    event_type: str | None = Query(None),
    limit: int = Query(100),
) -> Any:
    items = await NotifyService(db).list_event_logs(event_type, limit)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)
