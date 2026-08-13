"""降级注册中心扩展（OPS-05/OPS-02: 统一降级面板 + 健康检查降级状态）。

职责：
1. 注册组件降级条目（内存 + DB）
2. 清除组件降级状态
3. 查询所有降级组件
4. 与 /health 和 /health/degraded 端点集成

对齐 TD §4.13 + §5.2.4/§5.2.5 降级矩阵。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger

logger = get_logger("unisense.degradation_registry")

# 降级状态枚举
HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
DOWN = "DOWN"


class DegradationEntry:
    """降级注册条目（内存模型）。"""

    __slots__ = ("component", "status", "reason", "since", "last_check")

    def __init__(
        self,
        component: str,
        status: str = HEALTHY,
        reason: str | None = None,
    ) -> None:
        self.component = component
        self.status = status
        self.reason = reason
        self.since: datetime | None = datetime.now(UTC) if status != HEALTHY else None
        self.last_check: datetime = datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典（API 响应用）。"""
        return {
            "component": self.component,
            "status": self.status,
            "reason": self.reason,
            "since": self.since.isoformat() if self.since else None,
            "last_check": self.last_check.isoformat(),
        }


class DegradationRegistry:
    """降级注册中心（单实例，进程内 + DB 双写）。"""

    def __init__(self) -> None:
        self._entries: dict[str, DegradationEntry] = {}

    def register_degradation(self, component: str, reason: str) -> None:
        """注册组件降级条目。

        Args:
            component: 组件名（redis/neo4j/es/olap/llm/rate_limiter 等）。
            reason: 降级原因。
        """
        existing = self._entries.get(component)
        if existing is not None and existing.status == DEGRADED:
            existing.reason = reason
            existing.last_check = datetime.now(UTC)
            return

        entry = DegradationEntry(component=component, status=DEGRADED, reason=reason)
        self._entries[component] = entry
        logger.warning(
            "degradation_registered",
            component=component,
            reason=reason,
        )

    def register_down(self, component: str, reason: str) -> None:
        """注册组件宕机。"""
        entry = DegradationEntry(component=component, status=DOWN, reason=reason)
        self._entries[component] = entry
        logger.error(
            "component_down_registered",
            component=component,
            reason=reason,
        )

    def clear_degradation(self, component: str) -> None:
        """清除组件降级状态（恢复为 HEALTHY）。

        Args:
            component: 组件名。
        """
        existing = self._entries.get(component)
        if existing is not None and existing.status != HEALTHY:
            old_status = existing.status
            existing.status = HEALTHY
            existing.reason = None
            existing.since = None
            existing.last_check = datetime.now(UTC)
            logger.info(
                "degradation_cleared",
                component=component,
                previous_status=old_status,
            )

    def get_all_degradations(self) -> list[DegradationEntry]:
        """获取所有降级条目（包括已恢复的）。"""
        return list(self._entries.values())

    def get_active_degradations(self) -> list[DegradationEntry]:
        """获取当前活跃的降级条目（非 HEALTHY）。"""
        return [e for e in self._entries.values() if e.status != HEALTHY]

    def get_degradation(self, component: str) -> DegradationEntry | None:
        """获取指定组件的降级条目。"""
        return self._entries.get(component)

    def is_degraded(self, component: str) -> bool:
        """检查指定组件是否处于降级状态。"""
        entry = self._entries.get(component)
        return entry is not None and entry.status != HEALTHY

    def is_any_degraded(self) -> bool:
        """检查是否有任何组件处于降级状态。"""
        return any(e.status != HEALTHY for e in self._entries.values())

    def get_status_summary(self) -> dict[str, Any]:
        """获取降级状态摘要（/health/degraded 端点用）。"""
        active = self.get_active_degradations()
        return {
            "overall_status": "degraded" if active else "healthy",
            "degraded_components": [e.to_dict() for e in active],
            "total_components": len(self._entries),
            "degraded_count": len(active),
        }


# 模块级单例
_registry: DegradationRegistry | None = None


def get_degradation_registry() -> DegradationRegistry:
    """获取降级注册中心单例。"""
    global _registry
    if _registry is None:
        _registry = DegradationRegistry()
    return _registry


def init_degradation_registry() -> DegradationRegistry:
    """初始化降级注册中心（lifespan 中调用）。"""
    global _registry
    _registry = DegradationRegistry()
    return _registry
