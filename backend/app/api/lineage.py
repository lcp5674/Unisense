"""血缘 API 路由。

对齐 TD §12.2 与 DEV_GUIDE §8b：RBAC 写闸门、SQL 注入守卫、审计、trace_id 可观测。
影响分析读路径图优先 + MySQL 兜底，结果分页返回；what-if 预览走写闸门 + 审计。
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.guard import guard_against_injection, guard_against_injection_exempt
from app.core.resilience import CircuitBreaker
from app.db.mysql import get_db_session
from app.db.redis import get_redis
from app.services.lineage.events import LineageEventPublisher
from app.services.lineage.graph import LineageGraphClient
from app.services.lineage.schemas import (
    CoverageBrokenEdgeItem,
    CoverageOrphanItem,
    EdgeDeleteResult,
    ImpactPreviewRequest,
    LineageCoverageResponse,
    LineageEdgeDetailResponse,
    LineageEdgeListParams,
    LineageImpactParams,
    LineageParseRequest,
    LineageStaleParams,
    ManualEdgeCreateRequest,
    ManualEdgeCreateResponse,
)
from app.services.lineage.service import LineageService, paginate_edges

router = APIRouter(prefix="/lineage", tags=["lineage"])

_WRITE_ROLES = ("platform_admin", "domain_admin", "metric_owner")
_READ_ROLES = ALL_ROLES
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一挂注入守卫（纵深防御：ORM 参数化兜底之外拦截注入 payload）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]
# /parse 的 sql 字段就是待解析的 SQL 文本本身：仅经 sqlglot 纯函数解析（不执行、
# 不拼接进任何 DB 查询），全局注入正则会误伤合法 SQL（-- 注释 / /* */ 块注释 /
# UNION SELECT / 多语句 ETL），故对该字段豁免；其余字段与 query 参数仍全量扫描。
_PARSE_DEPS = [
    Depends(require_roles(*_WRITE_ROLES)),
    Depends(guard_against_injection_exempt("sql")),
]

# Neo4j 驱动持连接池，每请求新建 LineageGraphClient 且从不 dispose 会让 driver
# 随请求泄漏、连接持续耗尽（P1）。改为模块级单例复用同一 driver：
# 惰性创建一次、跨请求复用，进程退出时由 lifespan 统一 dispose。
_graph_client: LineageGraphClient | None = None


def _get_graph_client() -> LineageGraphClient:
    global _graph_client
    if _graph_client is None:
        _graph_client = LineageGraphClient()
    return _graph_client


def _svc(db: Any) -> LineageService:
    redis = None
    with contextlib.suppress(RuntimeError):
        redis = get_redis()
    return LineageService(
        db,
        graph=_get_graph_client(),
        events=LineageEventPublisher(redis, CircuitBreaker()),
        redis=redis,
    )


async def dispose_graph_client() -> None:
    """关闭共享 Neo4j driver（lifespan shutdown 调用）。"""
    global _graph_client
    if _graph_client is not None:
        await _graph_client.dispose()
        _graph_client = None


@router.post("/parse", dependencies=_PARSE_DEPS)
async def parse_lineage(
    body: LineageParseRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """解析 SQL 并持久化血缘（表级 + 字段级）。"""
    svc = _svc(db)
    result = await svc.parse_and_store(body, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="LINEAGE_PARSE",
        entity_type="lineage",
        entity_id=body.source_node or "sql",
        detail={
            "table_edges": result.table_edges,
            "field_edges": result.field_edges,
            "graph_written": result.graph_written,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result.model_dump(), trace_id=trace_id)


@router.post(
    "/impact-preview",
    dependencies=[
        Depends(require_roles(*_WRITE_ROLES)),
        Depends(guard_against_injection),
    ],
)
async def impact_preview(
    body: ImpactPreviewRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """变更影响预览 what-if：估算影响面（指标/物理表/消费方）与风险等级。"""
    svc = _svc(db)
    result = await svc.impact_preview(body.metric_code, body.change_type)
    await write_audit(
        db,
        actor_id=user.id,
        action="LINEAGE_IMPACT_PREVIEW",
        entity_type="lineage",
        entity_id=f"metric:{body.metric_code}",
        detail=result.model_dump(),
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@router.get("/impact", dependencies=_READ_DEPS)
async def impact(
    params: Annotated[LineageImpactParams, Depends()],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """影响分析：给定节点向上/向下/双向展开血缘（分页返回）。

    响应除边列表外携带 ``nodes``（当前页节点的基础元数据，含 entity_id/domain/
    owner/pii），供前端血缘查询/影响分析图谱点击节点时在侧边栏展示具体信息。
    """
    svc = _svc(db)
    edges = await svc.query_impact(params)
    page = paginate_edges(edges, params.page, params.page_size)
    page["nodes"] = await svc.node_meta(
        {it["source_node"] for it in page["items"]} | {it["target_node"] for it in page["items"]}
    )
    return ok(data=page, trace_id=trace_id)


@router.get("/edges", dependencies=_READ_DEPS)
async def list_edges(
    params: Annotated[LineageEdgeListParams, Depends()],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """列出与节点相关的血缘边（分页返回，含 total）。

    与 ``/impact`` 一致携带 ``nodes`` 节点元数据（当前页节点），供前端图谱点击
    节点在侧边栏展示具体信息。
    """
    svc = _svc(db)
    edges = await svc.list_edges(params.node, params.direction)
    page = paginate_edges(edges, params.page, params.page_size)
    page["nodes"] = await svc.node_meta(
        {it["source_node"] for it in page["items"]} | {it["target_node"] for it in page["items"]}
    )
    return ok(data=page, trace_id=trace_id)


@router.delete(
    "/edges",
    dependencies=[Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)],
)
async def delete_edges_by_node(
    params: Annotated[LineageEdgeListParams, Depends()],
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """级联软删某节点相关的全部血缘边（数据源删除时维护一致性）。"""
    svc = _svc(db)
    deleted = await svc.delete_by_node(params.node)
    await write_audit(
        db,
        actor_id=user.id,
        action="LINEAGE_DELETE",
        entity_type="lineage",
        entity_id=params.node,
        detail={"deleted_edges": deleted},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"deleted": deleted}, trace_id=trace_id)


@router.get("/edges/{edge_id}", dependencies=_READ_DEPS)
async def edge_detail(
    edge_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[LineageEdgeDetailResponse]:
    """单条血缘边详情：边当前值 + 变更历史（边元数据查询）。"""
    detail = await _svc(db).edge_detail(edge_id)
    return ok(data=detail.model_dump(mode="json"), trace_id=trace_id)


@router.post(
    "/edges/manual",
    dependencies=[
        Depends(require_roles(*_WRITE_ROLES)),
        Depends(guard_against_injection),
    ],
)
async def add_manual_edge(
    body: ManualEdgeCreateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[ManualEdgeCreateResponse]:
    """手动登记一条血缘边（人工治理：自动解析覆盖不到的业务依赖）。

    ``source_node`` 为上游、``target_node`` 为下游，均须带受支持前缀
    （``metric:``/``table:``/``column:``/``dimension:``/``consumer:``/``external:``）。
    登记边 provenance=manual、owner=登记人，幂等（重复提交更新既有边）。
    """
    svc = _svc(db)
    result = await svc.add_manual_edge(body, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="LINEAGE_MANUAL_ADD",
        entity_type="lineage",
        entity_id=f"{result.edge.source_node}->{result.edge.target_node}",
        detail={
            "edge_type": result.edge.edge_type,
            "granularity": result.edge.granularity,
            "created": result.created,
            "note": body.note,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@router.delete(
    "/edges/{edge_id}",
    dependencies=[
        Depends(require_roles(*_WRITE_ROLES)),
        Depends(guard_against_injection),
    ],
)
async def delete_single_edge(
    edge_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[EdgeDeleteResult]:
    """单条血缘边软删（人工治理：误登记/断链修复的单边删除，非整节点级联删）。"""
    svc = _svc(db)
    result = await svc.delete_edge_by_id(edge_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="LINEAGE_EDGE_DELETE",
        entity_type="lineage",
        entity_id=f"{result.source_node}->{result.target_node}",
        detail={"edge_id": edge_id},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@router.post(
    "/consumers/{metric_code}/sync",
    dependencies=_WRITE_DEPS,
)
async def sync_metric_consumers(
    metric_code: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """按接入方白名单批量补齐某指标的全部消费方血缘边（CONSUMED_BY）。

    消费侧指标上线/白名单变更后调用，使指标详情血缘图展示其数据消费方。
    """
    svc = _svc(db)
    registered = await svc.register_metric_consumers_from_db(metric_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="LINEAGE_CONSUMER_SYNC",
        entity_type="lineage",
        entity_id=f"metric:{metric_code}",
        detail={"registered_edges": registered},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"metric_code": metric_code, "registered_edges": registered}, trace_id=trace_id)


# ---- 血缘覆盖率治理（Task B）----


@router.get("/coverage", dependencies=_READ_DEPS)
async def coverage(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[LineageCoverageResponse]:
    """血缘覆盖率统计：指标/表血缘完整度、孤儿子数与断链边数（治理看板）。"""
    stats = await _svc(db).coverage_stats()
    return ok(data=stats.model_dump(mode="json"), trace_id=trace_id)


@router.get("/coverage/orphans", dependencies=_READ_DEPS)
async def coverage_orphans(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[list[CoverageOrphanItem]]:
    """无任何血缘边的孤立指标清单（预案式治理。"""
    orphans = await _svc(db).coverage_orphan_metrics()
    return ok(data=[o.model_dump(mode="json") for o in orphans], trace_id=trace_id)


@router.get("/coverage/broken", dependencies=_READ_DEPS)
async def coverage_broken(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    limit: int = Query(200, ge=1, le=2000, description="返回断链边条数上限"),
) -> ApiResponse[list[CoverageBrokenEdgeItem]]:
    """断链边明细：source 节点对应的目录/指标实体已不存在（供人工修复跳转）。"""
    broken = await _svc(db).coverage_broken_edges(limit=limit)
    return ok(data=[b.model_dump(mode="json") for b in broken], trace_id=trace_id)


# ---- 血缘采集通道（增量采集运维，TD §12.2）----


@router.get("/graph", dependencies=_READ_DEPS)
async def lineage_graph(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    domain: str | None = Query(None, description="按业务域过滤节点"),
    pii_only: bool = Query(False, description="仅返回含 PII 标记的节点"),
    limit: int = Query(1000, ge=1, le=5000, description="返回边数上限"),
    provenance: str | None = Query(
        None,
        description=(
            "来源通道过滤（dp_csv/sqlglot/metric_definition）；为空=采集目录视角，"
            "指定=该通道完整表级血缘"
        ),
    ),
) -> ApiResponse[Any]:
    """血缘图谱：返回节点+边数据，前端力导向图渲染（血缘视图默认 Tab）。

    ``provenance`` 指定时从血缘边直接构建表级血缘（DP/SQL 通道导入的表完整可见，
    不再受采集目录交集限制）；为空时复用资产地图采集目录视角图谱。
    """
    data = await _svc(db).query_graph(
        domain=domain, pii_only=pii_only, limit=limit, provenance=provenance
    )
    return ok(data=data, trace_id=trace_id)


@router.get("/channels", dependencies=_READ_DEPS)
async def list_channels(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """血缘采集通道总览：各来源边数/节点数/失效数/最近运行。"""
    svc = _svc(db)
    channels = await svc.list_channels()
    return ok(
        data=[c.model_dump(mode="json") for c in channels],
        trace_id=trace_id,
    )


@router.get("/channels/{source}/runs", dependencies=_READ_DEPS)
async def list_channel_runs(
    source: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    limit: int = 20,
) -> ApiResponse[Any]:
    """某来源通道的采集运行历史（变更摘要，按时间倒序）。"""
    svc = _svc(db)
    runs = await svc.list_ingest_runs(source, limit)
    return ok(
        data=[r.model_dump(mode="json") for r in runs],
        trace_id=trace_id,
    )


@router.get("/runs/{run_id}", dependencies=_READ_DEPS)
async def run_detail(
    run_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """单条采集运行详情（含详情快照 detail）。

    SQL 解析运行含 SQL 原文 / dialect / target_table / source_node / 表级与字段级
    边明细；批量采集运行含新增/更新边明细。供「运行历史行 → 详情」展示具体信息。
    """
    svc = _svc(db)
    run = await svc.get_ingest_run_detail(run_id)
    return ok(data=run.model_dump(mode="json"), trace_id=trace_id)


@router.get("/stale", dependencies=_READ_DEPS)
async def list_stale(
    params: Annotated[LineageStaleParams, Depends()],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """失效队列：连续未被采集确认、待人工处置的血缘边。"""
    svc = _svc(db)
    edges = await svc.list_stale(params.source, params.limit)
    return ok(
        data=[e.model_dump(mode="json") for e in edges],
        trace_id=trace_id,
    )


@router.get("/nodes", dependencies=_READ_DEPS)
async def list_nodes(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    kw: str | None = Query(None, max_length=100, description="关键词过滤节点（匹配节点 id）"),
    limit: int = Query(50, ge=1, le=200, description="返回节点数上限"),
) -> ApiResponse[Any]:
    """血缘候选节点：影响分析/血缘查询选项框的预加载与关键词搜索。

    无 ``kw`` 时按参与边数倒序返回 top-N（预加载常用节点）；带 ``kw`` 时按节点 id
    模糊过滤，供用户输入关键词搜索指定节点（table:/metric:/field: 前缀节点）。
    """
    svc = _svc(db)
    nodes = await svc.list_nodes(kw=kw, limit=limit)
    return ok(
        data=[n.model_dump(mode="json") for n in nodes],
        trace_id=trace_id,
    )


@router.post(
    "/stale/{edge_id}/confirm",
    dependencies=_WRITE_DEPS,
)
async def confirm_stale(
    edge_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """确认失效边：软删权威存储并同步清理图存储。"""
    svc = _svc(db)
    edge = await svc.confirm_stale_edge(edge_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="LINEAGE_STALE_CONFIRM",
        entity_type="lineage_edge",
        entity_id=str(edge_id),
        detail={
            "source_node": edge.source_node,
            "target_node": edge.target_node,
            "provenance": edge.provenance,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=edge.model_dump(mode="json"), trace_id=trace_id)


@router.post(
    "/stale/{edge_id}/restore",
    dependencies=_WRITE_DEPS,
)
async def restore_stale(
    edge_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """恢复失效边：清除失效标记，重新参与血缘查询。"""
    svc = _svc(db)
    edge = await svc.restore_stale_edge(edge_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="LINEAGE_STALE_RESTORE",
        entity_type="lineage_edge",
        entity_id=str(edge_id),
        detail={
            "source_node": edge.source_node,
            "target_node": edge.target_node,
            "provenance": edge.provenance,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=edge.model_dump(mode="json"), trace_id=trace_id)
