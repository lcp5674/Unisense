"""审计日志模型（WORM 表，禁止 UPDATE/DELETE）。

对齐 TD §4.1 audit_log 表和 §15.4 审计合规。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, String
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import TimestampMixin


class AuditLog(Base, TimestampMixin):
    """操作审计日志（WORM：只写不删）。

    全写操作审计：actor/action/entity/detail/ip/trace_id。
    禁止 UPDATE/DELETE（MySQL 触发器强制，对齐 TD §4.1）。
    不继承 SoftDeleteMixin（不可删除）。

    Attributes:
        actor_id: 操作人 ID。
        action: 操作类型。
        entity_type: 实体类型。
        entity_id: 实体 ID。
        detail_json: 操作详情（before/after diff）。
        ip: 操作 IP。
        trace_id: 链路追踪 ID。
        pii_access: 是否涉及 PII 访问。
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, comment="主键 ID")
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("user.id", name="fk_audit_log_user"),
        nullable=True,
        comment=(
            "操作人 ID（系统级事件如登录失败无对应用户时为 NULL，X-4；"
            "BigInteger 对齐 user.id）"
        ),
    )
    action: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="操作类型（CREATE/UPDATE/DELETE/PUBLISH 等）"
    )
    entity_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="实体类型（metric/data_source/term 等）"
    )
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="实体 ID")
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="操作详情（before/after diff）"
    )
    ip: Mapped[str] = mapped_column(String(64), nullable=False, comment="操作 IP")
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="链路追踪 ID")
    pii_access: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否涉及 PII 访问"
    )
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否已归档（冷热分离）"
    )

    __table_args__ = (
        Index("idx_audit_log_actor", "actor_id"),
        Index("idx_audit_log_entity", "entity_type", "entity_id"),
        Index("idx_audit_log_trace", "trace_id"),
        Index("idx_audit_log_archived", "archived", "created_at"),
    )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（供审计 API 返回）。"""
        return {
            "id": self.id,
            "actor_id": self.actor_id,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "detail_json": self.detail_json,
            "ip": self.ip,
            "trace_id": self.trace_id,
            "pii_access": self.pii_access,
            "archived": self.archived,
            "created_at": self.created_at,
        }
