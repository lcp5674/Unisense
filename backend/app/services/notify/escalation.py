"""告警升级服务（TD §12.9 扩展：事件分级触达 + 状态化重试/升级）。

对已产生的业务事件（quality/conflict/governance 等）按严重级执行
升级策略：确定触达频率、目标角色，并经 EventBus 发布 ``escalation.triggered``
事件，由 notify 消费者落 EventLog 并按订阅扇出投递（Webhook/钉钉/SMTP/console）。

分级约定：
- P0（严重）：阻断业务 / 数据损坏。立即触达 + 每 10 分钟重试（最多 6 次）。
- P1（高）：主流程受损。立即触达 + 每 30 分钟重试（最多 4 次）。
- P2（中）：局部影响。立即触达，不重复。

状态化重试（B2）：
- 每次 ``escalate`` 在注入 session 时落 ``escalation_record``（attempts=1）。
- 周期任务 ``check_escalation_retries`` 调用 ``check_retries()``：
  到点未确认 → 重发（attempts+1）；达到当前级别上限 → 逐级升级
  （P2→P1→P0）并重置计数；已是 P0 且到上限 → ``MAXED_OUT`` 停止。
- ``acknowledge()`` 人工确认后停止重试。

任何失败仅记日志，不阻断业务主流程（best-effort）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.escalation import EscalationRecord, EscalationStatus

logger = get_logger("unisense.notify.escalation")

#: 升级策略表：level → 策略。
#: repeat_minutes=0 表示不重复；target_roles 供运维订阅/值班路由参考。
_ESCALATION_POLICY: dict[str, dict[str, Any]] = {
    "P0": {
        "label": "严重",
        "repeat_minutes": 10,
        "max_repeats": 6,
        "target_roles": ("platform_admin", "domain_admin"),
    },
    "P1": {
        "label": "高",
        "repeat_minutes": 30,
        "max_repeats": 4,
        "target_roles": ("domain_admin", "metric_owner"),
    },
    "P2": {
        "label": "中",
        "repeat_minutes": 0,
        "max_repeats": 0,
        "target_roles": ("metric_owner",),
    },
}

#: 合法级别集合；非法级别降级为 P2（防误配置导致触达失效）。
_ALLOWED_LEVELS = frozenset({"P0", "P1", "P2"})

#: 逐级升级映射：当前级别达到重试上限后升级到上一级（P0 为最高，无更高级）。
_LEVEL_UPGRADE: dict[str, str] = {"P2": "P1", "P1": "P0", "P0": "P0"}


def resolve_policy(level: str | None) -> dict[str, Any]:
    """解析升级策略；非法/未知级别回落到 P2。"""
    key = (level or "").upper()
    if key not in _ALLOWED_LEVELS:
        logger.warning("escalation_unknown_level_fallback_p2", level=level)
        key = "P2"
    policy = _ESCALATION_POLICY[key]
    return {
        "level": key,
        "label": policy["label"],
        "repeat_minutes": policy["repeat_minutes"],
        "max_repeats": policy["max_repeats"],
        "target_roles": list(policy["target_roles"]),
    }


class EscalationService:
    """告警升级编排：解析策略 + 状态持久化 + 重试/升级扫描（best-effort）。"""

    def __init__(self, bus: Any | None = None, session: AsyncSession | None = None) -> None:
        self._bus = bus
        self._session = session

    def _get_bus(self) -> Any:
        """惰性取 EventBus（延迟 import，避免测试/CLI 上下文强依赖）。"""
        if self._bus is None:
            from app.core.eventbus import get_eventbus

            self._bus = get_eventbus()
        return self._bus

    async def escalate(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        level: str | None = None,
        actor_id: str | int | None = None,
        source_ref: str | None = None,
    ) -> dict[str, Any]:
        """执行一次告警升级（落状态记录 + 发布升级事件）。

        Args:
            event_type: 原始业务事件类型（如 ``quality.anomaly``）。
            payload: 事件负载（含 metric_id/告警详情等）。
            level: 严重级（P0/P1/P2），非法回落 P2。
            actor_id: 触发者（可选，审计归因）。
            source_ref: 来源引用（如 ``quality_event:{id}``，可选，用于去重与恢复定位）。

        Returns:
            升级结果：``{event_type, level, label, policy, published, record_id}``。
        """
        policy = resolve_policy(level)
        base = {
            "event_type": event_type,
            "level": policy["level"],
            "label": policy["label"],
            "policy": policy,
        }
        record_id: int | None = None
        # 落状态记录（session 注入时才持久化；P2 不重复 → next_retry_at=None）
        if self._session is not None:
            rec = EscalationRecord(
                event_type=event_type,
                source_ref=source_ref,
                level=policy["level"],
                label=policy["label"],
                attempts=1,
                max_attempts=policy["max_repeats"],
                next_retry_at=(
                    datetime.now(UTC) + timedelta(minutes=policy["repeat_minutes"])
                    if policy["repeat_minutes"] > 0
                    else None
                ),
                status=EscalationStatus.ESCALATED,
                last_payload=payload,
                actor_id=str(actor_id) if actor_id is not None else None,
            )
            self._session.add(rec)
            await self._session.flush()
            record_id = rec.id

        published = await self._publish_event(
            event_type,
            policy,
            payload=payload,
            actor_id=str(actor_id) if actor_id is not None else None,
            retry=False,
            attempt=1,
        )
        return {**base, "published": published, "record_id": record_id}

    async def acknowledge(self, record_id: int) -> bool:
        """人工确认升级（停止重试）。

        Args:
            record_id: escalation_record 主键。

        Returns:
            True 表示确认成功（记录存在且处于 ESCALATED）。
        """
        if self._session is None:
            return False
        rec = await self._session.get(EscalationRecord, record_id)
        if rec is None or rec.status != EscalationStatus.ESCALATED:
            return False
        rec.status = EscalationStatus.ACKNOWLEDGED
        rec.next_retry_at = None
        await self._session.flush()
        logger.info("escalation_acknowledged", record_id=record_id)
        return True

    async def check_retries(self) -> dict[str, int]:
        """扫描到点未确认的升级并驱动重试/逐级升级（由周期任务调用）。

        Returns:
            ``{due, resent, escalated, maxed_out}`` 统计（仅统计本次扫描命中）。
        """
        if self._session is None:
            return {"due": 0, "resent": 0, "escalated": 0, "maxed_out": 0}
        now = datetime.now(UTC)
        stmt = (
            select(EscalationRecord)
            .where(
                EscalationRecord.status == EscalationStatus.ESCALATED,
                EscalationRecord.next_retry_at.is_not(None),
                EscalationRecord.next_retry_at <= now,
            )
            .order_by(EscalationRecord.next_retry_at)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        resent = 0
        escalated = 0
        maxed_out = 0
        for rec in rows:
            if rec.attempts < rec.max_attempts:
                # 到点重发（attempts+1）
                policy = resolve_policy(rec.level)
                await self._publish_event(
                    rec.event_type,
                    policy,
                    payload=rec.last_payload,
                    actor_id=rec.actor_id,
                    retry=True,
                    attempt=rec.attempts + 1,
                )
                rec.attempts += 1
                rec.next_retry_at = (
                    now + timedelta(minutes=policy["repeat_minutes"])
                    if policy["repeat_minutes"] > 0
                    else None
                )
                resent += 1
                continue
            # 达到当前级别上限 → 逐级升级 或 最高级打满停止
            higher = _LEVEL_UPGRADE.get(rec.level, rec.level)
            if higher != rec.level:
                new_policy = resolve_policy(higher)
                await self._publish_event(
                    rec.event_type,
                    new_policy,
                    payload=rec.last_payload,
                    actor_id=rec.actor_id,
                    retry=True,
                    attempt=1,
                    escalated=True,
                )
                rec.level = higher
                rec.label = new_policy["label"]
                rec.attempts = 1
                rec.max_attempts = new_policy["max_repeats"]
                rec.next_retry_at = (
                    now + timedelta(minutes=new_policy["repeat_minutes"])
                    if new_policy["repeat_minutes"] > 0
                    else None
                )
                escalated += 1
            else:
                rec.status = EscalationStatus.MAXED_OUT
                rec.next_retry_at = None
                maxed_out += 1
            logger.info(
                "escalation_state_changed",
                record_id=rec.id,
                status=rec.status,
                level=rec.level,
                attempts=rec.attempts,
            )
        return {"due": len(rows), "resent": resent, "escalated": escalated, "maxed_out": maxed_out}

    async def _publish_event(
        self,
        event_type: str,
        policy: dict[str, Any],
        *,
        payload: dict[str, Any] | None,
        actor_id: str | None,
        retry: bool,
        attempt: int,
        escalated: bool = False,
    ) -> bool:
        """发布升级事件到 EventBus（best-effort，失败仅记日志）。"""
        try:
            bus = self._get_bus()
            await bus.publish(
                "escalation.triggered",
                {
                    "source_event_type": event_type,
                    "level": policy["level"],
                    "label": policy["label"],
                    "target_roles": policy["target_roles"],
                    "payload": payload or {},
                    "actor_id": actor_id or "",
                    "retry": retry,
                    "attempt": attempt,
                    "escalated": escalated,
                },
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 升级失败不阻断业务主流程
            logger.warning("escalation_publish_failed", event_type=event_type, error=str(exc))
            return False
