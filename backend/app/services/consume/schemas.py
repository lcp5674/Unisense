"""consume 层 Pydantic schemas（TD §12.6 / FR-12,13）。

覆盖：查询请求/响应、dry-run 响应、接入方 CRUD、快照、收藏。
对齐 DEV_GUIDE §3（入参/出参 schema）与 TD §3.6 接口契约。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.consume import ApiClientStatus, FavoriteAssetType, SnapshotGeneratedBy


class DimensionExpr(BaseModel):
    """维度过滤表达式（对齐 TD §3.6 query 入参）。"""

    name: str = Field(..., description="维度名")
    value: Any = Field(..., description="维度值")


class QueryRequest(BaseModel):
    """指标查询请求（POST /consume/query）。

    承载口径真相源查询意图；是否真正下推 OLAP 由执行引擎决定（本期未连 OLAP 时降级 503）。
    """

    metric_code: str = Field(..., description="指标码", examples=["gmv_net"])
    dimensions: list[DimensionExpr] = Field(default_factory=list, description="维度过滤")
    date_range: str = Field(..., description="日期区间（如 2026-01~2026-03）")
    granularity: str | None = Field(None, description="时间粒度（day/week/month/quarter）")
    # 组合粒度消费（2026-08-28 方案 B）：指标粒度维度（参与唯一性的业务实体，如
    # hospital）缺省是消费 SQL 的固定构成；消费方可显式传粒度维度子集声明消费范围
    # （与挂载 granularity_dims 一致性校验；维度过滤按此收敛）。空 = 全部粒度维度。
    granularity_dims: list[str] | None = Field(
        None, description="粒度维度子集（组合粒度唯一性实体，如 [\"hospital\"]）"
    )
    # 多变体消费（2026-08-27 放开一指标一挂载）：多挂载指标缺省按默认变体
    # （default_period 行优先）消费——旧契约零破坏；显式传 variant 可覆盖：
    # 挂载行 ID（数字）或 "粒度:周期"（如 "医院:day"），命中不存在变体则 422。
    variant: str | None = Field(
        None, description="变体标识（多挂载指标显式指定：挂载行 ID 或 '粒度:周期'）"
    )
    comparison: str | None = Field(None, description="对比方式（MoM/YoY/None）")
    accept_stale: bool = Field(False, description="接受降级缓存结果")
    params: dict[str, Any] = Field(default_factory=dict, description="其他参数")


class DryRunResponse(BaseModel):
    """dry-run 响应（POST /consume/query/dry-run）。

    不执行、不写、不计费、不进缓存（TD §12.6）。返回执行计划 + 元信息标注 + 校验结论。
    """

    metric_code: str
    status: str = Field(..., description="校验结果 ok / rejected")
    checks: list[dict[str, Any]] = Field(default_factory=list, description="逐项校验结论")
    execution_plan: dict[str, Any] = Field(
        default_factory=dict, description="执行计划（AST/方言 SQL 占位）"
    )
    meta: dict[str, Any] = Field(
        default_factory=dict, description="元信息标注（粒度/单位/PII/血缘/版本）"
    )


class QueryResponse(BaseModel):
    """查询响应（POST /consume/query）。"""

    metric_code: str
    degraded: bool = Field(False, description="是否降级（OLAP 不可用时 True）")
    data: dict[str, Any] | None = Field(None, description="查询结果（降级时为 None）")
    execution_plan: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class ClientCreateRequest(BaseModel):
    """创建接入方（平台管理员，POST /consume/api-clients）。"""

    client_id: str | None = Field(
        None, min_length=3, max_length=64, description="接入方ID（缺省由系统自动生成 app_ 前缀）"
    )
    secret: str = Field(..., min_length=8, description="接入方密钥（明文，仅返回一次）")
    scope_domain: str | None = None
    metric_whitelist: list[str] | None = None
    qps: int = Field(20, ge=1, le=1000)
    daily_quota: int = Field(100_000, ge=1)


class ClientResponse(BaseModel):
    """接入方视图（不含 secret）。"""

    client_id: str
    scope_domain: str | None
    metric_whitelist: list[str] | None
    qps: int
    daily_quota: int
    status: ApiClientStatus


class ClientCreatedResponse(ClientResponse):
    """创建接入方响应（含一次性明文 secret）。"""

    secret: str = Field(..., description="仅此一次返回")


class SnapshotResponse(BaseModel):
    """结果快照视图（WORM 只读）。"""

    id: int
    metric_code: str
    version: int
    dims: dict[str, Any]
    date_range: str
    value_json: dict[str, Any]
    quality_flag: str | None
    generated_at: datetime
    generated_by: SnapshotGeneratedBy


class FavoriteRequest(BaseModel):
    """收藏/取消收藏请求（通用多资产收藏）。

    asset_id 统一为资产业务编码：指标码 / 库.表 / 术语码 / 维度码 / 模板码。
    """

    asset_type: FavoriteAssetType = Field(
        default=FavoriteAssetType.METRIC, description="资产类型"
    )
    asset_id: str = Field(..., description="资产业务编码", examples=["sales_gmv"])


class FavoriteResponse(BaseModel):
    """收藏响应。"""

    asset_type: str
    asset_id: str
    pinned: bool


class RejectRequest(BaseModel):
    """版本拒绝请求（消费方确认回调）。"""

    reason: str | None = Field(None, description="拒绝原因")
