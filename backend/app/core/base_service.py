"""服务基类 Protocol（对齐 TD §5.5 / DEV_GUIDE §14）。

统一服务注入模式：db + eventbus + settings，
提供审计写入和事件发布辅助方法，所有服务逐步迁移继承。
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.config import Settings
from app.core.eventbus import EventBus

logger = structlog.get_logger("unisense.base_service")


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
        # T1（审查修复）：事件延迟到事务提交后投递（after_commit），杜绝
        # publish-before-commit——commit 失败时事件队列不投递，订阅方不会
        # 收到「已创建/已回滚」但库中不存在的消息。
        self._pending_events: list[tuple[str, dict[str, Any], str]] = []
        # R4（审查修复）：after_commit 投递任务的强引用集合——防止 create_task
        # 返回值被 GC 回收导致事件静默丢失（与 degradation.py 同范式）。
        self._in_flight_tasks: set[asyncio.Task[Any]] = set()
        try:
            sa_event.listen(db.sync_session, "after_commit", self._on_after_commit)
        except Exception as exc:  # noqa: BLE001 - 测试 mock 会话或非 SQLAlchemy 会话时忽略
            # R4（审查修复）：注册失败不再静默——否则事件永不 flush 且无任何日志，
            # 治理/通知事件全部消失，运营侧「通知中心收不到升级提醒」无法定位。
            logger.warning(
                "after_commit_listener_register_failed",
                error=str(exc),
                pending=0,
            )

    async def _write_audit(
        self,
        *,
        actor_id: int,
        action: str,
        entity_type: str,
        entity_id: str,
        detail: dict[str, Any],
        ip: str = "",
        trace_id: str = "",
        pii_access: bool = False,
    ) -> None:
        """写入审计记录（仅 add 到会话，由调用方 commit）。

        S5（审查修复）：新增 ``ip`` 必填语义（事件来源，满足等保「审计记录
        包含事件来源」）；服务层无法取得 request 时可留空，API 层应尽量透传
        ``client_ip(request)``。
        """
        await write_audit(
            self._db,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail,
            ip=ip,
            trace_id=trace_id,
            pii_access=pii_access,
        )

    async def _publish_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        actor_id: str = "",
    ) -> None:
        """收集事件到待投递队列（T1：事务提交后统一投递，见 ``_on_after_commit``）。

        Args:
            event_type: 事件类型。
            payload: 事件负载。
            actor_id: 事件发起者 ID。
        """
        self._pending_events.append((event_type, payload, actor_id))

    def _on_after_commit(self, _session: Any) -> None:
        """SQLAlchemy after_commit 回调：事务提交成功后异步投递待发事件。

        同步回调内不能 await，故 create_task 派发；commit 失败/回滚时本回调
        不触发，事件队列随 service 实例丢弃（best-effort 语义，不补发）。
        """
        if not self._pending_events:
            return
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._flush_pending_events())
            # R4（审查修复）：保存强引用防 GC，完成后自动移除
            self._in_flight_tasks.add(task)
            task.add_done_callback(self._in_flight_tasks.discard)
        except RuntimeError:
            # 无运行循环（CLI/极少数同步场景）：事件丢失，仅告警
            logger.warning(
                "event_flush_no_running_loop",
                pending=len(self._pending_events),
            )

    async def _flush_pending_events(self) -> None:
        """逐个投递待发事件并清空队列（best-effort，失败仅告警）。"""
        events, self._pending_events = self._pending_events, []
        for event_type, payload, actor_id in events:
            try:
                await self._eventbus.publish(event_type, payload, actor_id)
            except Exception as exc:  # noqa: BLE001 - best-effort 不阻断业务
                logger.warning(
                    "event_publish_failed",
                    event_type=event_type,
                    error=str(exc),
                )

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
