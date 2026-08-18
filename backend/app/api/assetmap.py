"""资产地图 API（TD §12.11 / FR-18）。

P2 增强：GET /graph（图谱）、GET /heatmap（热力）、GET /owner-view（责任人视图）。
产品补充（FR-18 生产化）：GET /search（全局搜索）、GET /health（资产健康）、
GET /pii（PII 合规视图）、GET /changes（变更追踪）、GET /my-assets（我的资产）、
GET /export.csv（资产导出）。
"""

from __future__ import annotations

import csv
import enum
import io
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.exceptions import AuthError, ValidationError
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.models.user import User
from app.services.assetmap.schemas import (
    ApplyPiiTemplateRequest,
    AssignOwnerRequest,
    BatchOwnerRequest,
    BatchSensitivityRequest,
    CatalogReviewRequest,
    PiiFieldOverrideRequest,
    ReclassifySensitivityRequest,
    SetMaskingPolicyRequest,
    SetRetentionRequest,
)
from app.services.assetmap.service import AssetMapService

router = APIRouter(prefix="/assetmap", tags=["assetmap"])


# P2-2：枚举过滤参数集中校验（任意字符串静默返回空 → 改为 422 明确报错）。
_VALID_SENSITIVITY = {"PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "NEEDS_REVIEW"}
_VALID_ENTITY_TYPE = {"TABLE", "VIEW", "FIELD"}
_VALID_REVIEW_STATUS = {"reviewed", "unreviewed"}


def _assert_enum(value: str | None, allowed: set[str], field: str) -> None:
    """过滤参数为非法枚举值时返回 422（而非静默空结果）。

    大小写兼容：合法值不论大小写均接受（如 ``table``/``TABLE``），仅拒绝
    完全不匹配的垃圾输入（如 ``foobar``），避免既有小写过滤链路被误伤。
    """
    if value is None:
        return
    if value in allowed or value.upper() in allowed or value.lower() in allowed:
        return
    raise ValidationError(
        f"非法的{field}过滤值: {value}",
        error_code="INVALID_FILTER_VALUE",
        ctx={"field": field, "value": value, "allowed": sorted(allowed)},
    )


def _is_platform_admin(user: User) -> bool:
    """当前用户是否平台管理员（角色归一化兼容 enum 成员/字符串/多角色列表）。"""
    role = user.role
    if isinstance(role, (list, tuple, set)):
        role_strs = [r.value if isinstance(r, enum.Enum) else str(r) for r in role]
        return "platform_admin" in role_strs
    role_val = role.value if isinstance(role, enum.Enum) else role
    return str(role_val) == "platform_admin"


_READ_ROLES = ("metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]

# 写能力仅限治理角色（认领/重分类/批量会影响资产归属与合规口径）
_WRITE_ROLES = ("platform_admin", "domain_admin")
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]

# PII 合规治理角色（表级复核/脱敏/标注/模板：职责分离，须合规官或平台管理员）
_COMPLIANCE_ROLES = ("compliance_officer", "platform_admin")
_COMPLIANCE_DEPS = [Depends(require_roles(*_COMPLIANCE_ROLES)), Depends(guard_against_injection)]

# PII 合规数据读取角色：治理域角色（domain_admin 负责本域 PII 复核；
# compliance_officer/platform_admin 全局）。区别于 _READ_DEPS——低权限角色
# viewer/analyst 一律不可见 PII（P0-1：防合规数据泄露）。
_PII_READ_ROLES = ("platform_admin", "domain_admin", "compliance_officer")
_PII_READ_DEPS = [Depends(require_roles(*_PII_READ_ROLES)), Depends(guard_against_injection)]

def _svc(db: AsyncSession, user: User) -> AssetMapService:
    """构造资产地图服务（注入当前用户组织 ID 用于多租户隔离过滤，P1 加固）。

    platform_admin 豁免 org 过滤（org_id=None 全组织可见）；其余角色按
    user.org_id 仅见本组织资产。
    """
    org_id = None if _is_platform_admin(user) else getattr(user, "org_id", None)
    return AssetMapService(db, org_id=org_id)



@router.get("/summary", dependencies=_READ_DEPS)
async def catalog_summary(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await _svc(db, user).catalog_summary(), trace_id=trace_id)


@router.get("/classification", dependencies=_READ_DEPS)
async def classification_summary(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await _svc(db, user).classification_summary(), trace_id=trace_id)


@router.get("/metrics", dependencies=_READ_DEPS)
async def metric_summary(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await _svc(db, user).metric_summary(), trace_id=trace_id)


@router.get("/metric-dimensions", dependencies=_READ_DEPS)
async def metric_dimension_summary(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """指标体系聚合：指标类型/分层/分级/单位/聚合/时间语义/状态/域分布 + PII 合规率。

    概览 Tab「指标体系」区块数据源，每类分布可下钻对应指标明细。
    """
    return ok(data=await _svc(db, user).metric_dimension_summary(), trace_id=trace_id)


@router.get("/tables", dependencies=_READ_DEPS)
async def list_tables(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    source_id: str | None = Query(None),
    sensitivity: str | None = Query(None),
    domain: str | None = Query(None, description="业务域（经数据源继承过滤）"),
    owner_id: int | None = Query(
        None, description="责任人（Owner）ID 过滤；0 表示无责任人（未分配）"
    ),
    schema_status: str | None = Query(
        None, description="Schema 完整性：complete / incomplete"
    ),
    keyword: str | None = Query(None, description="关键字：表名或数据源模糊搜索"),
    limit: int = Query(100, ge=1, le=200),
) -> Any:
    _assert_enum(sensitivity, _VALID_SENSITIVITY, "敏感度")
    items = await _svc(db, user).list_tables(
        source_id,
        sensitivity,
        limit,
        domain=domain,
        owner_id=owner_id,
        schema_status=schema_status,
        keyword=keyword,
    )
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


@router.get("/orphans", dependencies=_READ_DEPS)
async def orphan_assets(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    keyword: str | None = Query(None, description="关键字：实体名或数据源模糊搜索"),
    source_id: str | None = Query(None, description="数据源 ID 过滤"),
    domain: str | None = Query(None, description="业务域（经数据源继承过滤）"),
    entity_type: str | None = Query(None, description="实体类型：table / view / field 等"),
    sensitivity: str | None = Query(None, description="敏感度等级过滤"),
    schema_status: str | None = Query(
        None, description="Schema 完整性：complete / incomplete"
    ),
    limit: int = Query(200, ge=1, le=500),
) -> Any:
    _assert_enum(entity_type, _VALID_ENTITY_TYPE, "实体类型")
    _assert_enum(sensitivity, _VALID_SENSITIVITY, "敏感度")
    items = await _svc(db, user).orphan_assets(
        keyword=keyword,
        source_id=source_id,
        domain=domain,
        entity_type=entity_type,
        sensitivity=sensitivity,
        schema_status=schema_status,
        limit=limit,
    )
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


@router.get("/entities/{entity_id}", dependencies=_READ_DEPS)
async def get_entity_detail(
    entity_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """资产实体详情：表/字段元数据 + 敏感度 + PII + 血缘边数（TD §12.11 流程 #5）。

    前端「数据表目录/孤儿资产」详情抽屉调用此端点；实体不存在返回 404。
    """
    from app.core.exceptions import NotFoundError

    data = await _svc(db, user).get_entity_detail(entity_id)
    if data is None:
        raise NotFoundError(f"资产不存在或已删除: {entity_id}", ctx={"entity_id": entity_id})
    return ok(data=data, trace_id=trace_id)


# ----------------------------------------------------------------
# P2 Enhancement: 图谱 / 热力 / 责任人视图
# ----------------------------------------------------------------


@router.get("/graph", dependencies=_READ_DEPS)
async def get_graph(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    domain: str | None = Query(None, description="按域过滤"),
    depth: int = Query(3, ge=1, le=10, description="图遍历深度"),
    pii_only: bool = Query(False, description="仅返回含 PII 标记的节点"),
) -> Any:
    """资产图谱：返回节点+边数据，前端力导向图渲染。"""
    data = await _svc(db, user).get_graph(domain=domain, depth=depth, pii_only=pii_only)
    return ok(data=data, trace_id=trace_id)


@router.get("/heatmap", dependencies=_READ_DEPS)
async def get_heatmap(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    dimension: str = Query("domain", description="聚合维度: domain/sensitivity/owner/dw_layer"),
) -> Any:
    """敏感分布热力图：按维度聚合返回分桶数据。"""
    data = await _svc(db, user).get_heatmap(dimension=dimension)
    return ok(data=data, trace_id=trace_id)


@router.get("/heatmap-matrix", dependencies=_READ_DEPS)
async def get_heatmap_matrix(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    asset_type: str = Query(
        "catalog",
        pattern="^(catalog|metric)$",
        description="资产视角：catalog=目录资产 / metric=指标资产",
    ),
) -> Any:
    """二维热力矩阵：业务域 × 敏感级别资产分布（前端真热力图数据源）。

    catalog 视角聚合 db_catalog（域经数据源继承）；metric 视角聚合指标表
    （PII / 内部两列）。
    """
    data = await _svc(db, user).heatmap_matrix(asset_type=asset_type)
    return ok(data=data, trace_id=trace_id)


@router.get("/owner-view", dependencies=_READ_DEPS)
async def get_owner_view(
    owner_id: Annotated[int, Query(description="责任人 ID")],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """责任人视图：按 owner_id 聚合资产统计。

    P2-3 加固：跨组织越权枚举防护。非平台管理员仅可查看本组织内责任人的
    资产视图；目标责任人不存在或被跨组织查询时返回 403/404。
    """
    target = await db.get(User, owner_id)
    if target is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError("责任人不存在", ctx={"owner_id": owner_id})
    if not _is_platform_admin(user):
        user_org = getattr(user, "org_id", None)
        target_org = getattr(target, "org_id", None)
        if user_org != target_org:
            raise AuthError(
                "无权查看其他组织的责任人视图",
                error_code="CROSS_ORG_OWNER_VIEW_FORBIDDEN",
                ctx={"user_org": user_org, "target_org": target_org},
            )
    data = await _svc(db, user).get_owner_view(owner_id=owner_id)
    return ok(data=data, trace_id=trace_id)


# ----------------------------------------------------------------
# 产品补充（FR-18 生产化）：全局搜索 / 健康 / PII / 变更 / 我的资产 / 导出
# ----------------------------------------------------------------


@router.get("/search", dependencies=_READ_DEPS)
async def search_assets(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    q: str = Query(..., min_length=1, description="搜索关键词"),
    asset_type: str | None = Query(
        None, alias="type", description="资产类型: catalog/table/field/metric"
    ),
    limit: int = Query(20, ge=1, le=200),
) -> Any:
    """全局资产搜索：目录 + 指标统一结果（资产地图核心工具能力）。"""
    data = await _svc(db, user).search_assets(q, entity_type=asset_type, limit=limit)
    return ok(data={"items": data, "total": len(data)}, trace_id=trace_id)


@router.get("/health", dependencies=_READ_DEPS)
async def health_summary(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """资产健康视图：不健康源/schema 不完整/孤儿/陈旧资产。"""
    data = await _svc(db, user).health_summary()
    return ok(data=data, trace_id=trace_id)


@router.get("/pii", dependencies=_PII_READ_DEPS)
async def pii_overview(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    request: Request,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """PII 合规资产视图：按敏感级/域聚合 PII 资产（面向 compliance_officer）。"""
    data = await _svc(db, user).pii_overview()
    # 合规敏感数据访问留痕：PII 概览属敏感数据读取，须可追溯（P0-1）
    await write_audit(
        db,
        actor_id=user.id,
        action="asset.pii_view",
        entity_type="asset_pii",
        entity_id="overview",
        detail={"categories": data.get("by_category", {}) if isinstance(data, dict) else {}},
        ip=client_ip(request),
        pii_access=True,
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


@router.get("/changes", dependencies=_READ_DEPS)
async def recent_changes(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(50, ge=1, le=200),
) -> Any:
    """变更追踪流：最近 N 天新增/变更的目录与指标。"""
    data = await _svc(db, user).recent_changes(days=days, limit=limit)
    return ok(data=data, trace_id=trace_id)


@router.get("/my-assets", dependencies=_READ_DEPS)
async def my_assets(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    limit: int = Query(50, ge=1, le=200),
) -> Any:
    """我的资产：当前登录用户负责的目录与指标（个人工作台视角）。"""
    data = await _svc(db, user).my_assets(owner_id=user.id, limit=limit)
    return ok(data=data, trace_id=trace_id)


@router.get("/export.csv", dependencies=_READ_DEPS)
async def export_tables(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    source_id: str | None = Query(None),
    sensitivity: str | None = Query(None),
    domain: str | None = Query(None, description="业务域（经数据源继承过滤）"),
    owner_id: int | None = Query(
        None, description="责任人（Owner）ID 过滤；0 表示无责任人（未分配）"
    ),
    schema_status: str | None = Query(
        None, description="Schema 完整性：complete / incomplete"
    ),
    keyword: str | None = Query(None, description="关键字：表名或数据源模糊搜索"),
) -> Response:
    """资产 CSV 导出：目录资产（表/视图）清单，供盘点/审计（与列表同过滤条件）。"""
    items = await _svc(db, user).export_tables(
        source_id,
        sensitivity,
        domain=domain,
        owner_id=owner_id,
        schema_status=schema_status,
        keyword=keyword,
    )

    output = io.StringIO()
    writer = csv.writer(output)

    # CSV 注入防护（OWASP）：单元格以 = / + / - / @ 开头时 Excel/WPS 会当作公式执行，
    # 采集的 entity_name/source_id 等可能被注入，导出前统一前缀单引号消毒。
    def _sanitize(v: object) -> str:
        s = "" if v is None else str(v)
        if s.startswith(("=", "+", "-", "@")):
            return "'" + s
        return s

    writer.writerow(
        [
            "entity_name",
            "entity_type",
            "source_id",
            "sensitivity_level",
            "owner_id",
            "schema_incomplete",
            "created_at",
            "updated_at",
        ]
    )
    for it in items:
        writer.writerow(
            [
                _sanitize(it.get("entity_name", "")),
                _sanitize(it.get("entity_type", "")),
                _sanitize(it.get("source_id", "")),
                _sanitize(it.get("sensitivity_level", "")),
                _sanitize(it.get("owner_id", "")),
                _sanitize(it.get("schema_incomplete", "")),
                _sanitize(it.get("created_at", "")),
                _sanitize(it.get("updated_at", "")),
            ]
        )
    # UTF-8 BOM 便于 Excel 正确识别中文
    body = "\ufeff" + output.getvalue()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="assetmap_export.csv"',
        },
    )


# ----------------------------------------------------------------
# 写能力（FR-18 资产工作台）：认领/转让归属、敏感级重分类、批量操作
# 全部仅限 platform_admin / domain_admin，写入与审计同事务原子提交（PLAT-3）。
# ----------------------------------------------------------------


@router.post("/entities/{entity_id}/owner", dependencies=_WRITE_DEPS)
async def assign_owner(
    entity_id: int,
    payload: AssignOwnerRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """认领/转让资产归属（owner_id=None 解除归属回到孤儿池）。"""
    data = await _svc(db, user).assign_owner(entity_id, payload.owner_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="asset.assign_owner",
        entity_type="db_catalog",
        entity_id=str(entity_id),
        detail={"owner_id": payload.owner_id},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


@router.post("/entities/{entity_id}/sensitivity", dependencies=_WRITE_DEPS)
async def reclassify_sensitivity(
    entity_id: int,
    payload: ReclassifySensitivityRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """重分类资产敏感级（仅允许枚举值，影响 PII 合规口径）。"""
    data = await _svc(db, user).reclassify_sensitivity(
        entity_id, str(payload.sensitivity_level)
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="asset.reclassify",
        entity_type="db_catalog",
        entity_id=str(entity_id),
        detail={"sensitivity_level": str(payload.sensitivity_level)},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


@router.post("/batch-owner", dependencies=_WRITE_DEPS)
async def batch_assign_owner(
    payload: BatchOwnerRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """批量认领/转让归属（单次 ≤200，同事务原子提交）。"""
    data = await _svc(db, user).batch_assign_owner(payload.entity_ids, payload.owner_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="asset.batch_assign_owner",
        entity_type="db_catalog",
        entity_id="batch",
        detail={
            "entity_ids": payload.entity_ids,
            "owner_id": payload.owner_id,
            "affected": data.get("affected"),
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


@router.post("/batch-sensitivity", dependencies=_WRITE_DEPS)
async def batch_reclassify(
    payload: BatchSensitivityRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """批量重分类敏感级（单次 ≤200，同事务原子提交）。"""
    data = await _svc(db, user).batch_reclassify(
        payload.entity_ids, str(payload.sensitivity_level)
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="asset.batch_reclassify",
        entity_type="db_catalog",
        entity_id="batch",
        detail={
            "entity_ids": payload.entity_ids,
            "sensitivity_level": str(payload.sensitivity_level),
            "affected": data.get("affected"),
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


# ----------------------------------------------------------------
# PII 合规增强（A/B/C）：明细列表 / 概览增强 / 表级复核 / 脱敏 / 标注 / 保留期 / 模板 / 导出
# ----------------------------------------------------------------


@router.get("/pii-assets", dependencies=_PII_READ_DEPS)
async def list_pii_assets(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    request: Request,
    trace_id: Annotated[str, Depends(get_trace_id)],
    keyword: str | None = Query(None, description="关键字：实体名或数据源模糊搜索"),
    source_id: str | None = Query(None, description="数据源 ID 过滤"),
    domain: str | None = Query(None, description="业务域（经数据源继承过滤）"),
    owner_id: int | None = Query(
        None, description="责任人 ID 过滤；0 表示无主 PII（最高优先级合规风险）"
    ),
    review_status: str | None = Query(
        None, description="复核状态：unreviewed / reviewed"
    ),
    category: str | None = Query(None, description="PII 类别过滤（ID_CARD/PHONE/...）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    """PII 资产明细列表（分页 + 多维度筛选），PII 合规 Tab 可下钻。"""
    _assert_enum(review_status, _VALID_REVIEW_STATUS, "复核状态")
    data = await _svc(db, user).list_pii_assets(
        keyword=keyword,
        source_id=source_id,
        domain=domain,
        owner_id=owner_id,
        review_status=review_status,
        category=category,
        page=page,
        page_size=page_size,
    )
    # 合规敏感数据访问留痕：PII 明细属敏感数据读取，须可追溯（P0-1）
    await write_audit(
        db,
        actor_id=user.id,
        action="asset.pii_list",
        entity_type="asset_pii",
        entity_id=f"page={page}&size={page_size}",
        detail={
            "total": data.get("total", 0) if isinstance(data, dict) else 0,
            "category": category,
            "owner_id": owner_id,
            "review_status": review_status,
        },
        ip=client_ip(request),
        pii_access=True,
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


@router.get("/pii/templates", dependencies=_PII_READ_DEPS)
async def list_pii_templates(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    request: Request,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """行业分级模板列表（PII 合规盘点与批量升级）。"""
    data = await _svc(db, user).pii_templates()
    # 合规敏感数据访问留痕：分级模板属 PII 合规配置，读取须可追溯（P0-1）
    await write_audit(
        db,
        actor_id=user.id,
        action="asset.pii_templates",
        entity_type="asset_pii",
        entity_id="templates",
        detail={"count": len(data)},
        ip=client_ip(request),
        pii_access=True,
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"items": data, "total": len(data)}, trace_id=trace_id)


@router.post("/pii/templates/apply", dependencies=_COMPLIANCE_DEPS)
async def apply_pii_template(
    payload: ApplyPiiTemplateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """应用行业分级模板：按字段类别升级资产敏感级（个保法/金融等）。"""
    data = await _svc(db, user).apply_pii_template(
        payload.template_id,
        catalog_ids=payload.catalog_ids,
        source_id=payload.source_id,
        all_pii=payload.all_pii,
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="asset.apply_pii_template",
        entity_type="db_catalog",
        entity_id=payload.template_id,
        detail={"applied": data.get("applied"), "changed": data.get("changed")},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


@router.post("/entities/{entity_id}/review", dependencies=_COMPLIANCE_DEPS)
async def review_catalog_entity(
    entity_id: int,
    payload: CatalogReviewRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """表级 PII 合规复核（APPROVE/REJECT；禁自审：资产责任人不得复核本人资产）。"""
    data = await _svc(db, user).review_catalog(
        entity_id, payload.decision, reviewer_id=user.id
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="asset.review_pii",
        entity_type="db_catalog",
        entity_id=str(entity_id),
        detail={"decision": payload.decision, "comment": payload.comment},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


@router.post("/entities/{entity_id}/masking", dependencies=_COMPLIANCE_DEPS)
async def set_masking_policy(
    entity_id: int,
    payload: SetMaskingPolicyRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """设置资产脱敏策略（none/mask/hash/deny）。"""
    data = await _svc(db, user).set_masking_policy(entity_id, payload.policy)
    await write_audit(
        db,
        actor_id=user.id,
        action="asset.set_masking",
        entity_type="db_catalog",
        entity_id=str(entity_id),
        detail={"masking_policy": payload.policy},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


@router.post("/entities/{entity_id}/pii-overrides", dependencies=_COMPLIANCE_DEPS)
async def upsert_pii_override(
    entity_id: int,
    payload: PiiFieldOverrideRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """字段级人工标注（suppressed=True 误报非 PII；False 人工确认是 PII）。"""
    data = await _svc(db, user).upsert_pii_override(
        entity_id, payload.column, payload.suppressed, payload.reason, actor_id=user.id
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="asset.override_pii",
        entity_type="db_catalog",
        entity_id=str(entity_id),
        detail={"column": payload.column, "suppressed": payload.suppressed},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


@router.post("/entities/{entity_id}/pii-overrides/remove", dependencies=_COMPLIANCE_DEPS)
async def remove_pii_override(
    entity_id: int,
    payload: PiiFieldOverrideRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """撤销字段级人工标注（恢复规则引擎判定）。"""
    data = await _svc(db, user).delete_pii_override(entity_id, payload.column)
    await write_audit(
        db,
        actor_id=user.id,
        action="asset.remove_pii_override",
        entity_type="db_catalog",
        entity_id=str(entity_id),
        detail={"column": payload.column},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


@router.put("/entities/{entity_id}/retention", dependencies=_COMPLIANCE_DEPS)
async def set_retention(
    entity_id: int,
    payload: SetRetentionRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """设置资产保留期与合法性基础（合规留存期限）。"""
    data = await _svc(db, user).set_retention(
        entity_id, payload.retention_days, payload.legal_basis
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="asset.set_retention",
        entity_type="db_catalog",
        entity_id=str(entity_id),
        detail={"retention_days": payload.retention_days, "legal_basis": payload.legal_basis},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


@router.get("/pii-export.csv", dependencies=_PII_READ_DEPS)
async def export_pii_csv(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    request: Request,
    trace_id: Annotated[str, Depends(get_trace_id)],
    keyword: str | None = Query(None),
    source_id: str | None = Query(None),
    domain: str | None = Query(None),
    owner_id: int | None = Query(None),
    review_status: str | None = Query(None),
    category: str | None = Query(None),
) -> Response:
    """PII 合规盘点 CSV 导出（含字段明细/类别/复核/脱敏状态，交合规盘点）。"""
    _assert_enum(review_status, _VALID_REVIEW_STATUS, "复核状态")
    items = await _svc(db, user).export_pii_rows(
        keyword=keyword,
        source_id=source_id,
        domain=domain,
        owner_id=owner_id,
        review_status=review_status,
        category=category,
    )

    output = io.StringIO()
    writer = csv.writer(output)

    def _sanitize(v: object) -> str:
        s = "" if v is None else str(v)
        if s.startswith(("=", "+", "-", "@")):
            return "'" + s
        return s

    writer.writerow(
        [
            "entity_name",
            "entity_type",
            "source_id",
            "sensitivity_level",
            "owner_id",
            "owner_name",
            "domain",
            "compliance_reviewed",
            "masking_policy",
            "pii_field_count",
            "pii_categories",
            "pii_fields",
            "updated_at",
        ]
    )
    for it in items:
        pii_fields = it.get("pii_fields") or []
        writer.writerow(
            [
                _sanitize(it.get("entity_name", "")),
                _sanitize(it.get("entity_type", "")),
                _sanitize(it.get("source_id", "")),
                _sanitize(it.get("sensitivity_level", "")),
                _sanitize(it.get("owner_id", "")),
                _sanitize(it.get("owner_name", "")),
                _sanitize(it.get("domain", "")),
                _sanitize(it.get("compliance_reviewed", "")),
                _sanitize(it.get("masking_policy", "")),
                _sanitize(it.get("pii_field_count", "")),
                _sanitize(",".join(it.get("categories") or [])),
                _sanitize(
                    ";".join(
                        f"{f.get('column')}:{f.get('category')}:{f.get('confidence')}"
                        for f in pii_fields
                        if not f.get("suppressed")
                    )
                ),
                _sanitize(it.get("updated_at", "")),
            ]
        )
    body = "\ufeff" + output.getvalue()
    # 合规敏感数据导出留痕：PII 盘点 CSV 属最高敏感级别数据导出，须强制审计（P0-1）
    await write_audit(
        db,
        actor_id=user.id,
        action="asset.pii_export",
        entity_type="asset_pii",
        entity_id="csv",
        detail={"rows": len(items), "category": category, "review_status": review_status},
        ip=client_ip(request),
        pii_access=True,
        trace_id=trace_id,
    )
    await db.commit()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="pii_compliance_export.csv"',
        },
    )
