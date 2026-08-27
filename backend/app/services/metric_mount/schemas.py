"""指标挂载实体 Schemas（OneData 挂载层，TD §4.2 dataset_metric）。

字段长度对齐模型列（source_table/source_column=255 / granularity=64 / default_period=32 / domain=64）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class MetricMountInput(BaseModel):
    """挂载实体输入（指标创建/更新请求内嵌用）：不含 metric_id——创建时指标尚不存在，
    由 service 以新建 metric.id 落库；更新时以路径指标为准 upsert。"""

    #: 已有挂载行 ID（编辑回传，service 全量 diff 对齐时按 id 匹配：有 id 更新、
    #: 无 id 新增、未出现在请求的删除）；创建场景为 None。
    id: int | None = Field(None, ge=1, description="已有挂载行 ID（编辑回传，创建为空）")
    source_table: str = Field(..., max_length=255, description="源表（可带库前缀）")
    source_column: str = Field(..., max_length=255, description="度量列（映射原子逻辑度量）")
    #: 粒度从 metric.granularity 下沉到此（界限文档 §2.3 第 3 条）
    granularity: str = Field(..., max_length=64, description="粒度（一行代表什么）")
    default_period: str | None = Field(None, max_length=32, description="默认统计周期")
    domain: str = Field(..., max_length=64, description="业务域")
    #: 业务限定（变体级，OneData 派生 = 基础原子 + 业务限定 + 周期）；缺省继承
    #: 指标级 definition_json.business_filter（default_business_filter 兜底）。
    business_filter: str | None = Field(
        None, max_length=512, description="业务限定（变体级，如 病种=门特）"
    )

    @field_validator("source_table", "source_column", "granularity")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("字段不能为空")
        return v

    @field_validator("business_filter")
    @classmethod
    def _filter_blank_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return v
        stripped = v.strip()
        return stripped or None


class MetricMountCreate(BaseModel):
    metric_id: int = Field(..., gt=0, description="所属指标 ID（派生指标）")
    source_table: str = Field(..., max_length=255, description="源表（可带库前缀）")
    source_column: str = Field(..., max_length=255, description="度量列（映射原子逻辑度量）")
    #: 粒度从 metric.granularity 下沉到此（界限文档 §2.3 第 3 条）
    granularity: str = Field(..., max_length=64, description="粒度（一行代表什么）")
    default_period: str | None = Field(None, max_length=32, description="默认统计周期")
    domain: str = Field(..., max_length=64, description="业务域")
    business_filter: str | None = Field(
        None, max_length=512, description="业务限定（变体级，缺省继承指标级）"
    )

    @field_validator("source_table", "source_column", "granularity")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("字段不能为空")
        return v


class MetricMountUpdate(BaseModel):
    source_table: str | None = Field(None, max_length=255)
    source_column: str | None = Field(None, max_length=255)
    granularity: str | None = Field(None, max_length=64)
    default_period: str | None = Field(None, max_length=32)
    domain: str | None = Field(None, max_length=64)
    business_filter: str | None = Field(None, max_length=512)


class MetricMountResponse(BaseModel):
    id: int
    metric_id: int
    source_table: str
    source_column: str
    granularity: str
    default_period: str | None = None
    domain: str
    business_filter: str | None = None
    #: 所属指标编码/名称/类型（list 时 LEFT JOIN Metric 回填，治理展示用）
    metric_code: str | None = None
    metric_name: str | None = None
    metric_type: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any, metric: Any = None) -> MetricMountResponse:
        return cls(
            id=m.id,
            metric_id=m.metric_id,
            source_table=m.source_table,
            source_column=m.source_column,
            granularity=m.granularity,
            default_period=getattr(m, "default_period", None),
            domain=m.domain,
            business_filter=getattr(m, "business_filter", None),
            metric_code=getattr(metric, "metric_code", None) if metric is not None else None,
            metric_name=getattr(metric, "name", None) if metric is not None else None,
            metric_type=getattr(metric, "type", None) if metric is not None else None,
            created_at=getattr(m, "created_at", None),
            updated_at=getattr(m, "updated_at", None),
        )
