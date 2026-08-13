"""QuickBI 嵌入票据签发服务（TD §12.3 / FR-12 / PRD 4.11.1）。

消费方三类之一：BI 报表（QuickBI Report）嵌入消费。本服务为受控的报表
嵌入提供短期票据（ticket），票据由 ``quickbi_sign_key`` 做 HMAC-SHA256
签名，内嵌过期时间（默认 30 分钟），供前端 iframe 拼接到 QuickBI 网关：

    embed_url = quickbi_embed_base_url + ?ticket=<ticket>&reportId=...

签名与票据自洽（服务端可校验，不依赖外部账号体系），避免把报表访问
直接暴露为无鉴权链接；未配置签名密钥时视为依赖未就绪，明确 503 降级
（对齐 TD §11 韧性：可选依赖缺失走 DEPENDENCY_DEGRADED_ENGINE）。

安全约束：
- ticket 内含过期时间戳，服务端拒绝已过期票据；
- 密钥仅存环境变量，不落库、不入日志；
- report_id 白名单可后续在网关侧收敛，本服务仅签发不授权。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from app.core.config import settings
from app.core.exceptions import ExternalDependencyError

logger = logging.getLogger("unisense.semantic.quickbi")

#: 票据默认有效期（秒）：30 分钟，报表嵌入会话足够且避免长时滥用。
_DEFAULT_TTL = 30 * 60
#: 票据体版本标记（未来升级签名算法时按版本兼容校验）。
_TICKET_VERSION = "v1"


class QuickBiService:
    """QuickBI 嵌入票据签发。"""

    def __init__(self) -> None:
        self._sign_key = (settings.quickbi_sign_key or "").strip()
        self._embed_base = (settings.quickbi_embed_base_url or "").strip() or (
            "https://quickbi.aliyun.com/embed"
        )

    @property
    def enabled(self) -> bool:
        """是否已配置签名密钥（未配置则票据无法被网关校验，视为依赖缺失）。"""
        return bool(self._sign_key)

    def issue_ticket(
        self,
        report_id: str,
        dashboard_id: str | None = None,
        params: dict[str, str] | None = None,
        ttl: int = _DEFAULT_TTL,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """签发嵌入票据。

        Args:
            report_id: 报表 ID（必填）。
            dashboard_id: 看板 ID（可选）。
            params: 报表参数（可选，key/value 均为字符串）。
            ttl: 有效期秒数（默认 30 分钟）。
            actor: 签发者身份声明（如 ``{"user_id": 1, "role": "analyst",
                "domain": "sales"}``）。票据为自洽签名体，网关侧应据
                ``actor`` 做用户级收敛（PII 报表/越权报表不得被任意调用者
                凭未绑定身份的票据嵌入）；签名保证声明不可篡改。

        Returns:
            ``{ticket, embed_url, expires_at}``。

        Raises:
            ExternalDependencyError: 未配置 ``quickbi_sign_key``（依赖缺失，
                映射 503 DEPENDENCY_DEGRADED_ENGINE）。
        """
        if not self.enabled:
            raise ExternalDependencyError(
                "QuickBI 嵌入票据未配置 UNISENSE_QUICKBI_SIGN_KEY，"
                "请配置签名密钥后使用（可选依赖降级 503）"
            )

        expires_at = int(time.time()) + ttl
        body: dict[str, Any] = {
            "v": _TICKET_VERSION,
            "report_id": report_id,
            "dashboard_id": dashboard_id,
            "params": params or {},
            "iat": int(time.time()),
            "exp": expires_at,
        }
        if actor:
            # 绑定签发者身份声明：网关侧据此按用户收敛报表访问权限。
            # 仅纳入白名单键，避免调用方塞入任意字段（如覆盖 exp/iat）。
            body["actor"] = {
                k: actor[k]
                for k in ("user_id", "role", "domain")
                if k in actor and actor[k] is not None
            }
        # 票据体：base64url(json)，签名：HMAC-SHA256(票据体)
        payload = base64.urlsafe_b64encode(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).decode("ascii")
        sig = hmac.new(
            self._sign_key.encode("utf-8"),
            payload.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        ticket = f"{payload}.{sig}"

        embed_base = (self._embed_base or "https://quickbi.aliyun.com/embed").rstrip("/")
        embed_url = embed_base
        if not embed_url.endswith("/embed"):
            embed_url += "/embed"
        logger.info(
            "quickbi_ticket_issued report_id=%s dashboard_id=%s ttl=%d",
            report_id,
            dashboard_id or "",
            ttl,
        )
        return {
            "ticket": ticket,
            "embed_url": embed_url,
            "expires_at": expires_at,
        }

    @staticmethod
    def verify_ticket(ticket: str, sign_key: str, max_skew: int = 300) -> dict[str, Any] | None:
        """校验票据（供网关侧/自检使用）。

        Args:
            ticket: 待校验票据。
            sign_key: 签名密钥（与签发一致）。
            max_skew: 允许的时钟偏差（秒）。

        Returns:
            票据体；签名不符、结构非法或已过期返回 ``None``。
        """
        if "." not in ticket:
            return None
        payload, sig = ticket.rsplit(".", 1)
        expected = hmac.new(
            sign_key.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            body = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        except Exception:  # noqa: BLE001 - 非法票据体视为无效
            return None
        if not isinstance(body, dict) or body.get("v") != _TICKET_VERSION:
            return None
        exp = body.get("exp")
        iat = body.get("iat")
        now = int(time.time())
        # 过期即拒；签发时刻过分未来（>max_skew）视为伪造/时钟异常亦拒。
        if not isinstance(exp, int) or exp < now:
            return None
        if isinstance(iat, int) and iat > now + max_skew:
            return None
        return body
