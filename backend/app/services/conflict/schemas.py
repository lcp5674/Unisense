"""冲突服务 Schemas（TD §12.4 / FR-09）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    # 同名不同义「保留差异+指定一方改名」（TD §12.4 扩展）：仲裁者认定两指标
    # 语义确有差异、需共存，但同名会造成混淆——指定候选或现有指标改名以区分。
    # rename_target 用「角色」标识改名方（candidate/existing）——同名冲突下
    # candidate 与 existing 的 metric_code 天然相同（检测以 cand_code==ext_code 触发），
    # 用 code 无法区分，故以角色定位；rename_metric_code 为兼容旧调用保留（取
    # 冲突双方之一），二者同时提供时以 rename_target 为准。
    rename_target: Literal["candidate", "existing"] | None = None
    rename_metric_code: str | None = None
    arbitrator_id: int | None = None  # PLAT-2: 以服务端认证身份为准，客户端可不传
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
    # B1-1: 前端 types.ts 依赖的字段（从 metric_codes / decision_json / created_at 推导）
    severity: str | None = None
    candidate_metric_code: str | None = None
    existing_metric_code: str | None = None
    description: str | None = None
    detected_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> ConflictResponse:
        mc = m.metric_codes or {}
        dj = m.decision_json or {}
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
            # B1-1: 推导缺失字段
            severity=dj.get("status") or dj.get("severity"),
            candidate_metric_code=mc.get("candidate"),
            existing_metric_code=mc.get("existing"),
            description=mc.get("description"),
            detected_at=m.created_at,
        )


class RulingRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conflict_id: str
    metric_codes: dict[str, Any] | None = None
    dispute_desc: str | None = None
    decision: str | None = None
    reason: str | None = None
    arbitrator_id: int | None = None
    decided_at: datetime | None = None
