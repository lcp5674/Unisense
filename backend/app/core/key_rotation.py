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
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet

from app.core.logging import get_logger

logger = get_logger("unisense.key_rotation")

# 密钥轮换默认过期天数（对齐 NIST SP 800-132 建议）
DEFAULT_KEY_EXPIRY_DAYS = 90

# PBKDF2 参数（对齐 R&D-01）
PBKDF2_ITERATIONS = 600_000
PBKDF2_SALT_MIN_LENGTH = 16
PBKDF2_ALGORITHM = "sha256"

# 确定性盐（与 secrets.py `_build_key` 保持一致）。
# SEC-01 修复：绝不可用随机盐——`derive_key_pbkdf2` 默认 salt=None 时会 os.urandom，
# 导致每个进程/每次重启派生不同的活跃密钥，先前用 Fernet 加密落库的连接配置将
# 全部无法解密（数据回归）。固定盐保证跨进程、跨重启派生一致。
_SALT_RAW = b"unisense-fernet-salt"[:16]
_SALT_DEV = b"dev-salt-16byte!"

#: 密钥链持久化文件名（默认 backend/data/fernet_keychain.json，gitignore 不入库）。
#: rotate_key 把完整密钥链（活跃 + 全部解密密钥）落盘，重启后从文件恢复——
#: 修复「rotate 仅写进程 os.environ、重启丢密钥链导致未迁移数据不可解密」。
_KEYCHAIN_FILENAME = "fernet_keychain.json"


def _keychain_path() -> str:
    """密钥链文件路径（可经 UNISENSE_KEYCHAIN_PATH 覆盖，默认 backend/data/）。"""
    env_path = os.environ.get("UNISENSE_KEYCHAIN_PATH", "").strip()
    if env_path:
        return env_path
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "data", _KEYCHAIN_FILENAME)


