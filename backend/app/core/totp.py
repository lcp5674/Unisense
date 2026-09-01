"""TOTP（RFC 6238）双因子认证工具——纯标准库实现，零第三方依赖。

- ``generate_totp_secret``：生成 32 字符 base32 密钥（160 位随机）。
- ``totp_uri``：生成 otpauth:// 标准 URI（供身份验证器扫码/手动录入）。
- ``verify_totp``：按当前时间窗校验 6 位动态码（容忍前后各 1 个周期的时钟漂移）。
- ``encrypt_secret/decrypt_secret``：TOTP 密钥经 SecretManager（Fernet）加密落库，
  杜绝明文存储（与数据源连接配置同安全级别，TD §12.4）。

默认参数与主流验证器（Google Authenticator / Microsoft Authenticator）一致：
SHA1 + 6 位 + 30 秒周期。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

TOTP_PERIOD = 30
TOTP_DIGITS = 6
TOTP_ISSUER = "WeSemantics"


def generate_totp_secret() -> str:
    """生成随机 TOTP 密钥（base32，160 位 = 32 字符，无填充）。"""
    raw = secrets.token_bytes(20)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _base32_decode(secret: str) -> bytes:
    """解码 base32 密钥（容忍空格/小写/缺失填充）。"""
    cleaned = "".join(
        ch for ch in secret.upper() if ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    )
    padding = "=" * ((8 - len(cleaned) % 8) % 8)
    return base64.b32decode(cleaned + padding)


def _hotp(secret_bytes: bytes, counter: int, digits: int = TOTP_DIGITS) -> str:
    """HOTP（RFC 4226）动态码：HMAC-SHA1 截断为 6 位数字。"""
    msg = struct.pack(">Q", counter)
    digest = hmac.new(secret_bytes, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)


def totp_uri(secret: str, account: str, issuer: str = TOTP_ISSUER) -> str:
    """生成 otpauth:// 标准 URI（身份验证器扫码/手动录入）。"""
    label = f"{issuer}:{account}"
    return (
        f"otpauth://totp/{quote(label)}?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_PERIOD}"
    )


def verify_totp(
    secret: str, code: str, window: int = 1, ts: float | None = None
) -> bool:
    """校验 TOTP 动态码。

    Args:
        secret: base32 密钥（明文，调用方负责解密）。
        code: 用户输入的 6 位动态码。
        window: 容忍前后窗口数（默认 1 = 校验 3 个时间窗，兼容时钟漂移）。
        ts: 时间戳（测试注入用），缺省取当前时间。

    Returns:
        匹配返回 True；密钥非法/码格式非法恒 False（fail-safe，不抛异常）。
    """
    if not code or not secret:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return False
    try:
        secret_bytes = _base32_decode(secret)
    except Exception:  # noqa: BLE001 - 密钥非法按校验失败处理（fail-safe）
        return False
    counter = int((ts if ts is not None else time.time()) // TOTP_PERIOD)
    for offset in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret_bytes, counter + offset), code):
            return True
    return False


def encrypt_secret(secret: str) -> str:
    """加密 TOTP 密钥（Fernet 对称加密，落库存储）。"""
    from app.core.secrets import SecretManager

    return SecretManager.encrypt({"totp": secret})


def decrypt_secret(token: str) -> str | None:
    """解密 TOTP 密钥；解密失败（密钥轮换/数据损坏）返回 None。"""
    from app.core.secrets import SecretManager

    try:
        data = SecretManager.decrypt(token)
        value = data.get("totp")
        return str(value) if value else None
    except Exception:  # noqa: BLE001 - 解密失败按无密钥处理（fail-safe）
        return None
