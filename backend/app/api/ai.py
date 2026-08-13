"""AI 问数 API（TD §12.7 / FR-14）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import write_audit
from app.core.exceptions import AuthError
from app.core.feature_flags import is_feature_enabled_or_default
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.ai.schemas import NL2SQLRequest
from app.services.ai.service import AiService

router = APIRouter(prefix="/ai", tags=["ai"])

_WRITE_ROLES = ("metric_owner", "domain_admin", "platform_admin", "analyst", "viewer")
_READ_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]


@router.post("/nl2sql", dependencies=_READ_DEPS)
async def nl2sql(
    payload: NL2SQLRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # OPS-09 特性开关：AI 问数能力可被平台管理员灰度关闭（kill switch）。
    # 此前开关已在 main.py 注册但端点未接线，关闭配置形同虚设。
    if not is_feature_enabled_or_default("ai.nl2sql"):
        raise AuthError(
            "AI 问数能力已被平台管理员关闭",
            error_code="FORBIDDEN",
            ctx={"feature_flag": "ai.nl2sql"},
        )
    svc = AiService(db)
    try:
        result = await svc.ask(
            payload.nl_query, execute=payload.execute, metric_scope=payload.metric_scope
        )
        await write_audit(
            db,
            actor_id=user.id,
            action="ai.nl2sql",
            entity_type="nl_query",
            entity_id=payload.nl_query[:64],
            detail={"safe": result["safe"]},
            trace_id=trace_id,
        )
        # PLAT-3: 审计须提交持久化，否则随会话关闭被回滚（合规审计静默丢失）
        await db.commit()
        return ok(data=result, trace_id=trace_id)
    finally:
        await svc.close()