def _load_keychain() -> dict[str, Any] | None:
    """读取持久化密钥链（文件不存在/损坏返回 None）。"""
    try:
        with open(_keychain_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_keychain(state: dict[str, Any]) -> None:
    """原子写密钥链（tmp + replace + 0600 权限，防半写/他读）。"""
    path = _keychain_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        logger.warning("keychain_persist_failed", path=path)


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
        self._key_version: int = 1
        self._key_created_at: datetime | None = None

    def initialize(self) -> None:
        """初始化密钥：优先从持久化密钥链恢复，否则构建活跃密钥与解密密钥列表。

        密钥链（backend/data/fernet_keychain.json）存在时直接恢复完整密钥链
        （活跃 + 全部历史解密密钥），保证 rotate 后重启不解密失败；
        否则按原逻辑从环境/派生构建并落盘（首次初始化即持久化）。

        优先使用 PBKDF2 派生；若 UNISENSE_FERNET_KEY 配置了 salt 前缀
        （格式：base64url_salt:base64url_derived_key），则直接使用。
        否则从原始密码材料重新派生。
        """
        if self._load_from_keychain():
            logger.info(
                "key_rotation_loaded_from_keychain",
                version=self._key_version,
                decrypt_key_count=len(self._decrypt_fernets),
            )
            return
        raw = os.environ.get("UNISENSE_FERNET_KEY", "").strip()

        if not raw:
            env = os.environ.get("UNISENSE_ENV", "local")
            if env == "prod":
                from app.core.config import ConfigurationError

                raise ConfigurationError(
                    "生产环境 UNISENSE_FERNET_KEY 必须独立配置，"
                    "禁止从 JWT_SECRET 派生降级。请设置独立的 Fernet 密钥后重启。"
                )
            # 开发环境：使用 PBKDF2 派生开发密钥（固定盐，保证跨进程一致）
            dev_secret = "dev-fernet-key-for-local-testing-only"
            active_key, salt = derive_key_pbkdf2(dev_secret, _SALT_DEV)
        else:
            # 尝试解析 salt:key 格式
            if ":" in raw and len(raw.split(":")) == 2:
                parts = raw.split(":")
                try:
                    salt = base64.urlsafe_b64decode(parts[0])
                    active_key = base64.urlsafe_b64decode(parts[1])
                    active_key = base64.urlsafe_b64encode(active_key)
                except Exception:
                    # 解析失败，用 PBKDF2 重新派生（固定盐）
                    active_key, salt = derive_key_pbkdf2(raw, _SALT_RAW)
            else:
                # 无 salt 前缀，用 PBKDF2 派生（固定盐）
                active_key, salt = derive_key_pbkdf2(raw, _SALT_RAW)

        self._active_key = active_key
        self._active_salt = salt
        self._active_fernet = Fernet(active_key)
        self._key_created_at = datetime.now(UTC)

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
        # 首次初始化不落盘：env/固定盐派生可跨重启重现活跃密钥，rotate 时才需持久化
        # 密钥链（避免测试/收集阶段生成 keychain 文件污染环境）。

    def _load_from_keychain(self) -> bool:
        """从持久化密钥链恢复（活跃 + 全部解密密钥）。失败返回 False 走原初始化。"""
        data = _load_keychain()
        if not data or not data.get("active_key_b64"):
            return False
        try:
            active_key = str(data["active_key_b64"]).encode("utf-8")
            self._active_key = active_key
            salt_b64 = data.get("active_salt_b64")
            self._active_salt = (
                base64.urlsafe_b64decode(str(salt_b64)) if salt_b64 else None
            )
            self._active_fernet = Fernet(active_key)
            decrypt_b64 = data.get("decrypt_keys_b64") or []
            self._decrypt_keys = [str(k).encode("utf-8") for k in decrypt_b64]
            self._decrypt_fernets = [Fernet(str(k).encode("utf-8")) for k in decrypt_b64]
            self._key_version = int(data.get("version", 1))
            created = data.get("created_at")
            self._key_created_at = (
                datetime.fromisoformat(str(created)) if created else None
            )
            return True
        except Exception:
            logger.warning("keychain_load_failed", exc_info=True)
            return False

    def _persist(self) -> None:
        """把当前密钥链（活跃 + 全部解密密钥）原子写入持久化文件。"""
        state: dict[str, Any] = {
            "version": self._key_version,
            "created_at": (
                self._key_created_at.isoformat() if self._key_created_at else None
            ),
            "active_salt_b64": (
                base64.urlsafe_b64encode(self._active_salt).decode("utf-8")
                if self._active_salt
                else None
            ),
            "active_key_b64": self._active_key.decode("utf-8") if self._active_key else None,
            "decrypt_keys_b64": [k.decode("utf-8") for k in self._decrypt_keys],
        }
        _save_keychain(state)

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
        raise last_exc or Exception("所有密钥均无法解密")

    def encrypt(self, data: bytes) -> bytes:
        """使用活跃密钥加密。"""
        return self.active_fernet.encrypt(data)

    def needs_rotation(self, key_created_at: datetime | None = None) -> bool:
        """检查密钥是否需要轮换（超过90天）。"""
        if key_created_at is None:
            return False
        return datetime.now(UTC) - key_created_at > timedelta(days=DEFAULT_KEY_EXPIRY_DAYS)

    def migrate_secrets(
        self,
        list_func: Callable[[], list[str]],
        decrypt_func: Callable[[bytes], bytes],
        encrypt_func: Callable[[bytes], bytes],
    ) -> int:
        """迁移旧 SHA-256 加密的数据至当前 PBKDF2 活跃密钥。

        Args:
            list_func: 无参回调，返回待迁移加密条目列表（list[str]）。
            decrypt_func: 接收密文 token 返回明文 bytes 的回调。
            encrypt_func: 接收明文 bytes 返回密文 bytes 的回调。

        Returns:
            成功迁移的条目数。
        """
        items = list_func()
        migrated = 0
        for token in items:
            try:
                raw = token if isinstance(token, bytes) else token.encode("utf-8")
                plaintext = decrypt_func(raw)
                encrypt_func(plaintext)
                migrated += 1
            except Exception:
                logger.warning("migrate_secret_failed", exc_info=True)
                continue
        logger.info("migrate_secrets_completed", total=len(items), migrated=migrated)
        return migrated

    def rotate_key(self, new_raw_key: str) -> None:
        """轮换密钥：旧活跃密钥移入解密列表，新密钥成为活跃密钥。

        Args:
            new_raw_key: 新的原始密钥材料。

        Raises:
            ValueError: new_raw_key 为空。
        """
        if not new_raw_key:
            raise ValueError("new_raw_key 不能为空")

        # 确定性盐派生（与 initialize 的 env 路径一致，杜绝随机盐漂移）；
        # 密钥链经 _persist 落盘，重启后从文件恢复完整解密链（未迁移数据仍可解）。
        new_key, new_salt = derive_key_pbkdf2(new_raw_key, _SALT_RAW)
        new_fernet = Fernet(new_key)

        if self._active_fernet is not None and self._active_key is not None:
            self._decrypt_keys.append(self._active_key)
            self._decrypt_fernets.append(self._active_fernet)

        self._active_key = new_key
        self._active_salt = new_salt
        self._active_fernet = new_fernet

        self._decrypt_keys.insert(0, new_key)
        self._decrypt_fernets.insert(0, new_fernet)

        self._key_version += 1
        self._key_created_at = datetime.now(UTC)

        encoded_salt = base64.urlsafe_b64encode(new_salt).decode("utf-8")
        encoded_key = new_key.decode("utf-8")
        os.environ["UNISENSE_FERNET_KEY"] = f"{encoded_salt}:{encoded_key}"
        # 持久化密钥链：重启后恢复 active + 全部 decrypt 密钥（修复 rotate 仅写进程
        # 环境变量、重启丢密钥链导致未迁移数据不可解密）
        self._persist()

        logger.info(
            "key_rotated",
            version=self._key_version,
            decrypt_key_count=len(self._decrypt_fernets),
        )

    def is_key_expired(self) -> bool:
        """检查当前活跃密钥是否超过90天有效期。"""
        if self._key_created_at is None:
            return False
        return datetime.now(UTC) - self._key_created_at > timedelta(days=DEFAULT_KEY_EXPIRY_DAYS)

    def get_key_metadata(self) -> dict[str, object]:
        """获取当前密钥元数据。"""
        return {
            "version": self._key_version,
            "created_at": self._key_created_at.isoformat() if self._key_created_at else None,
            "is_expired": self.is_key_expired(),
            "decrypt_key_count": len(self._decrypt_fernets),
        }


def migrate_legacy_secrets(
    encrypt_func: Callable[[bytes], bytes],
    decrypt_func: Callable[[bytes], bytes],
    list_func: Callable[[], list[str]],
) -> int:
    """独立密钥迁移函数，供管理员端点调用。

    遍历所有旧 SHA-256 加密数据，解密后用当前 PBKDF2 活跃密钥重加密。

    Args:
        encrypt_func: 加密回调（plaintext bytes → ciphertext bytes）。
        decrypt_func: 解密回调（ciphertext bytes → plaintext bytes），支持多密钥。
        list_func: 列出待迁移条目的回调（→ list[str]）。

    Returns:
        成功迁移条目数。
    """
    manager = get_key_rotation_manager()
    return manager.migrate_secrets(
        list_func=list_func,
        decrypt_func=decrypt_func,
        encrypt_func=encrypt_func,
    )


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
