"""冲突事件发布（best-effort，熔断降级，对齐 lineage.events）。

冲突服务不阻塞发布主流程：notify/governance 不可达时降级为静默丢弃，
仅通过 CircuitBreaker 保护后续重试，避免外部依赖抖动拖垮仲裁 API。
仅发布 notify.todo 通知（给治理角色）+ governance.pii_review 路由（PII 冲突）。
不引入同步 HTTP 依赖，发布为 fire-and-forget 异步任务。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from app.core.resilience import CircuitBreaker

logger = logging.getLogger("unisense.conflict.events")

_BREAKER = CircuitBreaker(failure_threshold=5, reset_timeout=30.0)


class ConflictEventPublisher:
    """轻量异步事件发布器；外部 notify 服务未就绪时静默降级。"""

    def __init__(self, notify_url: str | None = None) -> None:
        self._notify_url = notify_url
        self._http_client: Any | None = None

    async def publish(self, event: dict[str, Any]) -> None:
        if not self._allow():
            logger.warning("conflict 事件熔断开启，丢弃事件 %s", event.get("event_type"))
            return
        try:
            await self._send(event)
            _BREAKER.record_success()
        except Exception as exc:  # noqa: BLE001 - 降级：不向上抛
            _BREAKER.record_failure()
            logger.warning("conflict 事件发布失败（降级）：%s", exc)

    def _allow(self) -> bool:
        return _BREAKER.allow()

    async def _send(self, event: dict[str, Any]) -> None:
        if not self._notify_url:
            return  # 无 notify 端点配置 → 静默降级
        import httpx

        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=2.0)
        # 复用共享客户端：不能用 `async with` 包裹（其 __aexit__ 会 aclose 客户端，
        # 导致第二次发布抛 "client has been closed"）；直接 await post 保持连接复用。
        resp = await self._http_client.post(f"{self._notify_url}/api/v1/notify/events", json=event)
        if resp.status_code >= 300:
            raise RuntimeError(f"notify 返回 {resp.status_code}: {resp.text[:200]}")

    async def close(self) -> None:
        if self._http_client is not None:
            with contextlib.suppress(Exception):
                await self._http_client.aclose()
            self._http_client = None
