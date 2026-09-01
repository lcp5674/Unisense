"""TOTP（RFC 6238）核心逻辑测试。

覆盖：官方测试向量（SHA1 6 位截断）、时间窗容忍、非法输入 fail-safe、
密钥加解密往返、otpauth URI 结构。
"""

from __future__ import annotations

import time

from app.core.totp import (
    _base32_decode,
    _hotp,
    decrypt_secret,
    encrypt_secret,
    generate_totp_secret,
    totp_uri,
    verify_totp,
)

# RFC 6238 附录 B 测试密钥（ASCII "12345678901234567890" 的 base32）。
_RFC_SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_hotp_matches_rfc_6238_six_digit() -> None:
    """RFC 6238 官方向量：T=59→94287082、T=1111111109→07081804（8 位），
    6 位截断应取其末 6 位：287082 / 081804。"""
    sb = _base32_decode(_RFC_SECRET_B32)
    assert _hotp(sb, 1) == "287082"
    assert _hotp(sb, 37037036) == "081804"


def test_verify_totp_accepts_current_code() -> None:
    secret = generate_totp_secret()
    sb = _base32_decode(secret)
    code = _hotp(sb, int(time.time() // 30))
    assert verify_totp(secret, code) is True


def test_verify_totp_tolerates_clock_drift() -> None:
    """window=1 允许前后各一个周期（时钟漂移容忍）。"""
    secret = generate_totp_secret()
    sb = _base32_decode(secret)
    counter = int(time.time() // 30)
    assert verify_totp(secret, _hotp(sb, counter - 1)) is True
    assert verify_totp(secret, _hotp(sb, counter + 1)) is True


def test_verify_totp_rejects_wrong_code() -> None:
    secret = generate_totp_secret()
    assert verify_totp(secret, "000000") is False


def test_verify_totp_rejects_invalid_input_fail_safe() -> None:
    """非法输入（空/非数字/长度不符/密钥非法）恒 False，不抛异常。"""
    assert verify_totp("", "123456") is False
    assert verify_totp(generate_totp_secret(), "") is False
    assert verify_totp(generate_totp_secret(), "12ab56") is False
    assert verify_totp(generate_totp_secret(), "12345") is False
    assert verify_totp("!!!not-base32!!!", "123456") is False


def test_generate_secret_is_base32_32_chars() -> None:
    secret = generate_totp_secret()
    assert len(secret) == 32
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in secret)


def test_totp_uri_structure() -> None:
    secret = generate_totp_secret()
    uri = totp_uri(secret, "admin")
    assert uri.startswith("otpauth://totp/WeSemantics%3Aadmin?secret=")
    assert f"secret={secret}" in uri
    assert "&algorithm=SHA1&digits=6&period=30" in uri


def test_secret_encrypt_decrypt_roundtrip() -> None:
    secret = generate_totp_secret()
    token = encrypt_secret(secret)
    assert decrypt_secret(token) == secret


def test_decrypt_secret_invalid_token_returns_none() -> None:
    assert decrypt_secret("not-a-valid-token") is None
