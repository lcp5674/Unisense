"""告警升级服务（TD §12.9 扩展：事件分级触达与升级路由）。

对已产生的业务事件（quality/conflict/governance 等）按严重级执行
升级策略：确定触达频率、目标角色，并经 EventBus 发布 ``escalation.triggered``
事件，由 notify 消费者落 EventLog 并按订阅扇出投递（Webhook/钉钉/SMTP/console）。

分级约定：
- P0（严重）：阻断业务 / 数据损坏。立即触达 + 每 10 分钟重试（最多 6 次）。
- P1（高）：主流程受损。立即触达 + 每 30 分钟重试（最多 4 次）。
- P2（中）：局部影响。立即触达，不重复。

重试由上层调度（如 arq cron）驱动；本服务幂等记录每次升级事件，
不自行循环（避免事件风暴）。任何失败仅记日志，不阻断业务主流程（best-effort）。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

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
    """告警升级编排：解析策略 + 发布升级事件（best-effort）。"""

    def __init__(self, bus: Any | None = None) -> None:
        self._bus = bus

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
    ) -> dict[str, Any]:
        """执行一次告警升级。

        Args:
            event_type: 原始业务事件类型（如 ``quality.anomaly``）。
            payload: 事件负载（含 metric_id/告警详情等）。
            level: 严重级（P0/P1/P2），非法回落 P2。
            actor_id: 触发者（可选，审计归因）。

        Returns:
            升级结果：``{event_type, level, label, policy, published}``。
        """
        policy = resolve_policy(level)
        base = {
            "event_type": event_type,
            "level": policy["level"],
            "label": policy["label"],
            "policy": policy,
        }
        published = False
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
                    "actor_id": str(actor_id) if actor_id is not None else "",
                },
            )
            published = True
        except Exception as exc:  # noqa: BLE001 - 升级失败不阻断业务主流程
            logger.warning("escalation_publish_failed", event_type=event_type, error=str(exc))

        return {**base, "published": published}
