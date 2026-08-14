"""LLM 结构化输出统一解析器（TD §11 韧性 / FR-023 格式保证）。

所有 LLM 输出的解析收敛到本模块，统一处理三类常见的「格式不确定性」：
- markdown 代码围栏（`` ```json ... ``` ``）包裹；
- 模型用别名替代约定字段名（如 ``desc`` / ``text`` / ``label`` 代替 ``description``）；
- 数值类型漂移（``"0.8"`` 字符串与 ``0.8`` 数字混用、置信度越界）。

设计原则：解析失败一律返回 ``None``（上层据此降级为「LLM 不可用」，绝不抛异常、
绝不静默误用错误字段），由调用方决定是否重试或人工兜底。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 匹配以 ``` 或 ```json 开头/结尾的代码围栏，捕获中间内容（忽略大小写与首尾空白）。
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def strip_code_fence(raw: str) -> str:
    """剥离 markdown 代码围栏；非围栏文本原样返回。"""
    stripped = raw.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """将 LLM 文本解析为 JSON 对象。

    非字符串、空串、非法 JSON 或非 dict 结构统一返回 ``None``。
    """
    if not raw or not raw.strip():
        return None
    try:
        parsed = json.loads(strip_code_fence(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def extract_str_field(obj: dict[str, Any], *aliases: str) -> str | None:
    """按别名顺序抽取首个非空字符串字段；数字也会转为文本兜底。"""
    for key in aliases:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                return text
    return None


def extract_numeric_field(
    obj: dict[str, Any],
    *aliases: str,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float | None:
    """抽取首个数值字段并强转为 float；越界或缺失返回 ``None``。"""
    for key in aliases:
        value = obj.get(key)
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if min_value is not None and number < min_value:
            return None
        if max_value is not None and number > max_value:
            return None
        return number
    return None


def parse_description_result(raw: str) -> tuple[str | None, float | None]:
    """解析字段/表描述推断结果。

    Returns:
        ``(description, confidence)``；任一缺失或越界返回 ``(None, None)``。
    """
    obj = parse_json_object(raw)
    if obj is None:
        return None, None
    description = extract_str_field(obj, "description", "desc", "text", "label", "summary")
    confidence = extract_numeric_field(
        obj, "confidence", "score", "prob", "certainty", min_value=0.0, max_value=1.0
    )
    if description is None or confidence is None:
        return None, None
    return description, confidence


def parse_bool_result(raw: str, *aliases: str) -> bool | None:
    """解析布尔型判定结果（如同义判定 ``{"same": true}``）。

    Returns:
        ``True`` / ``False`` / ``None``（无法判定时）。
    """
    obj = parse_json_object(raw)
    if obj is None:
        return None
    keys = aliases or ("same", "is_same", "equal", "result", "match")
    for key in keys:
        value = obj.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "yes", "1"):
                return True
            if normalized in ("false", "no", "0"):
                return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return bool(value)
    return None
