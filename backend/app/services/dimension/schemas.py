"""维度管理 Schemas（TD §12.15 / FR-05 / FR-09）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class DimensionCreate(BaseModel):
    dim_code: str
    name: str
    domain: str
    type: str = "SCD1"
    description: str | None = None
    owner_id: int


class DimensionUpdate(BaseModel):
    name: str | None = None
    domain: str | None = None
    type: str | None = None
    description: str | None = None


class DimensionResponse(BaseModel):
    id: int
    dim_code: str
    name: str
    domain: str
    type: str
    description: str | None = None
    owner_id: int
    status: str

    @classmethod
    def from_model(cls, m: Any) -> DimensionResponse:
        return cls(
            id=m.id,
            dim_code=m.dim_code,
            name=m.name,
            domain=m.domain,
            type=m.type,
            description=getattr(m, "description", None),
            owner_id=m.owner_id,
            status=m.status,
        )


class DimensionMemberCreate(BaseModel):
    dim_code: str
    member_code: str
    member_name: str
    parent_code: str | None = None
    path: str | None = None
    attributes: dict[str, Any] | None = None
    status: str = "PUBLISHED"


class DimensionMemberResponse(BaseModel):
    id: int
    dim_code: str
    member_code: str
    member_name: str
    parent_code: str | None = None
    path: str | None = None
    attributes: dict[str, Any] | None = None
    status: str

    @classmethod
    def from_model(cls, m: Any) -> DimensionMemberResponse:
        return cls(
            id=m.id,
            dim_code=m.dim_code,
            member_code=m.member_code,
            member_name=m.member_name,
            parent_code=getattr(m, "parent_code", None),
            path=getattr(m, "path", None),
            attributes=getattr(m, "attributes", None),
            status=m.status,
        )


class DimensionMappingCreate(BaseModel):
    source_dim_code: str
    target_dim_code: str
    mapping_type: str
    expression: str | None = None
    created_by: int


class DimensionMappingResponse(BaseModel):
    id: int
    source_dim_code: str
    target_dim_code: str
    mapping_type: str
    expression: str | None = None
    created_by: int

    @classmethod
    def from_model(cls, m: Any) -> DimensionMappingResponse:
        return cls(
            id=m.id,
            source_dim_code=m.source_dim_code,
            target_dim_code=m.target_dim_code,
            mapping_type=m.mapping_type,
            expression=getattr(m, "expression", None),
            created_by=m.created_by,
        )


class MetricDimensionBind(BaseModel):
    metric_id: int
    dim_code: str
    role: str
    default_member: str | None = None


class MetricDimensionResponse(BaseModel):
    id: int
    metric_id: int
    dim_code: str
    role: str
    default_member: str | None = None

    @classmethod
    def from_model(cls, m: Any) -> MetricDimensionResponse:
        return cls(
            id=m.id,
            metric_id=m.metric_id,
            dim_code=m.dim_code,
            role=m.role,
            default_member=getattr(m, "default_member", None),
        )


class ReconciliationSubmit(BaseModel):
    metric_id: int
    dim_code: str | None = None
    expected_expr: str
    actual_expr: str
    diff_summary: str | None = None


class ReconciliationReview(BaseModel):
    decision: str  # APPROVED | REJECTED
    reviewer_id: int


class ReconciliationResponse(BaseModel):
    id: int
    metric_id: int
    dim_code: str | None = None
    expected_expr: str
    actual_expr: str
    status: str
    diff_summary: str | None = None
    reviewed_by: int | None = None
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> ReconciliationResponse:
        return cls(
            id=m.id,
            metric_id=m.metric_id,
            dim_code=getattr(m, "dim_code", None),
            expected_expr=m.expected_expr,
            actual_expr=m.actual_expr,
            status=m.status,
            diff_summary=getattr(m, "diff_summary", None),
            reviewed_by=getattr(m, "reviewed_by", None),
            created_at=getattr(m, "created_at", None),
        )
