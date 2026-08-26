"""密钥轮换单测（SEC-01/SEC-02: PBKDF2 密钥派生 + 轮换协议）。

覆盖：
- derive_key_pbkdf2：盐自动生成/固定盐/盐长校验/确定性
- derive_key_legacy_sha256（仅迁移兼容）
- KeyRotationManager：显式密钥/prod 拒绝/开发默认/多密钥解密
- encrypt/decrypt 往返、decrypt_with_any_key 旧密钥兜底
- needs_rotation 90 天策略
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import ConfigurationError
from app.core.key_rotation import (
    DEFAULT_KEY_EXPIRY_DAYS,
    KeyRotationManager,
    derive_key_legacy_sha256,
    derive_key_pbkdf2,
)


class TestDeriveKey:
    def test_pbkdf2_with_random_salt(self) -> None:
        key1, salt1 = derive_key_pbkdf2("secret")
        key2, salt2 = derive_key_pbkdf2("secret")
        # 随机盐 → 密钥不同（但都是合法 Fernet key）
        assert salt1 != salt2
        assert key1 != key2
        assert len(key1) > 0

    def test_pbkdf2_with_fixed_salt_is_deterministic(self) -> None:
        salt = b"fixed-salt-16byte"
        key1, _ = derive_key_pbkdf2("secret", salt)
        key2, _ = derive_key_pbkdf2("secret", salt)
        assert key1 == key2

    def test_pbkdf2_salt_too_short_raises(self) -> None:
        with pytest.raises(ValueError):
            derive_key_pbkdf2("secret", b"short")

    def test_legacy_sha256_deterministic(self) -> None:
        a = derive_key_legacy_sha256("secret")
        b = derive_key_legacy_sha256("secret")
        assert a == b
        assert len(a) > 0


class TestManagerInitialize:
    def test_init_with_explicit_key(self, monkeypatch) -> None:
        monkeypatch.setenv("UNISENSE_FERNET_KEY", "explicit-key-material")
        monkeypatch.setenv("UNISENSE_ENV", "local")
        mgr = KeyRotationManager()
        mgr.initialize()
        assert mgr.active_fernet is not None
        # 活跃密钥在解密列表首位
        assert len(mgr._decrypt_fernets) >= 1

    def test_init_prod_without_key_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("UNISENSE_FERNET_KEY", raising=False)
        monkeypatch.setenv("UNISENSE_ENV", "prod")
        mgr = KeyRotationManager()
        with pytest.raises(ConfigurationError):
            mgr.initialize()

    def test_init_dev_default_key(self, monkeypatch) -> None:
        monkeypatch.delenv("UNISENSE_FERNET_KEY", raising=False)
        monkeypatch.setenv("UNISENSE_ENV", "local")
        mgr = KeyRotationManager()
        mgr.initialize()
        assert mgr.active_fernet is not None
        assert mgr._active_key is not None

    def test_init_key_deterministic_across_instances(self, monkeypatch) -> None:
        """SEC-01 回归：显式密钥路径两次 initialize 派生相同活跃密钥。

        随机盐曾导致每个进程/每次重启派生不同密钥 → 跨进程解密失败
        （先前加密落库的连接配置全部无法解密，数据回归）。
        """
        monkeypatch.setenv("UNISENSE_FERNET_KEY", "explicit-key-material")
        monkeypatch.setenv("UNISENSE_ENV", "local")
        m1 = KeyRotationManager()
        m1.initialize()
        m2 = KeyRotationManager()
        m2.initialize()
        assert m1._active_key == m2._active_key
        # 跨实例互解密也应成功（同一密钥）
        token = m1.active_fernet.encrypt(b"payload")
        assert m2.decrypt_with_any_key(token) == b"payload"

    def test_init_dev_key_deterministic_across_instances(self, monkeypatch) -> None:
        """SEC-01 回归：开发默认密钥路径跨实例派生一致。"""
        monkeypatch.delenv("UNISENSE_FERNET_KEY", raising=False)
        monkeypatch.setenv("UNISENSE_ENV", "local")
        m1 = KeyRotationManager()
        m1.initialize()
        m2 = KeyRotationManager()
        m2.initialize()
        assert m1._active_key == m2._active_key
        token = m1.active_fernet.encrypt(b"dev-payload")
        assert m2.decrypt_with_any_key(token) == b"dev-payload"

    def test_init_salt_prefix_format(self, monkeypatch) -> None:
        import base64

        salt = base64.urlsafe_b64encode(b"1234567890123456")
        key = base64.urlsafe_b64encode(b"k" * 32)
        monkeypatch.setenv("UNISENSE_FERNET_KEY", f"{salt.decode()}:{key.decode()}")
        monkeypatch.setenv("UNISENSE_ENV", "local")
        mgr = KeyRotationManager()
        mgr.initialize()
        assert mgr._active_key is not None
        assert mgr._active_salt is not None

    def test_init_salt_prefix_decode_failure_falls_back(self, monkeypatch) -> None:
        """salt:key 格式但 base64 解码失败 → PBKDF2 回退（不崩）。"""
        monkeypatch.setenv("UNISENSE_FERNET_KEY", "!!!bad!!!:!!!bad!!!")
        monkeypatch.setenv("UNISENSE_ENV", "local")
        mgr = KeyRotationManager()
        mgr.initialize()
        assert mgr._active_key is not None
        assert mgr.active_fernet is not None

    def test_lazy_init_via_property(self, monkeypatch) -> None:
        monkeypatch.setenv("UNISENSE_FERNET_KEY", "some-key")
        monkeypatch.setenv("UNISENSE_ENV", "local")
        mgr = KeyRotationManager()
        # active_fernet 触发惰性 initialize
        fernet = mgr.active_fernet
        assert fernet is not None


class TestEncryptDecrypt:
    def test_roundtrip(self, monkeypatch) -> None:
        monkeypatch.setenv("UNISENSE_FERNET_KEY", "roundtrip-key")
        monkeypatch.setenv("UNISENSE_ENV", "local")
        mgr = KeyRotationManager()
        mgr.initialize()
        token = mgr.encrypt(b"hello")
        assert mgr.decrypt_with_any_key(token) == b"hello"

    def test_decrypt_with_legacy_key_fallback(self, monkeypatch) -> None:
        # 用 SHA256 旧密钥加密，新 PBKDF2 管理器应能解密（过渡期兼容）
        from cryptography.fernet import Fernet

        legacy = Fernet(derive_key_legacy_sha256("legacy-material"))
        token = legacy.encrypt(b"old-data")

        monkeypatch.setenv("UNISENSE_FERNET_KEY", "legacy-material")
        monkeypatch.setenv("UNISENSE_ENV", "local")
        mgr = KeyRotationManager()
        mgr.initialize()
        assert mgr.decrypt_with_any_key(token) == b"old-data"

    def test_decrypt_invalid_token_raises(self, monkeypatch) -> None:
        from cryptography.fernet import InvalidToken

        monkeypatch.setenv("UNISENSE_FERNET_KEY", "key-a")
        monkeypatch.setenv("UNISENSE_ENV", "local")
        mgr = KeyRotationManager()
        mgr.initialize()
        with pytest.raises(InvalidToken):
            mgr.decrypt_with_any_key(b"garbage-token")

    def test_decrypt_lazy_initializes(self, monkeypatch) -> None:
        """未显式 initialize 时 decrypt_with_any_key 触发惰性初始化。"""
        from cryptography.fernet import InvalidToken

        monkeypatch.setenv("UNISENSE_FERNET_KEY", "lazy-key")
        monkeypatch.setenv("UNISENSE_ENV", "local")
        mgr = KeyRotationManager()  # 不调用 initialize
        with pytest.raises(InvalidToken):
            mgr.decrypt_with_any_key(b"garbage-token")  # 触发惰性 initialize
        assert mgr._active_fernet is not None


class TestRotationPolicy:
    def test_needs_rotation_after_90_days(self) -> None:
        mgr = KeyRotationManager()
        old = datetime.now(UTC) - timedelta(days=DEFAULT_KEY_EXPIRY_DAYS + 1)
        assert mgr.needs_rotation(old) is True

    def test_no_rotation_within_window(self) -> None:
        mgr = KeyRotationManager()
        recent = datetime.now(UTC) - timedelta(days=30)
        assert mgr.needs_rotation(recent) is False

    def test_no_rotation_when_created_at_none(self) -> None:
        mgr = KeyRotationManager()
        assert mgr.needs_rotation(None) is False

    def test_default_expiry_days(self) -> None:
        assert DEFAULT_KEY_EXPIRY_DAYS == 90


class TestMigrateSecrets:
    def test_migrate_all_success(self, monkeypatch) -> None:
        monkeypatch.setenv("UNISENSE_FERNET_KEY", "migrate-key")
        monkeypatch.setenv("UNISENSE_ENV", "local")
        mgr = KeyRotationManager()
        mgr.initialize()
        tokens = ["tok1", "tok2", "tok3"]
        n = mgr.migrate_secrets(
            list_func=lambda: tokens,
            decrypt_func=lambda t: b"plain",
            encrypt_func=lambda p: b"cipher",
        )
        assert n == 3

    def test_migrate_skips_failures(self, monkeypatch) -> None:
        monkeypatch.setenv("UNISENSE_FERNET_KEY", "migrate-key")
        monkeypatch.setenv("UNISENSE_ENV", "local")
        mgr = KeyRotationManager()
        mgr.initialize()

        def _decrypt(t: bytes) -> bytes:
            if t == b"tok_bad":
                raise ValueError("bad token")
            return b"plain"

        n = mgr.migrate_secrets(
            list_func=lambda: ["tok_ok", "tok_bad"],
            decrypt_func=_decrypt,
            encrypt_func=lambda p: b"cipher",
        )
        # 失败条目跳过，成功 1 条
        assert n == 1

    def test_migrate_legacy_secrets_module(self, monkeypatch) -> None:
        from app.core import key_rotation as kr_module

        old = kr_module._manager
        monkeypatch.setattr(kr_module, "_manager", None)
        try:
            from app.core.key_rotation import migrate_legacy_secrets

            n = migrate_legacy_secrets(
                encrypt_func=lambda p: b"c",
                decrypt_func=lambda t: b"p",
                list_func=lambda: ["a", "b"],
            )
            assert n == 2
        finally:
            monkeypatch.setattr(kr_module, "_manager", old)


class TestRotate:
    def test_rotate_key_rotates_active(self, monkeypatch) -> None:
        from cryptography.fernet import Fernet

        monkeypatch.setenv("UNISENSE_FERNET_KEY", "old-key")
        monkeypatch.setenv("UNISENSE_ENV", "local")
        mgr = KeyRotationManager()
        mgr.initialize()
        old_key = mgr._active_key
        mgr.rotate_key("new-key-material")
        assert mgr._active_key != old_key
        assert mgr._key_version == 2
        # 旧密钥保留在解密列表（过渡期兼容）
        assert old_key in mgr._decrypt_keys
        # 用旧活跃密钥加密的数据，轮换后仍可解密（多密钥过渡期）
        old_fernet = Fernet(old_key)
        old_token = old_fernet.encrypt(b"legacy")
        assert mgr.decrypt_with_any_key(old_token) == b"legacy"

    def test_rotate_empty_key_raises(self, monkeypatch) -> None:
        monkeypatch.setenv("UNISENSE_FERNET_KEY", "old-key")
        monkeypatch.setenv("UNISENSE_ENV", "local")
        mgr = KeyRotationManager()
        mgr.initialize()
        with pytest.raises(ValueError):
            mgr.rotate_key("")

    def test_rotate_updates_env(self, monkeypatch) -> None:
        monkeypatch.setenv("UNISENSE_FERNET_KEY", "old-key")
        monkeypatch.setenv("UNISENSE_ENV", "local")
        mgr = KeyRotationManager()
        mgr.initialize()
        mgr.rotate_key("new-key-material")
        # 环境变量更新为 salt:key 格式
        new_env = __import__("os").environ.get("UNISENSE_FERNET_KEY", "")
        assert ":" in new_env


class TestKeyExpiry:
    def test_is_key_expired_when_created_at_none(self) -> None:
        mgr = KeyRotationManager()
        assert mgr.is_key_expired() is False

    def test_is_key_expired_true_after_90_days(self) -> None:
        from datetime import timedelta

        mgr = KeyRotationManager()
        mgr._key_created_at = datetime.now(UTC) - timedelta(days=DEFAULT_KEY_EXPIRY_DAYS + 1)
        assert mgr.is_key_expired() is True

    def test_get_key_metadata(self) -> None:
        mgr = KeyRotationManager()
        meta = mgr.get_key_metadata()
        assert "version" in meta
        assert "created_at" in meta
        assert "is_expired" in meta
        assert "decrypt_key_count" in meta


class TestInit:
    def test_init_manager(self, monkeypatch) -> None:
        from app.core import key_rotation as kr_module

        old = kr_module._manager
        monkeypatch.setattr(kr_module, "_manager", None)
        try:
            inst = kr_module.init_key_rotation_manager()
            assert kr_module.get_key_rotation_manager() is inst
        finally:
            monkeypatch.setattr(kr_module, "_manager", old)


class TestKeychainPersistence:
    """密钥链持久化：rotate 后落盘，新 manager（模拟重启）恢复完整密钥链。"""

    def test_rotate_persists_and_new_manager_restores(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("UNISENSE_KEYCHAIN_PATH", str(tmp_path / "keychain.json"))
        m1 = KeyRotationManager()
        m1.initialize()
        # 轮换前：env 派生密钥可加密/解密
        tok1 = m1.encrypt(b"before")
        assert m1.decrypt_with_any_key(tok1) == b"before"

        m1.rotate_key("new-secret-material-16chars")
        tok2 = m1.encrypt(b"after")
        assert m1.decrypt_with_any_key(tok2) == b"after"

        # 新 manager（模拟重启）从 keychain 恢复完整密钥链：
        # 活跃密钥 = rotate 后新密钥（能解 after），旧密钥仍在解密链（能解 before）
        m2 = KeyRotationManager()
        m2.initialize()
        assert m2._key_version == 2
        assert len(m2._decrypt_fernets) == len(m1._decrypt_fernets)
        assert m2.decrypt_with_any_key(tok2) == b"after"
        assert m2.decrypt_with_any_key(tok1) == b"before"
        # 恢复后新活跃密钥加密 → m2 可解
        tok3 = m2.encrypt(b"restored")
        assert m2.decrypt_with_any_key(tok3) == b"restored"
