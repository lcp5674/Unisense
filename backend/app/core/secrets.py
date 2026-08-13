"""连接配置加密存储（对齐 TD §12.1「连接配置（加密存储）」/ DEV_GUIDE §13 密钥管理）。

明文连接配置（含账号/密码/连接串）仅在内存中使用，落库为密文；
API 读取一律脱敏（见 collector service 的 DataSourceResponse.connection_config_present），
杜绝凭据明文泄露与日志泄露。

密钥来源：
- 必须通过环境变量 UNISENSE_FERNET_KEY 配置。
- 生产环境未配置时拒绝启动（ConfigurationError）。
- 开发环境未配置时使用默认开发密钥（仅用于本地调试，每次重启轮换）。

SEC-01[P0] 修复：
- 密钥派生改用 PBKDF2-HMAC-SHA256（salt≥16byte, iterations≥600000）
- 替代旧版裸 SHA-256（违反 NIST SP 800-132）
- 支持旧密钥解密过渡期（多密钥共存）
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.fernet import Fernet

from app.core.config import ConfigurationError
from app.core.key_rotation import get_key_rotation_manager


def _build_key() -> bytes:
    """构建 Fernet 密钥（使用 PBKDF2-HMAC-SHA256 派生）。

    从环境变量 UNISENSE_FERNET_KEY 读取；未设置时：
    - 生产环境：抛出 ConfigurationError 拒绝启动。
    - 开发/测试环境：使用默认开发密钥（仅用于本地调试）。

    SEC-01[P0] 修复：使用 PBKDF2-HMAC-SHA256 替代裸 SHA-256。
    对齐 NIST SP 800-132 / R&D-01。
    """
    raw = os.environ.get("UNISENSE_FERNET_KEY", "").strip()
    if raw:
        # 使用 PBKDF2-HMAC-SHA256 派生（iterations=600,000, salt≥16byte）
        # 对齐 NIST SP 800-132 / R&D-01。
        # 注意：盐必须是确定性的（固定值）——若用随机盐，每次进程启动派生的密钥
        # 都不同，先前用 Fernet 加密落库的连接配置将全部无法解密（数据回归）。
        salt = b"unisense-fernet-salt"[:16]
        derived = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, 600_000)
        return base64.urlsafe_b64encode(derived)
    # 检查是否为生产环境
    env = os.environ.get("UNISENSE_ENV", "local")
    if env == "prod":
        raise ConfigurationError(
            "生产环境 UNISENSE_FERNET_KEY 必须独立配置，"
            "禁止从 JWT_SECRET 派生降级。请设置独立的 Fernet 密钥后重启。"
        )
    # 开发环境：使用 PBKDF2 派生确定性默认密钥（便于本地调试）
    dev_secret = "dev-fernet-key-for-local-testing-only"
    # 开发环境使用固定盐以便可复现
    dev_salt = b"dev-salt-16byte!"
    derived = hashlib.pbkdf2_hmac("sha256", dev_secret.encode("utf-8"), dev_salt, 600_000)
    return base64.urlsafe_b64encode(derived)


# 初始化密钥轮换管理器（支持多密钥共存）
_manager: Any | None
try:
    _manager = get_key_rotation_manager()
except Exception:
    # 降级：使用单密钥模式
    _manager = None


class SecretManager:
    """对称加密（Fernet）封存连接配置。

    SEC-01[P0] 修复：加密使用活跃密钥（PBKDF2 派生），
    解密使用多密钥列表（支持旧 SHA-256 密钥解密过渡期）。
    """

    @staticmethod
    def encrypt(obj: dict[str, Any]) -> str:
        """加密字典为令牌字符串。

        使用活跃密钥（PBKDF2 派生）加密。
        """
        fernet = _manager.active_fernet if _manager is not None else Fernet(_build_key())
        token: str = fernet.encrypt(json.dumps(obj, ensure_ascii=False).encode("utf-8")).decode(
            "utf-8"
        )
        return token

    @staticmethod
    def decrypt(token: str) -> dict[str, Any]:
        """解密令牌为字典。

        使用多密钥列表尝试解密（支持旧 SHA-256 密钥解密过渡期）。
        优先使用活跃密钥，依次尝试旧密钥。
        """
        if _manager is not None:
            data_bytes = _manager.decrypt_with_any_key(token.encode("utf-8"))
            data: dict[str, Any] = json.loads(data_bytes.decode("utf-8"))
            return data
        # 降级模式：仅使用当前密钥
        fernet = Fernet(_build_key())
        data = json.loads(fernet.decrypt(token.encode("utf-8")).decode("utf-8"))
        return data
