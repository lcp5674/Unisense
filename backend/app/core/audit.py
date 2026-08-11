"""审计写入工具（对齐 TD §15.4 审计合规 / DEV_GUIDE §15）。

提供 PII 访问等敏感操作的审计落库能力。仅 ``add`` 到会话，调用方负责 ``commit``。
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request

from app.models.audit import AuditLog


def client_ip(request: Request | None) -> str:
    """从 X-Forwarded-For 或直连地址提取客户端 IP。"""
    if request is None:
        return ""
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


async def write_audit(
    session: Any,
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
    """写入一条审计记录（仅 add，由调用方负责 commit）。"""
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        detail_json=detail,
        ip=ip,
        trace_id=trace_id,
        pii_access=pii_access,
    )
    session.add(entry)
