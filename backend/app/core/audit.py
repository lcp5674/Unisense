"""审计写入工具（对齐 TD §15.4 审计合规 / DEV_GUIDE §15）。

提供 PII 访问等敏感操作的审计落库能力。仅 ``add`` 到会话，调用方负责 ``commit``。
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request

from app.models.audit import AuditLog


def client_ip(request: Request | None) -> str:
    """从 X-Forwarded-For 或直连地址提取客户端 IP。

    仅当直连 IP 在 trusted_proxies 白名单中时才信任 XFF 头，
    否则使用直连 IP（SEC-03: 防止 XFF 伪造）。
    """
    if request is None:
        return ""
    try:
        from app.core.config import settings

        trusted = settings.trusted_proxies_list
    except Exception:
        trusted = []

    direct_ip = request.client.host if request.client else ""
    fwd = request.headers.get("X-Forwarded-For")

    if fwd and direct_ip in trusted:
        ips = [ip.strip() for ip in fwd.split(",")]
        for ip in reversed(ips):
            if ip not in trusted:
                return ip
        return ips[0]

    return direct_ip


async def write_audit(
    session: Any,
    *,
    actor_id: int | None,
    action: str,
    entity_type: str,
    entity_id: str,
    detail: dict[str, Any],
    ip: str = "",
    trace_id: str = "",
    pii_access: bool = False,
) -> None:
    """写入一条审计记录（仅 add，由调用方负责 commit）。

    ``actor_id`` 可为 None（系统级事件，如登录失败无对应用户，X-4）——
    audit_log.actor_id 已改可空，避免 FK 违规把认证失败变成 500。
    """
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        # 列宽 String(64) 防御：超长拼接（如 维度code:成员code）截断，审计不阻断业务
        entity_id=str(entity_id)[:64],
        detail_json=detail,
        ip=ip,
        trace_id=trace_id,
        pii_access=pii_access,
    )
    session.add(entry)
