"""指标复用度 / 资产账本统计 REST API（数仓视角治理统计）。

对齐 TD §12.3：复用血缘边量化「原子指标 → 派生指标 → 报表引用」复用度（P0），
聚合活跃/僵尸/重复建设资产账本（P1）。全部成功响应套用统一信封
``{code, message, data, trace_id}``（见 app.api.responses）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.semantic.metric_stats import MetricStatsService

router = APIRouter(prefix="/metric-definitions", tags=["metric-definitions-stats"])

# 治理统计为只读参考类端点：任意登录用户可读（对齐 metrics.py 的 _READ_DEPS 模式）
_READ_DEPS = [Depends(require_roles(*ALL_ROLES)), Depends(guard_against_injection)]


class MetricReuseItem(BaseModel):
    """单个指标的被引用统计。"""

    metric_code: str
    name: str
    domain: str | None = None
    type: str
    status: str
    derived_by_count: int = Field(description="被派生指标 DERIVED_FROM 引用数")
    consumed_by_count: int = Field(description="被报表/接入方 CONSUMED_BY 引用数")
    reuse_count: int = Field(description="总复用度（派生引用 + 消费引用）")


class MetricReuseResponse(BaseModel):
    """指标复用度统计清单（按复用度降序）。"""

    total: int = Field(description="参与统计的指标总数")
    referenced: int = Field(description="有被引用（复用度>0）的指标数")
    zero_reuse: int = Field(description="零复用指标数")
    items: list[MetricReuseItem] = Field(default_factory=list)


@router.get(
    "/stats/reuse",
    response_model=ApiResponse[MetricReuseResponse],
    summary="指标复用度分析（P0：原子→派生→报表引用复用度清单）",
    dependencies=_READ_DEPS,
)
async def metric_reuse_stats(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    domain: Annotated[str | None, Query(description="业务域 code 过滤")] = None,
    metric_type: Annotated[
        str | None, Query(alias="type", description="指标类型过滤（atomic/derived/composite）")
    ] = None,
    status: Annotated[str | None, Query(description="指标状态过滤")] = None,
) -> ApiResponse[MetricReuseResponse]:
    """返回每个指标的被引用情况（被多少派生指标/报表引用），按总复用度降序。

    ``reuse_count = derived_by_count + consumed_by_count``；顶部为高复用核心指标，
    尾部为零复用指标（潜在治理对象）。支持按业务域/指标类型/指标状态过滤，
    统计计数与清单基于同一过滤集合。
    """
    result = await MetricStatsService(db).reuse_summary(
        domain=domain,
        type=metric_type,
        status=status,
        visible_actor_id=user.id,
        visible_role=user.role,
    )
    return ok(data=MetricReuseResponse.model_validate(result), trace_id=trace_id)


class MetricLedgerZombieItem(BaseModel):
    """僵尸指标明细（长期无更新 + 零引用）。"""

    metric_code: str
    name: str
    domain: str | None = None
    type: str
    status: str
    last_updated_at: str | None = Field(default=None, description="最后一次更新时间（ISO8601）")
    days_since_update: int | None = Field(default=None, description="距最后一次更新的天数")
    derived_by_count: int = Field(description="被派生指标 DERIVED_FROM 引用数")
    consumed_by_count: int = Field(description="被报表/接入方 CONSUMED_BY 引用数")
    reuse_count: int = Field(description="总复用度（派生引用 + 消费引用）")


class MetricLedgerDuplicateItem(BaseModel):
    """重复建设指标明细（SAME_DEF_DIFF_NAME 冲突预检信号）。"""

    metric_code: str
    name: str
    domain: str | None = None
    conflict_score: float | None = Field(default=None, description="口径相似度得分")
    existing_code: str | None = Field(default=None, description="口径实质相同的既有指标码")
    reason: str | None = Field(default=None, description="冲突预检原因")


class MetricLedgerResponse(BaseModel):
    """指标资产账本（活跃/僵尸/重复建设）。"""

    total: int = Field(description="参与统计的指标总数")
    active_count: int = Field(description="活跃指标数（近 30 天有更新或有引用）")
    zombie_count: int = Field(description="僵尸指标数（长期无更新且零引用）")
    duplicate_count: int = Field(description="重复建设指标数（SAME_DEF_DIFF_NAME 信号）")
    zombies: list[MetricLedgerZombieItem] = Field(default_factory=list)
    duplicates: list[MetricLedgerDuplicateItem] = Field(default_factory=list)


@router.get(
    "/stats/ledger",
    response_model=ApiResponse[MetricLedgerResponse],
    summary="指标资产账本（P1：活跃/僵尸/重复建设清单）",
    dependencies=_READ_DEPS,
)
async def metric_asset_ledger(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    domain: Annotated[str | None, Query(description="业务域 code 过滤")] = None,
    metric_type: Annotated[
        str | None, Query(alias="type", description="指标类型过滤（atomic/derived/composite）")
    ] = None,
    status: Annotated[str | None, Query(description="指标状态过滤")] = None,
) -> ApiResponse[MetricLedgerResponse]:
    """返回活跃/僵尸指标数与每个僵尸指标详情（最后一次更新、被引用次数）。

    僵尸判定复用 HealthScorer 活跃度维度（近 30 天无更新）+ 零引用；重复建设
    以冲突预检挂载的 SAME_DEF_DIFF_NAME 信号为来源（低耦合，不深挖仲裁侧）。
    支持按业务域/指标类型/指标状态过滤，统计计数与明细基于同一过滤集合。
    """
    result = await MetricStatsService(db).asset_ledger(
        domain=domain,
        type=metric_type,
        status=status,
        visible_actor_id=user.id,
        visible_role=user.role,
    )
    return ok(data=MetricLedgerResponse.model_validate(result), trace_id=trace_id)
