"""可观测性服务 Schemas（TD §12.10 / FR-16）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator

# observability M-1: 限制 feedback 作用对象类型，防止伪造来源
_ALLOWED_TARGET_TYPES = {"metric", "term", "report", "dashboard"}
# 反馈分类 / 优先级合法值（非法值回退默认，容错客户端脏数据）
_ALLOWED_CATEGORIES = {"bug", "feature", "improvement", "question", "praise"}
_ALLOWED_PRIORITIES = {"high", "medium", "low"}


class FeedbackCreate(BaseModel):
    user_id: int | None = None  # PLAT-2: 以服务端认证身份为准，客户端可不传
    target_type: str
    target_id: str | None = None
    rating: int | None = None
    comment: str | None = None
    #: 反馈分类（bug/feature/improvement/question/praise），非法值回退 improvement
    category: str | None = None
    #: 反馈优先级（high/medium/low），非法值回退 medium
    priority: str | None = None
    #: 反馈来源页面 URL（自动捕获，不要求用户填写）
    source_url: str | None = None

    @field_validator("target_type")
    @classmethod
    def _validate_target_type(cls, v: str) -> str:
        if v not in _ALLOWED_TARGET_TYPES:
            raise ValueError(f"非法的反馈对象类型: {v}")
        return v

    @field_validator("category")
    @classmethod
    def _validate_category(cls, v: str | None) -> str | None:
        if v is not None and v not in _ALLOWED_CATEGORIES:
            return "improvement"
        return v

    @field_validator("priority")
    @classmethod
    def _validate_priority(cls, v: str | None) -> str | None:
        if v is not None and v not in _ALLOWED_PRIORITIES:
            return "medium"
        return v


class FeedbackResponse(BaseModel):
    id: int
    user_id: int
    target_type: str
    target_id: str | None = None
    #: 反馈对象名称（服务端批量解析，前端直显避免 N+1 探测；对象失效/删除时为 None）
    target_name: str | None = None
    rating: int | None = None
    nps_score: int | None = None
    category: str = "improvement"
    priority: str = "medium"
    source_url: str | None = None
    comment: str | None = None
    status: str = "pending"
    resolution_note: str | None = None
    resolver_id: int | None = None
    resolved_at: datetime | None = None
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> FeedbackResponse:
        return cls(
            id=m.id,
            user_id=m.user_id,
            target_type=m.target_type,
            target_id=getattr(m, "target_id", None),
            rating=getattr(m, "rating", None),
            nps_score=getattr(m, "nps_score", None),
            category=getattr(m, "category", "improvement") or "improvement",
            priority=getattr(m, "priority", "medium") or "medium",
            source_url=getattr(m, "source_url", None),
            comment=getattr(m, "comment", None),
            status=getattr(m, "status", "pending") or "pending",
            resolution_note=getattr(m, "resolution_note", None),
            resolver_id=getattr(m, "resolver_id", None),
            resolved_at=getattr(m, "resolved_at", None),
            created_at=getattr(m, "created_at", None),
        )
