"""连接配置加密存储（对齐 TD §12.1「连接配置（加密存储）」/ DEV_GUIDE §13 密钥管理）。

明文连接配置（含账号/密码/连接串）仅在内存中使用，落库为密文；
API 读取一律脱敏（见 collector service 的 DataSourceResponse.connection_config_present），
杜绝凭据明文泄露与日志泄露。
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet

from app.core.config import settings


def _build_key() -> bytes:
    """从环境变量 UNISENSE_FERNET_KEY 读取 32 字节 Fernet 密钥。

    若环境变量未设置，生成随机密钥并警告（开发环境兼容）。
    生产环境必须通过 Secret Manager 注入。
    """
    from os import environ

    raw = environ.get("UNISENSE_FERNET_KEY", "")
    if raw:
        return base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    # 开发环境：生成随机密钥（每次重启轮换，仅用于本地调试）
    import secrets as _secrets

    return _secrets.token_urlsafe(32).encode("utf-8")[:44]


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
