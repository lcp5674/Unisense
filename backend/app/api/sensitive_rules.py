"""敏感规则配置台 API（方案 A：规则引擎可视化配置）。

端点（/api/v1/sensitive-rules）：
- GET    ``/``                    规则列表（内置+自定义合并，含来源/状态）
- GET    ``/categories``          类别目录（PII 12 类 + 机密 3 类）
- POST   ``/``                    新增自定义规则（201）
- PUT    ``/{rule_id}``           更新规则（无 DB 项时创建为自定义覆盖）
- PATCH  ``/{rule_id}/status``    启用 / 停用规则
- DELETE ``/{rule_id}``           删除自定义规则（回退内置）
- POST   ``/validate-regex``      正则合法性校验（保存前即时反馈）
- POST   ``/test``                规则测试台（用当前生效规则模拟识别）

权限：读=全部已登录角色；写=platform_admin + compliance_officer（治理动作）。
审计：全部写操作落 ``audit_log``。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.sensitive_rules.schemas import (
    CategoryItem,
    RegexCheckRequest,
    RegexCheckResponse,
    RuleTestRequest,
    RuleTestResponse,
    SensitiveRuleCreate,
    SensitiveRuleItem,
    SensitiveRuleUpsert,
)
from app.services.sensitive_rules.service import SensitiveRuleService

router = APIRouter(prefix="/sensitive-rules", tags=["敏感规则配置"])

_WRITE_ROLES = ("platform_admin", "compliance_officer")
_READ_DEPS = [Depends(require_roles(*ALL_ROLES)), Depends(guard_against_injection)]
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]


def _svc(db: AsyncSession = Depends(get_db_session)) -> SensitiveRuleService:
    return SensitiveRuleService(db)


@router.get("", response_model=ApiResponse[list[SensitiveRuleItem]], dependencies=_READ_DEPS)
async def list_rules(
    svc: SensitiveRuleService = Depends(_svc),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[list[SensitiveRuleItem]]:
    return ok(data=await svc.list_rules(), trace_id=trace_id)


@router.get(
    "/categories",
    response_model=ApiResponse[list[CategoryItem]],
    dependencies=_READ_DEPS,
)
async def list_categories(
    svc: SensitiveRuleService = Depends(_svc),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[list[CategoryItem]]:
    return ok(data=svc.list_categories(), trace_id=trace_id)


@router.post(
    "/validate-regex",
    response_model=ApiResponse[RegexCheckResponse],
    dependencies=_READ_DEPS,
)
async def validate_regex(
    payload: RegexCheckRequest,
    svc: SensitiveRuleService = Depends(_svc),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[RegexCheckResponse]:
    return ok(data=svc.validate_regex(payload.pattern), trace_id=trace_id)


@router.post(
    "/test",
    response_model=ApiResponse[RuleTestResponse],
    dependencies=_READ_DEPS,
)
async def test_rule(
    payload: RuleTestRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    request: Request,
    svc: SensitiveRuleService = Depends(_svc),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[RuleTestResponse]:
    result = await svc.test_rule(payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="SENSITIVE_RULE_TEST",
        entity_type="sensitive_rule",
        entity_id=payload.column_name,
        detail={"sensitivity_level": result.sensitivity_level, "hits": len(result.hits)},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@router.post(
    "",
    response_model=ApiResponse[SensitiveRuleItem],
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE_DEPS,
)
async def create_rule(
    payload: SensitiveRuleCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    request: Request,
    svc: SensitiveRuleService = Depends(_svc),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[SensitiveRuleItem]:
    item = await svc.create_rule(payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="SENSITIVE_RULE_CREATE",
        entity_type="sensitive_rule",
        entity_id=item.rule_id,
        detail={"label": item.label, "category": item.category, "pii": item.pii},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=item, trace_id=trace_id)


@router.put(
    "/{rule_id}",
    response_model=ApiResponse[SensitiveRuleItem],
    dependencies=_WRITE_DEPS,
)
async def update_rule(
    rule_id: str,
    payload: SensitiveRuleUpsert,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    request: Request,
    svc: SensitiveRuleService = Depends(_svc),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[SensitiveRuleItem]:
    item = await svc.update_rule(rule_id, payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="SENSITIVE_RULE_UPDATE",
        entity_type="sensitive_rule",
        entity_id=item.rule_id,
        detail={"label": item.label, "category": item.category, "pii": item.pii},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=item, trace_id=trace_id)


@router.patch(
    "/{rule_id}/status",
    response_model=ApiResponse[SensitiveRuleItem],
    dependencies=_WRITE_DEPS,
)
async def set_rule_status(
    rule_id: str,
    action: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    request: Request,
    svc: SensitiveRuleService = Depends(_svc),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[SensitiveRuleItem]:
    if action not in ("activate", "deactivate"):
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="action 仅支持 activate/deactivate")
    item = await svc.set_status(rule_id, action)
    await write_audit(
        db,
        actor_id=user.id,
        action="SENSITIVE_RULE_STATUS",
        entity_type="sensitive_rule",
        entity_id=rule_id,
        detail={"action": action, "status": item.status},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=item, trace_id=trace_id)


@router.delete(
    "/{rule_id}",
    response_model=ApiResponse[dict[str, str]],
    dependencies=_WRITE_DEPS,
)
async def delete_rule(
    rule_id: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    request: Request,
    svc: SensitiveRuleService = Depends(_svc),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict[str, str]]:
    await svc.delete_rule(rule_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="SENSITIVE_RULE_DELETE",
        entity_type="sensitive_rule",
        entity_id=rule_id,
        detail={},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"detail": "deleted"}, trace_id=trace_id)
