"""通知服务 Schemas（TD §12.9 / FR-16 / FR-17）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator

# PLAT-5: 限制 source/level 取值，防止 client 伪造异常来源或告警级别
_ALLOWED_SOURCES = {
    "metric",
    "lineage",
    "quality",
    "governance",
    "semantic",
    "system",
    "scheduler",
    # 三梯队通知接入新增来源（采集/血缘断链、账号安全/组织、系统降级）
    "catalog",
    "collect",
    "user",
    "org",
    "degradation",
}
_ALLOWED_LEVELS = {"INFO", "WARN", "ERROR", "CRITICAL"}


class EventPublish(BaseModel):
    event_type: str
    source: str | None = None
    payload: dict[str, Any] | None = None
    level: str = "INFO"
    actor_id: int | None = None
    actor_name: str | None = None

    @field_validator("source")
    @classmethod
    def _validate_source(cls, v: str | None) -> str | None:
        if v is not None and v not in _ALLOWED_SOURCES:
            raise ValueError(f"非法的事件来源: {v}")
        return v

    @field_validator("level")
    @classmethod
    def _validate_level(cls, v: str) -> str:
        if v not in _ALLOWED_LEVELS:
            raise ValueError(f"非法的事件级别: {v}")
        return v


class NotificationResponse(BaseModel):
    id: int
    subscriber_id: int
    channel: str
    template_code: str | None = None
    title: str
    body: str | None = None
    status: str
    ref_type: str | None = None
    ref_id: int | None = None
    sent_at: datetime | None = None
    payload: dict[str, Any] | None = None
    send_at: datetime | None = None
    read_at: datetime | None = None
    created_at: datetime | None = None
    actor_id: int | None = None
    actor_name: str | None = None
    last_error: str | None = None
    handled_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> NotificationResponse:
        return cls(
            id=m.id,
            subscriber_id=m.subscriber_id,
            channel=m.channel,
            template_code=getattr(m, "template_code", None),
            title=m.title,
            body=getattr(m, "body", None),
            status=m.status,
            ref_type=getattr(m, "ref_type", None),
            ref_id=getattr(m, "ref_id", None),
            sent_at=getattr(m, "sent_at", None),
            payload=getattr(m, "payload", None),
            send_at=getattr(m, "send_at", None),
            read_at=getattr(m, "read_at", None),
            created_at=getattr(m, "created_at", None),
            actor_id=getattr(m, "actor_id", None),
            actor_name=getattr(m, "actor_name", None),
            last_error=getattr(m, "last_error", None),
            handled_at=getattr(m, "handled_at", None),
        )


class EventLogResponse(BaseModel):
    id: int
    event_type: str
    source: str | None = None
    payload: dict[str, Any] | None = None
    level: str
    notified: bool
    created_at: datetime | None = None
    actor_id: int | None = None
    actor_name: str | None = None

    @classmethod
    def from_model(cls, m: Any) -> EventLogResponse:
        return cls(
            id=m.id,
            event_type=m.event_type,
            source=getattr(m, "source", None),
            payload=getattr(m, "payload", None),
            level=m.level,
            notified=getattr(m, "notified", False),
            created_at=getattr(m, "created_at", None),
            actor_id=getattr(m, "actor_id", None),
            actor_name=getattr(m, "actor_name", None),
        )


class SubscriptionUpsert(BaseModel):
    # PLAT-2: user_id 允许客户端省略，服务端以认证身份覆盖（防越权绑定他人订阅）。
    user_id: int | None = None
    channel: str
    event_type: str | None = None
    #: 资产维度订阅（按指标/源表 watch）：asset_type+asset_id 与 event_type 二选一。
    #: asset_type 提供时 asset_id 必填，且二者组合须存在（服务端校验资产存在性）。
    asset_type: str | None = None
    asset_id: str | None = None
    enabled: bool = True
    threshold: int | None = None

    @field_validator("channel")
    @classmethod
    def _normalize_channel(cls, v: str) -> str:
        """统一渠道到 NotifyChannel 的规范 value（EMAIL/SMS/WEBHOOK/IN_APP/DINGTALK/console）。

        避免大小写漂移：DB 列以枚举 value 存储（大写，console 为小写），若客户端传
        小写 "webhook" 等，直接落库会导致幂等查询 miss 与 _dispatch 命中失败。
        """
        from app.models.notify import NotifyChannel

        v = (v or "").strip()
        try:
            # 按 name 匹配（console 的 name=CONSOLE, value=console）
            return NotifyChannel[v.upper()].value
        except KeyError:
            try:
                return NotifyChannel(v).value  # 按 value 匹配
            except ValueError as exc:
                raise ValueError(f"非法通知渠道: {v}") from exc

    @field_validator("asset_type")
    @classmethod
    def _validate_asset_type(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if v not in ("METRIC", "TABLE"):
            raise ValueError(f"非法资产类型: {v}（仅支持 METRIC/TABLE）")
        return v


class SubscriptionResponse(BaseModel):
    id: int
    user_id: int
    channel: str
    event_type: str | None = None
    asset_type: str | None = None
    asset_id: str | None = None
    enabled: bool
    threshold: int | None = None
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> SubscriptionResponse:
        return cls(
            id=m.id,
            user_id=m.user_id,
            channel=m.channel,
            event_type=getattr(m, "event_type", None),
            asset_type=getattr(m, "asset_type", None),
            asset_id=getattr(m, "asset_id", None),
            enabled=getattr(m, "enabled", True),
            threshold=getattr(m, "threshold", None),
            created_at=getattr(m, "created_at", None),
        )
