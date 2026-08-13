"""服务基类 Protocol（对齐 TD §5.5 / DEV_GUIDE §14）。

统一服务注入模式：db + eventbus + settings，
提供审计写入和事件发布辅助方法，所有服务逐步迁移继承。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.config import Settings
from app.core.eventbus import EventBus


@runtime_checkable
class BaseServiceProtocol(Protocol):
    """服务基类协议：统一注入 + 辅助方法。"""

    _db: AsyncSession
    _eventbus: EventBus
    _settings: Settings


class BaseService:
    """服务基类：提供审计写入和事件发布辅助方法。

    子类继承后自动获得：
    - _write_audit: 审计记录写入（仅 add，调用方负责 commit）
    - _publish_event: 事件发布（best-effort）
    """

    def __init__(
        self,
        db: AsyncSession,
        eventbus: EventBus | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._db = db
        self._eventbus = eventbus or self._get_default_eventbus()
        self._settings = settings or self._get_default_settings()

    async def _write_audit(
        self,
        *,
        actor_id: int,
        action: str,
        entity_type: str,
        entity_id: str,
        detail: dict[str, Any],
        trace_id: str = "",
        pii_access: bool = False,
    ) -> None:
        """写入审计记录（仅 add 到会话，由调用方 commit）。

        Args:
            actor_id: 操作者 ID。
            action: 操作类型。
            entity_type: 实体类型。
            entity_id: 实体 ID。
            detail: 操作详情。
            trace_id: 链路追踪 ID。
            pii_access: 是否涉及 PII 数据访问。
        """
        await write_audit(
            self._db,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail,
            trace_id=trace_id,
            pii_access=pii_access,
        )

    async def _publish_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        actor_id: str = "",
    ) -> None:
        """发布事件（best-effort，失败仅告警）。

        Args:
            event_type: 事件类型。
            payload: 事件负载。
            actor_id: 事件发起者 ID。
        """
        await self._eventbus.publish(event_type, payload, actor_id)

    @staticmethod
    def _get_default_eventbus() -> EventBus:
        """获取默认 EventBus 实例。"""
        from app.core.eventbus import get_eventbus

        return get_eventbus()

    @staticmethod
    def _get_default_settings() -> Settings:
        """获取默认 Settings 实例。"""
        from app.core.config import settings

        return settings

    async def commit(self) -> None:
        """Unit of Work 提交：封装 db.commit()（T048 UoW 渐进迁移）。

        对齐 PLAT-3：业务写入 + 审计同事务原子提交。
        渐进迁移策略：新代码优先调用 service.commit()，
        存量 API 层的 await db.commit() 逐步替换（按模块推进）。
        """
        await self._db.commit()
