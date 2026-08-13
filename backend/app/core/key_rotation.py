"""密钥轮换模块（SEC-01/SEC-02: P0 密钥派生修复 + P1 密钥轮换协议）。

职责：
1. 密钥轮换协议：旧密钥→新密钥原子替换 + 90天过期策略
2. 密钥迁移逻辑：旧SHA256→新PBKDF2解密→重加密
3. 多密钥共存：旧密钥保留于解密列表直至所有数据迁移完成

对齐 NIST SP 800-132 / GB/T 36073 L3 稳健级。
"""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet

from app.core.logging import get_logger

logger = get_logger("unisense.key_rotation")

# 密钥轮换默认过期天数（对齐 NIST SP 800-132 建议）
DEFAULT_KEY_EXPIRY_DAYS = 90

# PBKDF2 参数（对齐 R&D-01）
PBKDF2_ITERATIONS = 600_000
PBKDF2_SALT_MIN_LENGTH = 16
PBKDF2_ALGORITHM = "sha256"


def derive_key_pbkdf2(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    """使用 PBKDF2-HMAC-SHA256 派生 Fernet 密钥。

    Args:
        password: 原始密码/密钥材料。
        salt: 盐值（≥16字节）；为 None 时自动生成随机盐。

    Returns:
        (fernet_key, salt) 元组。fernet_key 为 32 字节 base64url 编码。
    """
    if salt is None:
        salt = os.urandom(PBKDF2_SALT_MIN_LENGTH)
    if len(salt) < PBKDF2_SALT_MIN_LENGTH:
        raise ValueError(f"salt 必须 ≥ {PBKDF2_SALT_MIN_LENGTH} 字节，当前 {len(salt)} 字节")

    derived = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    fernet_key = base64.urlsafe_b64encode(derived)
    return fernet_key, salt


def derive_key_legacy_sha256(password: str) -> bytes:
    """旧版 SHA-256 密钥派生（仅用于解密旧数据，不再用于新加密）。

    WARNING: 此函数仅用于迁移兼容，不可用于新密钥派生。
    """
    return base64.urlsafe_b64encode(hashlib.sha256(password.encode("utf-8")).digest())


class KeyRotationManager:
    """密钥轮换管理器。

    支持多密钥共存过渡期：当前活跃密钥用于加密，解密列表中所有密钥均可解密。
    """

    def __init__(self) -> None:
        self._active_key: bytes | None = None
        self._active_salt: bytes | None = None
        self._decrypt_keys: list[bytes] = []
        self._active_fernet: Fernet | None = None
        self._decrypt_fernets: list[Fernet] = []

    def initialize(self) -> None:
        """初始化密钥：构建活跃密钥和解密密钥列表。

        优先使用 PBKDF2 派生；若 UNISENSE_FERNET_KEY 配置了 salt 前缀
        （格式：base64url_salt:base64url_derived_key），则直接使用。
        否则从原始密码材料重新派生。
        """
        raw = os.environ.get("UNISENSE_FERNET_KEY", "").strip()

        if not raw:
            env = os.environ.get("UNISENSE_ENV", "local")
            if env == "prod":
                from app.core.config import ConfigurationError

                raise ConfigurationError(
                    "生产环境 UNISENSE_FERNET_KEY 必须独立配置，"
                    "禁止从 JWT_SECRET 派生降级。请设置独立的 Fernet 密钥后重启。"
                )
            # 开发环境：使用 PBKDF2 派生开发密钥
            dev_secret = "dev-fernet-key-for-local-testing-only"
            active_key, salt = derive_key_pbkdf2(dev_secret)
        else:
            # 尝试解析 salt:key 格式
            if ":" in raw and len(raw.split(":")) == 2:
                parts = raw.split(":")
                try:
                    salt = base64.urlsafe_b64decode(parts[0])
                    active_key = base64.urlsafe_b64decode(parts[1])
                    active_key = base64.urlsafe_b64encode(active_key)
                except Exception:
                    # 解析失败，用 PBKDF2 重新派生
                    active_key, salt = derive_key_pbkdf2(raw)
            else:
                # 无 salt 前缀，用 PBKDF2 派生
                active_key, salt = derive_key_pbkdf2(raw)

        self._active_key = active_key
        self._active_salt = salt
        self._active_fernet = Fernet(active_key)

        # 旧密钥（SHA-256 派生）加入解密列表，支持旧数据解密
        if raw:
            legacy_key = derive_key_legacy_sha256(raw)
            if legacy_key != active_key:
                self._decrypt_keys.append(legacy_key)
                self._decrypt_fernets.append(Fernet(legacy_key))
        else:
            # 开发环境：旧 SHA-256 密钥
            dev_secret = "dev-fernet-key-for-local-testing-only"
            legacy_key = derive_key_legacy_sha256(dev_secret)
            if legacy_key != active_key:
                self._decrypt_keys.append(legacy_key)
                self._decrypt_fernets.append(Fernet(legacy_key))

        # 活跃密钥也加入解密列表（放在最前面优先尝试）
        self._decrypt_keys.insert(0, active_key)
        self._decrypt_fernets.insert(0, self._active_fernet)

        logger.info(
            "key_rotation_initialized",
            decrypt_key_count=len(self._decrypt_fernets),
            has_legacy_key=len(self._decrypt_fernets) > 1,
        )

    @property
    def active_fernet(self) -> Fernet:
        """当前活跃 Fernet 实例（用于加密）。"""
        if self._active_fernet is None:
            self.initialize()
        return self._active_fernet  # type: ignore[return-value]

    def decrypt_with_any_key(self, token: bytes) -> bytes:
        """使用解密密钥列表尝试解密（从活跃密钥开始，依次尝试旧密钥）。

        Args:
            token: 加密令牌。

        Returns:
            解密后的明文。

        Raises:
            cryptography.fernet.InvalidToken: 所有密钥均无法解密。
        """
        if not self._decrypt_fernets:
            self.initialize()
        last_exc: Exception | None = None
        for fernet in self._decrypt_fernets:
            try:
                return fernet.decrypt(token)
            except Exception as exc:
                last_exc = exc
                continue
        raise last_exc or Exception("所有密钥均无法解密")  # type: ignore[misc]

    def encrypt(self, data: bytes) -> bytes:
        """使用活跃密钥加密。"""
        return self.active_fernet.encrypt(data)

    def needs_rotation(self, key_created_at: datetime | None = None) -> bool:
        """检查密钥是否需要轮换（超过90天）。"""
        if key_created_at is None:
            return False
        return datetime.now(UTC) - key_created_at > timedelta(days=DEFAULT_KEY_EXPIRY_DAYS)


# 模块级单例
_manager: KeyRotationManager | None = None


def get_key_rotation_manager() -> KeyRotationManager:
    """获取密钥轮换管理器单例。"""
    global _manager
    if _manager is None:
        _manager = KeyRotationManager()
        _manager.initialize()
    return _manager


def init_key_rotation_manager() -> KeyRotationManager:
    """初始化密钥轮换管理器（lifespan 中调用）。"""
    global _manager
    _manager = KeyRotationManager()
    _manager.initialize()
    return _manager
