"""连接配置加密存储（对齐 TD §12.1「连接配置（加密存储）」/ DEV_GUIDE §13 密钥管理）。

明文连接配置（含账号/密码/连接串）仅在内存中使用，落库为密文；
API 读取一律脱敏（见 collector service 的 DataSourceResponse.connection_config_present），
杜绝凭据明文泄露与日志泄露。

密钥来源：
- 生产：环境变量 UNISENSE_FERNET_KEY（32 字节 base64url 编码，由 Secret Manager 注入）
- 开发：若未设置，从 UNISENSE_JWT_SECRET 派生（仅用于本地调试，每次重启轮换）
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.fernet import Fernet


def _build_key() -> bytes:
    """构建 Fernet 密钥（32 字节 base64url 编码）。

    优先从环境变量 UNISENSE_FERNET_KEY 读取；未设置时从 jwt_secret 派生（开发兼容）。
    """
    raw = os.environ.get("UNISENSE_FERNET_KEY", "").strip()
    if raw:
        # 用户提供的原始值 → SHA-256 → base64url → 32 字节
        return base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    # 开发环境：从 jwt_secret 派生确定性密钥（便于本地调试）
    jwt_secret = os.environ.get("UNISENSE_JWT_SECRET", "default-jwt-secret-for-dev")
    return base64.urlsafe_b64encode(hashlib.sha256(jwt_secret.encode("utf-8")).digest())


_FERNET = Fernet(_build_key())


class SecretManager:
    """对称加密（Fernet）封存连接配置。"""

    @staticmethod
    def encrypt(obj: dict[str, Any]) -> str:
        """加密字典为令牌字符串。"""
        token: str = _FERNET.encrypt(json.dumps(obj, ensure_ascii=False).encode("utf-8")).decode(
            "utf-8"
        )
        return token

    @staticmethod
    def decrypt(token: str) -> dict[str, Any]:
        """解密令牌为字典。"""
        data: dict[str, Any] = json.loads(_FERNET.decrypt(token.encode("utf-8")).decode("utf-8"))
        return data
