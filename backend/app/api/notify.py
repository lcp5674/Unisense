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
from app.services.notify.schemas import (
    EventLogResponse,
    EventPublish,
    NotificationResponse,
    SubscriptionResponse,
    SubscriptionUpsert,
)
from app.services.notify.service import NotifyService

router = APIRouter(prefix="/notify", tags=["notify"])

_WRITE_ROLES = ("metric_owner", "domain_admin", "platform_admin", "system")
_READ_ROLES = ("metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer", "system")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一挂注入守卫（纵深防御：ORM 参数化兜底之外拦截注入 payload）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]


@router.post("/events", status_code=201, dependencies=_WRITE_DEPS)
async def publish_event(
    payload: EventPublish,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # PLAT-2: 操作人一律以服务端认证身份为准，忽略 client 传入的 actor_id 防止伪造
    payload.actor_id = user.id
    payload.actor_name = None  # 姓名快照由 service 反查，防止 client 伪造展示名
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
    read_state: str | None = Query(None, description="unread=未读 / read=已读"),
    template_code: str | None = Query(None, description="按消息类型过滤"),
    todo_only: bool = Query(False, description="仅待处理类事件"),
    days: int | None = Query(None, ge=1, le=365, description="近 N 天"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    # PLAT-2: 以认证身份 user.id 作为 subscriber，禁止 client 伪造 subscriber_id 越权读取
    notifs, total = await NotifyService(db).list_notifications_page(
        user.id, status, read_state, template_code, todo_only, days, page, page_size
    )
    items = [NotificationResponse.from_model(i) for i in notifs]
    return ok(
        data={"items": items, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.post(
    "/notifications/{notif_id}/read",
    dependencies=_WRITE_DEPS,
)
async def mark_read(
    notif_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await NotifyService(db).mark_read(notif_id, actor_id=user.id, role=user.role)
    await write_audit(
        db,
        actor_id=user.id,
        action="notify.mark_read",
        entity_type="notification",
        entity_id=str(notif_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=NotificationResponse.from_model(resp), trace_id=trace_id)


@router.post(
    "/notifications/read-all",
    dependencies=_WRITE_DEPS,
)
async def mark_all_read(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    updated = await NotifyService(db).mark_all_read(actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="notify.mark_all_read",
        entity_type="notification",
        entity_id="",
        detail={"updated": updated},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"updated": updated}, trace_id=trace_id)


@router.delete(
    "/notifications/{notif_id}",
    dependencies=_WRITE_DEPS,
)
async def delete_notification(
    notif_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    await NotifyService(db).delete_notification(notif_id, actor_id=user.id, role=user.role)
    await write_audit(
        db,
        actor_id=user.id,
        action="notify.delete",
        entity_type="notification",
        entity_id=str(notif_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"deleted": True}, trace_id=trace_id)


@router.delete(
    "/notifications",
    dependencies=_WRITE_DEPS,
)
async def delete_all_notifications(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    deleted = await NotifyService(db).delete_all(actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="notify.delete_all",
        entity_type="notification",
        entity_id="",
        detail={"deleted": deleted},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"deleted": deleted}, trace_id=trace_id)


@router.post(
    "/notifications/{notif_id}/sent",
    dependencies=_WRITE_DEPS,
)
async def mark_sent(
    notif_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await NotifyService(db).mark_sent(notif_id, actor_id=user.id, role=user.role)
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
    return ok(data=NotificationResponse.from_model(resp), trace_id=trace_id)


@router.post(
    "/notifications/{notif_id}/failed",
    dependencies=_WRITE_DEPS,
)
async def mark_failed(
    notif_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await NotifyService(db).mark_failed(notif_id, actor_id=user.id, role=user.role)
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
    return ok(data=NotificationResponse.from_model(resp), trace_id=trace_id)


@router.put("/subscriptions", dependencies=_WRITE_DEPS)
async def upsert_subscription(
    payload: SubscriptionUpsert,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # PLAT-2: 以认证身份 user.id 覆盖 client 传入的 user_id，杜绝越权绑定
    resp = await NotifyService(db).upsert_subscription(payload, actor_id=user.id)
    await db.commit()
    return ok(data=SubscriptionResponse.from_model(resp), trace_id=trace_id)


@router.get("/subscriptions", dependencies=_READ_DEPS)
async def list_subscriptions(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # PLAT-2: 以认证身份 user.id 查询，禁止 client 伪造 user_id 越权读取
    subs = await NotifyService(db).list_subscriptions(user.id)
    items = [SubscriptionResponse.from_model(i) for i in subs]
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


@router.get("/events", dependencies=_READ_DEPS)
async def list_event_logs(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    event_type: str | None = Query(None),
    limit: int = Query(100),
) -> Any:
    logs = await NotifyService(db).list_event_logs(event_type, limit)
    items = [EventLogResponse.from_model(i) for i in logs]
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)
