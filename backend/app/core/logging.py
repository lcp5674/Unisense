"""structlog 日志配置。

对齐 DEV_GUIDE §16 和 TD §16.2。

结构化 JSON 日志格式: ``{ts, level, service, trace_id, span_id, msg, ctx_json}``
"""

from __future__ import annotations

import logging
import re
from typing import Any

import structlog

from app.core.config import settings

# 敏感字段名黑名单（值一律脱敏）
_SENSITIVE_FIELDS = frozenset(
    {
        "password",
        "token",
        "secret",
        "api_key",
        "access_token",
        "refresh_token",
        "authorization",
    }
)

# PII 正则（对齐 TD §13 pii.identification.regex_list）
_PII_PATTERNS = [
    (re.compile(r"\b\d{15,18}[Xx]?\b"), "***ID***"),  # 身份证
    (re.compile(r"\b1[3-9]\d{9}\b"), "***PHONE***"),  # 手机号
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "***EMAIL***"),  # 邮箱
    (re.compile(r"\b(\d{1,3}\.){3}\d{1,3}\b"), "***IP***"),
]


def _redact_processor(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """脱敏 processor：对敏感字段值和 PII 进行脱敏。

    Args:
        _logger: logger 实例（未使用）。
        _method_name: 方法名（未使用）。
        event_dict: 日志事件字典。

    Returns:
        脱敏后的日志事件字典。
    """
    for key in list(event_dict.keys()):
        val = event_dict[key]
        if key.lower() in _SENSITIVE_FIELDS:
            event_dict[key] = "***REDACTED***"
        elif key.lower() in ("ip", "client_ip", "remote_addr"):
            event_dict[key] = "***IP***"
        elif isinstance(val, str):
            for pattern, replacement in _PII_PATTERNS:
                val = pattern.sub(replacement, val)
            event_dict[key] = val
    return event_dict


def configure_logging() -> None:
    """配置 structlog 日志系统。

    生产环境输出 JSON 格式；开发环境输出彩色控制台。
    """
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # 配置标准 logging（被 structlog 调用）
    logging.basicConfig(
        format="%(message)s",
        stream=None,
        level=log_level,
    )

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _redact_processor,
    ]

    if settings.log_format == "json" or settings.env != "local":
        # 生产：JSON 输出
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        # 开发：彩色控制台
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "unisense") -> Any:
    """获取 structlog logger。

    Args:
        name: logger 名称。

    Returns:
        绑定的 structlog logger。
    """
    return structlog.get_logger(name)
