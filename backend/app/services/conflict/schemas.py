"""冲突服务 Schemas（TD §12.4 / FR-09）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.conflict import ConflictStatus, ConflictType


class MetricInput(BaseModel):
    metric_code: str
    domain: str = ""
    definition: str = ""
    source_tables: list[str] = Field(default_factory=list)
    has_pii: bool = False
    pii_authorized: bool = False
    metric_id: int | None = None


class ConflictCheckRequest(BaseModel):
    candidate: MetricInput
    existing: list[MetricInput] = Field(default_factory=list)


class DetectionOut(BaseModel):
    conflict_type: ConflictType
    score: float
    existing_code: str
    existing_metric_id: int | None = None
    severity: str
    block_publish: bool
    reason: str = ""
    llm_confirmed: bool = False


class ConflictCheckResult(BaseModel):
    detections: list[DetectionOut]
    blocked: bool


class ArbitrateRequest(BaseModel):
    decision: Literal["choose_canonical", "merge", "keep_diff"]
    canonical_metric_code: str | None = None
    arbitrator_id: int
    reason: str = ""
    rule_template: str | None = None


class EscalateRequest(BaseModel):
    note: str = ""


class ConflictListParams(BaseModel):
    status: ConflictStatus | None = None
    type: ConflictType | None = None
    domain: str | None = None
    page: int = 1
    page_size: int = 20


class ConflictResponse(BaseModel):
    id: int
    conflict_id: str
    metric_a: int | None = None
    metric_b: int | None = None
    type: ConflictType
    status: ConflictStatus
    domain: str | None = None
    similarity_score: float
    metric_codes: dict[str, Any] | None = None
    arbitrator_id: int | None = None
    decision_json: dict[str, Any] | None = None
    created_at: datetime | None = None
    resolved_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> ConflictResponse:
        return cls(
            id=m.id,
            conflict_id=m.conflict_id,
            metric_a=m.metric_a,
            metric_b=m.metric_b,
            type=m.type,
            status=m.status,
            domain=m.domain,
            similarity_score=m.similarity_score,
            metric_codes=m.metric_codes,
            arbitrator_id=m.arbitrator_id,
            decision_json=m.decision_json,
            created_at=m.created_at,
            resolved_at=m.resolved_at,
        )


class RulingRecordResponse(BaseModel):
    id: int
    conflict_id: str
    metric_codes: dict[str, Any] | None = None
    dispute_desc: str | None = None
    decision: str | None = None
    reason: str | None = None
    arbitrator_id: int | None = None
    decided_at: datetime | None = None
