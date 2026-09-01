"""输入安全守卫：拦截 SQL 注入（对齐 TD §13 安全 / DEV_GUIDE §9）。

对进入后端的 query 参数与 JSON body **所有层级**的字符串做轻量正则扫描，
命中即返回 422 + ``error_code=INJECTION_DETECTED``（绝不拼接进 SQL）。
支持递归扫描嵌套 dict/list 结构，最大递归深度 10 层（防止栈溢出攻击）。
所有落库操作均经由 SQLAlchemy 参数化查询，本守卫是纵深防御的额外一层。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

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

# 路径豁免中 list 任意元素的占位段：``candidates[].definition_json`` 的 ``[]`` 部分
_LIST_ANY = "[*]"

# B4（审查修复）：纯业务文本字段豁免——驳回原因/变更说明/描述/备注等合法业务文本
# 含 ``--``/``/*``（如「口径错误；参考--附录」「成本--收入」「次日/*结算」）是正常的，
# 此前被注入正则一律 422（用户完全无法理解哪里非法）。这些字段仅落库展示/通知，
# 不拼接进任何 DB 查询（SQL 拼接处已参数化，守卫是纵深防御），故对 **字符串值**
# 豁免扫描；嵌套 dict/list 仍递归扫描（防攻击者把 payload 藏进深层结构绕过）。
_BUSINESS_TEXT_FIELDS = frozenset(
    {
        "reason",
        "description",
        "comment",
        "note",
        "message",
        "remark",
        "feedback",
        "change_reason",
        "summary",
        "definition",
        "business_definition",
        "pseudo_definition",
        "dw_definition",
        "etl_sql",
        "title",
        "content",
        "instruction",
    }
)


def _is_suspicious(value: str) -> bool:
    return any(p.search(value) for p in _PATTERNS)


def _parse_exempt_path(path: str) -> tuple[str, ...]:
    """解析路径豁免语法：``"candidates[].definition_json"`` → 路径段元组。

    点号分隔段；``[]`` 后缀表示该段是 list，匹配其中任意元素（list 元素上的
    后续段作用于每个元素的同名字段）。非法输入（空段）抛 ValueError 快速失败。
    """
    segments: list[str] = []
    for part in path.split("."):
        if not part:
            raise ValueError(f"非法豁免路径：{path!r}")
        if part.endswith("[]"):
            segments.append(part[: -len("[]")] or _LIST_ANY)
            segments.append(_LIST_ANY)
        else:
            segments.append(part)
    return tuple(segments)


def _is_path_exempt(exempt_paths: frozenset[tuple[str, ...]], path: tuple[str, ...]) -> bool:
    """判断 path 是否命中任一豁免路径（豁免路径是其下整棵子树的根，前缀匹配）。"""
    return any(len(path) >= len(p) and path[: len(p)] == p for p in exempt_paths)


def _scan_deep(
    value: object,
    depth: int = 0,
    max_depth: int = _MAX_DEPTH,
    exempt_paths: frozenset[tuple[str, ...]] = frozenset(),
    current_path: tuple[str, ...] = (),
) -> bool:
    """递归扫描嵌套 dict/list 中的所有字符串值，检测注入模式。

    Args:
        value: 待扫描的值（可能是 dict、list、str 或其他类型）。
        depth: 当前递归深度。
        max_depth: 最大递归深度，超过即截断（防止恶意深层嵌套攻击）。
        exempt_paths: 豁免路径集——命中路径的子树整体跳过扫描。顶层字段豁免
            （guard_against_injection_exempt）即单段路径 ``(key,)`` 的退化情况；
            嵌套豁免用 guard_against_injection_exempt_paths（如
            ``("candidates", "[*]", "definition_json")``）。路径必须精确匹配，
            不会因同名键自动豁免深层（保守设计，防绕过）。
        current_path: 当前扫描所处路径（内部递归用）。

    Returns:
        True 如果发现可疑字符串，False 否则。
    """
    if depth > max_depth:
        raise BusinessError("请求嵌套层级超限，已拦截", error_code="INJECTION_DETECTED")
    if isinstance(value, str):
        return _is_suspicious(value)
    if isinstance(value, dict):
        for key, v in value.items():
            child_path = current_path + (key,)
            if _is_path_exempt(exempt_paths, child_path):
                continue
            # B4（审查修复）：纯业务文本字段的字符串值豁免注入扫描（含 -- /* 属合法
            # 业务文本）；嵌套 dict/list 仍递归（防深层藏 payload）
            if key in _BUSINESS_TEXT_FIELDS and isinstance(v, str):
                continue
            if _scan_deep(v, depth + 1, max_depth, exempt_paths, child_path):
                return True
    if isinstance(value, list):
        # list 元素在路径上以 [*] 段表示，使豁免路径 ``candidates[].definition_json``
        # 能匹配 candidates 列表中每个候选元素的 definition_json 字段。
        for item in value:
            if _scan_deep(item, depth + 1, max_depth, exempt_paths, current_path + (_LIST_ANY,)):
                return True
    return False


async def _guard_request(request: Request, exempt_paths: frozenset[tuple[str, ...]]) -> None:
    """共享扫描逻辑：query 参数全量扫描，JSON body 跳过豁免路径子树。"""
    for value in request.query_params.values():
        if isinstance(value, str) and _is_suspicious(value):
            raise BusinessError("检测到疑似 SQL 注入，请求已拦截", error_code="INJECTION_DETECTED")
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
        except Exception:
            body = None
        if body is not None and _scan_deep(body, exempt_paths=exempt_paths):
            raise BusinessError(
                "检测到疑似 SQL 注入，请求已拦截",
                error_code="INJECTION_DETECTED",
            )


async def guard_against_injection(request: Request) -> None:
    """FastAPI 依赖：扫描 query 参数与 JSON body 所有层级字符串，命中注入即拦截。"""
    await _guard_request(request, frozenset())


def guard_against_injection_exempt(
    *exempt_fields: str,
) -> Callable[[Request], Awaitable[None]]:
    """FastAPI 依赖工厂：跳过指定**顶层** JSON 字段的注入扫描。

    适用场景：字段值本身就是要处理的文本，而非查询参数——如血缘 SQL 解析
    （``/lineage/parse``）的 ``sql`` 字段。该字段仅进入 sqlglot 纯函数解析器
    （解析失败降级返回空边，不执行、不拼接进任何 DB 查询），注入正则反而会误伤
    合法 SQL（``--`` 行注释、``/* */`` 块注释、``UNION [ALL] SELECT``、
    多语句 ETL）。其余字段与 query 参数仍保持全量扫描，纵深防御不削弱。

    注意：豁免仅作用于**顶层** dict 键，不递归豁免嵌套同名键（保守设计，
    避免攻击者把 payload 藏进深层结构绕过扫描）。嵌套字段的精确豁免请用
    ``guard_against_injection_exempt_paths``。

    Args:
        *exempt_fields: 需要豁免扫描的顶层 JSON 字段名。

    Returns:
        FastAPI 依赖函数（与 guard_against_injection 同构）。
    """
    paths = frozenset((f,) for f in exempt_fields)

    async def _guard(request: Request) -> None:
        await _guard_request(request, paths)

    return _guard


def guard_against_injection_exempt_paths(
    *exempt_paths: str,
) -> Callable[[Request], Awaitable[None]]:
    """FastAPI 依赖工厂：跳过指定**路径**（可含嵌套 list/dict）的注入扫描。

    与 guard_against_injection_exempt（仅顶层字段）互补——当待解析/待存储的
    SQL 文本位于嵌套结构中时使用，如 ``/metric-definitions/batch-register-from-sql``
    的候选口径 ``candidates[].definition_json``（该子树承载 ``sql``/``expression``
    等 SQL 表达式，仅经 sqlglot 纯函数解析/存储，不执行、不拼接进任何 DB 查询）。

    路径语法：点号分隔段，``[]`` 后缀匹配 list 中任意元素。示例：
    - ``"candidates[].definition_json"`` 匹配 ``{"candidates": [{"definition_json": {...}}]}``
      中每个候选元素的 definition_json 子树（整体跳过扫描）。

    其余路径与 query 参数仍全量扫描；豁免必须**精确**写出完整路径，不会因
    同名键自动豁免深层（保守设计，避免攻击者把 payload 藏进深层结构绕过扫描）。

    Args:
        *exempt_paths: 需要豁免扫描的路径（点号分隔，``[]`` 表示 list 任意元素）。

    Returns:
        FastAPI 依赖函数（与 guard_against_injection 同构）。
    """
    paths = frozenset(_parse_exempt_path(p) for p in exempt_paths)

    async def _guard(request: Request) -> None:
        await _guard_request(request, paths)

    return _guard
