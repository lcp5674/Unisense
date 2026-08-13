"""全局聚合搜索 API（FR-18 全局搜索栏生产化）。

GET /api/v1/search?q=...&limit=...：跨 8 类资源（指标/维度/术语/模板/
数据源/采集目录表+字段/主题域）按关键词聚合搜索，按类型分组返回。

只读端点：全部已登录角色可读（RBAC 读闸门）+ SQL 注入守卫（纵深防御）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.global_search.service import GlobalSearchService

router = APIRouter(prefix="/search", tags=["search"])

_READ_ROLES = (
    "metric_owner",
    "domain_admin",
    "platform_admin",
    "reviewer",
    "analyst",
    "compliance_officer",
    "viewer",
)
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]


@router.get("", dependencies=_READ_DEPS, summary="全局聚合搜索")
async def global_search(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    q: str = Query(..., min_length=1, max_length=100, description="搜索关键词"),
    limit: int = Query(5, ge=1, le=20, description="每类资源返回条数上限"),
) -> Any:
    """跨指标/维度/术语/模板/数据源/采集目录表+字段/主题域聚合搜索。

    返回按类型分组的命中的 top-N 条目，供顶栏实时下拉与全局搜索页消费。
    """
    data = await GlobalSearchService(db).search(q.strip(), limit=limit)
    total = sum(len(items) for items in data.values())
    return ok(data={"groups": data, "total": total}, trace_id=trace_id)
