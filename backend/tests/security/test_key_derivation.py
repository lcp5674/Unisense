"""SEC-01 密钥派生安全回归测试。"""

import base64
import hashlib

from app.core.key_rotation import derive_key_legacy_sha256, derive_key_pbkdf2


def test_pbkdf2_uses_sha256():
    key, salt = derive_key_pbkdf2("test-password")
    expected = hashlib.pbkdf2_hmac("sha256", b"test-password", salt, 600_000)
    assert key == base64.urlsafe_b64encode(expected)


def test_pbkdf2_salt_at_least_16_bytes():
    _, salt = derive_key_pbkdf2("test")
    assert len(salt) >= 16


def test_pbkdf2_deterministic_with_known_salt():
    salt = b"fixed-salt-16byte"  # 16 字节，满足 PBKDF2_SALT_MIN_LENGTH=16
    key1, _ = derive_key_pbkdf2("password", salt=salt)
    key2, _ = derive_key_pbkdf2("password", salt=salt)
    assert key1 == key2


def test_legacy_sha256_not_used_for_new_keys():
    key, _ = derive_key_pbkdf2("same-password")
    legacy = derive_key_legacy_sha256("same-password")
    assert key != legacy


def test_pbkdf2_different_passwords_different_keys():
    key1, _ = derive_key_pbkdf2("password1")
    key2, _ = derive_key_pbkdf2("password2")
    assert key1 != key2
