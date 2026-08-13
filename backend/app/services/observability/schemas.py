"""可观测性服务 Schemas（TD §12.10 / FR-16）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator

# observability M-1: 限制 feedback 作用对象类型，防止伪造来源
_ALLOWED_TARGET_TYPES = {"metric", "term", "report", "dashboard"}


class FeedbackCreate(BaseModel):
    user_id: int | None = None  # PLAT-2: 以服务端认证身份为准，客户端可不传
    target_type: str
    target_id: str | None = None
    rating: int | None = None
    comment: str | None = None

    @field_validator("target_type")
    @classmethod
    def _validate_target_type(cls, v: str) -> str:
        if v not in _ALLOWED_TARGET_TYPES:
            raise ValueError(f"非法的反馈对象类型: {v}")
        return v


class FeedbackResponse(BaseModel):
    id: int
    user_id: int
    target_type: str
    target_id: str | None = None
    rating: int | None = None
    comment: str | None = None
    status: str = "pending"
    resolution_note: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> FeedbackResponse:
        return cls(
            id=m.id,
            user_id=m.user_id,
            target_type=m.target_type,
            target_id=getattr(m, "target_id", None),
            rating=getattr(m, "rating", None),
            comment=getattr(m, "comment", None),
            status=getattr(m, "status", "pending") or "pending",
            resolution_note=getattr(m, "resolution_note", None),
            created_at=getattr(m, "created_at", None),
        )
