"""全局聚合搜索 API（FR-18 全局搜索栏生产化）。

GET /api/v1/search?q=...&limit=...：跨 9 类资源（指标/维度/术语/模板/
数据源/采集目录表+字段/主题域/度量目录）按关键词聚合搜索，按类型分组返回。

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
from app.services.search.es_indexer import EsIndexer

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
# ES 索引管理（建映射/全量同步）：管理员专属写操作
_INDEX_WRITE_DEPS = [
    Depends(require_roles("platform_admin", "domain_admin")),
    Depends(guard_against_injection),
]


@router.get("", dependencies=_READ_DEPS, summary="全局聚合搜索")
async def global_search(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    q: str = Query(..., min_length=1, max_length=100, description="搜索关键词"),
    limit: int = Query(5, ge=1, le=20, description="每类资源返回条数上限"),
) -> Any:
    """跨指标/维度/术语/模板/数据源/采集目录表+字段/主题域/度量目录聚合搜索。

    返回按类型分组的命中的 top-N 条目，供顶栏实时下拉与全局搜索页消费。

    可见性（D-1）：透传当前用户上下文，指标检索按目录同一语义做行级隔离——
    非管理角色仅可检索公开状态 + 本人负责的未发布资产，杜绝经搜索侧门窥探
    他人 DRAFT/REVIEW 草稿与 PII 标记。
    """
    data = await GlobalSearchService(db).search(
        q.strip(),
        limit=limit,
        visible_actor_id=user.id,
        visible_role=user.role,
        visible_user_domains=user.domain,
    )
    total = sum(len(items) for items in data.values())
    return ok(data={"groups": data, "total": total}, trace_id=trace_id)


@router.post("/indexes/ensure", dependencies=_INDEX_WRITE_DEPS, summary="ES 索引映射幂等创建")
async def ensure_indexes(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    force_recreate: bool = Query(
        False,
        description="强制删除重建（analyzer/同义词词表变更后使用，随后需 /indexes/sync 重灌）",
    ),
) -> Any:
    """创建 metric_idx / term_idx 索引映射（幂等：已含当前 analyzer 返回 False 不重建）。

    同义词过滤器变更无法原地更新 → 版本检测到旧 mapping 自动删除重建（返回 True），
    调用方随后调用 /indexes/sync 全量重灌。force_recreate=True 强制重建。

    ES 未配置/不可用抛 503（SearchUnavailableError），检索路径自动降级 MySQL。
    """
    indexer = EsIndexer(db)
    created = await indexer.ensure_indexes(force_recreate=force_recreate)
    return ok(data={"created": created, "enabled": indexer.enabled}, trace_id=trace_id)


@router.post("/indexes/sync", dependencies=_INDEX_WRITE_DEPS, summary="MySQL → ES 全量同步")
async def sync_indexes(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """从 MySQL 全量灌入指标/术语到 ES（按业务编码 upsert，可重复执行）。"""
    indexer = EsIndexer(db)
    counts = await indexer.sync_all()
    return ok(data={"counts": counts, "enabled": indexer.enabled}, trace_id=trace_id)
