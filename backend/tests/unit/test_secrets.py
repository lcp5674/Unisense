"""core/secrets.py 单测（Fernet 连接配置加密）。

覆盖：
- _build_key：显式密钥派生 / 生产未配置拒绝启动
- SecretManager：encrypt/decrypt 往返、中文内容、损坏 token 拒绝
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.fernet import InvalidToken

from app.core.config import ConfigurationError
from app.core.secrets import SecretManager, _build_key


class TestBuildKey:
    def test_explicit_key_uses_pbkdf2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """显式配置 UNISENSE_FERNET_KEY 时经 PBKDF2-HMAC-SHA256 → base64url 派生。

        对齐 SEC-01[P0]：NIST SP 800-132（salt≥16byte，iterations≥600000），
        替代旧版裸 SHA-256。
        """
        monkeypatch.setenv("UNISENSE_FERNET_KEY", "my-custom-fernet-key")
        captured: dict = {}

        def fake_pbkdf2(
            hash_name: str, password: bytes, salt: bytes, iterations: int
        ) -> bytes:
            captured["hash"] = hash_name
            captured["password"] = password
            captured["salt_len"] = len(salt)
            captured["iterations"] = iterations
            return b"x" * 32

        monkeypatch.setattr(hashlib, "pbkdf2_hmac", fake_pbkdf2)
        key = _build_key()
        assert key == base64.urlsafe_b64encode(b"x" * 32)
        assert captured["hash"] == "sha256"
        assert captured["password"] == b"my-custom-fernet-key"
        assert captured["salt_len"] >= 16  # salt≥16byte（NIST SP 800-132）
        assert captured["iterations"] == 600_000

    def test_dev_default_key_is_deterministic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """开发环境默认密钥使用固定盐，结果可复现（便于本地调试）。"""
        monkeypatch.delenv("UNISENSE_FERNET_KEY", raising=False)
        monkeypatch.setenv("UNISENSE_ENV", "local")
        key1 = _build_key()
        key2 = _build_key()
        assert key1 == key2
        assert len(base64.urlsafe_b64decode(key1)) == 32

    def test_prod_without_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """生产环境未配置 FERNET_KEY 时拒绝启动。"""
        monkeypatch.setenv("UNISENSE_ENV", "prod")
        monkeypatch.delenv("UNISENSE_FERNET_KEY", raising=False)
        with pytest.raises(ConfigurationError, match="FERNET_KEY"):
            _build_key()


class TestSecretManagerRoundtrip:
    def test_encrypt_decrypt_roundtrip(self) -> None:
        """加密→解密往返还原原始配置。"""
        cfg = {"host": "127.0.0.1", "port": 3306, "user": "root", "password": "secret"}
        token = SecretManager.encrypt(cfg)
        assert isinstance(token, str)
        assert "secret" not in token  # 密文不含明文
        assert SecretManager.decrypt(token) == cfg

    def test_encrypt_decrypt_chinese_content(self) -> None:
        """中文内容加密往返不丢失。"""
        cfg = {"note": "测试连接", "host": "db.internal"}
        token = SecretManager.encrypt(cfg)
        assert SecretManager.decrypt(token) == cfg

    def test_decrypt_tampered_token_raises(self) -> None:
        """损坏 token 解密抛 InvalidToken。"""
        with pytest.raises(InvalidToken):
            SecretManager.decrypt("not-a-valid-fernet-token")
