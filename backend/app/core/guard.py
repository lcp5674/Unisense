"""输入安全守卫：拦截 SQL 注入（对齐 TD §13 安全 / DEV_GUIDE §9）。

仅对进入后端的 query 参数与 JSON body 顶层标量字符串做轻量正则扫描，
命中即返回 400 + ``error_code=INJECTION_DETECTED``（绝不拼接进 SQL）。
所有落库操作均经由 SQLAlchemy 参数化查询，本守卫是纵深防御的额外一层。
"""

from __future__ import annotations

import re

from fastapi import Request

from app.core.exceptions import BusinessError

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)('\s*or\s*'|'\s*or\s*\d)"),
    re.compile(r"(?i)\bor\b\s+\d+\s*=\s*\d+"),
    re.compile(r"(?i)--"),
    re.compile(r"(?i);\s*(drop|delete|update|insert|truncate|alter)\b"),
    re.compile(r"(?i)\bunion\b.{0,40}?\bselect\b"),
    re.compile(r"(?i)/\*"),
    re.compile(r"(?i)\*/"),
    re.compile(r"(?i)\bxp_cmdshell\b"),
    re.compile(r"(?i)\bsleep\s*\("),
    re.compile(r"(?i)\bbenchmark\s*\("),
    re.compile(r"(?i)\bwaitfor\b\s+delay\b"),
]


def _is_suspicious(value: str) -> bool:
    return any(p.search(value) for p in _PATTERNS)


async def guard_against_injection(request: Request) -> None:
    """FastAPI 依赖：扫描 query 参数与 JSON body 顶层字符串，命中注入即拦截。"""
    for value in request.query_params.values():
        if isinstance(value, str) and _is_suspicious(value):
            raise BusinessError("检测到疑似 SQL 注入，请求已拦截", error_code="INJECTION_DETECTED")
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            for value in body.values():
                if isinstance(value, str) and _is_suspicious(value):
                    raise BusinessError(
                        "检测到疑似 SQL 注入，请求已拦截",
                        error_code="INJECTION_DETECTED",
                    )
