"""输入安全守卫：拦截 SQL 注入（对齐 TD §13 安全 / DEV_GUIDE §9）。

对进入后端的 query 参数与 JSON body **所有层级**的字符串做轻量正则扫描，
命中即返回 422 + ``error_code=INJECTION_DETECTED``（绝不拼接进 SQL）。
支持递归扫描嵌套 dict/list 结构，最大递归深度 10 层（防止栈溢出攻击）。
所有落库操作均经由 SQLAlchemy 参数化查询，本守卫是纵深防御的额外一层。
"""

from __future__ import annotations

import re

from fastapi import Request

from app.core.exceptions import BusinessError

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)('\s*or\s*'|'\s*or\s*\d)"),
    re.compile(r"(?i)\bor\b\s+\d+\s*=\s*\d+"),
    re.compile(r"(?i)(?<!\d)--(?!\d)"),
    re.compile(r"(?i);\s*(drop|delete|update|insert|truncate|alter)\b"),
    re.compile(r"(?i)\bunion\b.{0,40}?\bselect\b"),
    re.compile(r"(?i)/\*"),
    # 注意：*/ 单独出现（无前置 /*）不是 SQL 注入向量，且会误伤合法 cron 表达式
    # （如 "*/5 * * * *" 每分钟/每5分钟），故不单独拦截——SQL 块注释必须以 /* 开头，
    # 上面的 /\* 模式已覆盖真实注入场景。
    re.compile(r"(?i)\bxp_cmdshell\b"),
    re.compile(r"(?i)\bsleep\s*\("),
    re.compile(r"(?i)\bbenchmark\s*\("),
    re.compile(r"(?i)\bwaitfor\b\s+delay\b"),
]

_MAX_DEPTH = 10


def _is_suspicious(value: str) -> bool:
    return any(p.search(value) for p in _PATTERNS)


def _scan_deep(value: object, depth: int = 0, max_depth: int = _MAX_DEPTH) -> bool:
    """递归扫描嵌套 dict/list 中的所有字符串值，检测注入模式。

    Args:
        value: 待扫描的值（可能是 dict、list、str 或其他类型）。
        depth: 当前递归深度。
        max_depth: 最大递归深度，超过即截断（防止恶意深层嵌套攻击）。

    Returns:
        True 如果发现可疑字符串，False 否则。
    """
    if depth > max_depth:
        raise BusinessError("请求嵌套层级超限，已拦截", error_code="INJECTION_DETECTED")
    if isinstance(value, str):
        return _is_suspicious(value)
    if isinstance(value, dict):
        for v in value.values():
            if _scan_deep(v, depth + 1, max_depth):
                return True
    if isinstance(value, list):
        for item in value:
            if _scan_deep(item, depth + 1, max_depth):
                return True
    return False


async def guard_against_injection(request: Request) -> None:
    """FastAPI 依赖：扫描 query 参数与 JSON body 所有层级字符串，命中注入即拦截。"""
    for value in request.query_params.values():
        if isinstance(value, str) and _is_suspicious(value):
            raise BusinessError("检测到疑似 SQL 注入，请求已拦截", error_code="INJECTION_DETECTED")
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
        except Exception:
            body = None
        if body is not None and _scan_deep(body):
            raise BusinessError(
                "检测到疑似 SQL 注入，请求已拦截",
                error_code="INJECTION_DETECTED",
            )
