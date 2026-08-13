"""通用编码生成工具（生产级，对齐 TD §12 / FR-010）。

为"编码类字段系统自动生成"提供统一范式，与 subject_domain._generate_unique_code、
collector._generate_unique_source_id 的约定保持一致：

- ``slugify_code``：显示名 → 小写下划线 slug（非字母数字折叠为下划线）。
- ``generate_unique_code``：在基础编码上做冲突自增后缀（``_2/_3/...``），上限保护。

使用方（术语/维度/维度成员/API 客户端/指标模板等）只需提供基础编码与存在性判定函数。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

#: 编码最大长度（对齐各模型 String(64)）
MAX_CODE_LEN = 64
#: 冲突自增后缀上限（防止死循环）
MAX_CODE_ATTEMPTS = 100

# 编码合法性：小写字母开头 + 小写字母数字下划线（对齐 auto_fill._CODE_SEGMENT_PATTERN）
CODE_SEGMENT_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def slugify_code(name: str) -> str:
    """把显示名规范化为编码片段：中文转英文、ASCII 保留、段落用下划线连接。

    中文段经中英术语字典翻译（贪心最长匹配），未覆盖词逐字拼音兜底；
    空格/标点作为分隔符。返回空串表示无可提取字符（纯标点/空白名）。

    与 ``subject_domain.service._slugify_code`` 行为保持一致。
    """
    from app.core.zh_en_dict import zh_to_en

    tokens: list[str] = []
    cur: list[str] = []  # 当前 ASCII 字母数字段
    cjk: list[str] = []  # 当前中文段
    for ch in name:
        is_cjk = "\u4e00" <= ch <= "\u9fff"
        if is_cjk:
            if cur:
                tokens.append("".join(cur))
                cur = []
            cjk.append(ch)
        elif re.match(r"[a-z0-9]", ch, re.IGNORECASE):
            if cjk:
                tokens.append(zh_to_en("".join(cjk)))
                cjk = []
            cur.append(ch.lower())
        else:
            if cjk:
                tokens.append(zh_to_en("".join(cjk)))
                cjk = []
            if cur:
                tokens.append("".join(cur))
                cur = []
    if cjk:
        tokens.append(zh_to_en("".join(cjk)))
    if cur:
        tokens.append("".join(cur))
    return "_".join(t for t in tokens if t)


async def generate_unique_code(
    base_id: str,
    exists_fn: Callable[[str], Awaitable[bool]],
    *,
    max_len: int = MAX_CODE_LEN,
    max_attempts: int = MAX_CODE_ATTEMPTS,
) -> str:
    """生成唯一编码：基础编码 + 冲突自增后缀（``_2/_3/...``）。

    Args:
        base_id: 基础编码（已含 slug / 前缀 / 域前缀）。
        exists_fn: 异步存在性判定（如查库 ``SELECT ... WHERE code = ?``）。
        max_len: 编码最大长度（默认 64，对齐模型列宽）。
        max_attempts: 冲突自增上限（默认 100）。

    Returns:
        唯一编码（未冲突时即 base_id，冲突时追加 ``_N`` 后缀）。

    Raises:
        RuntimeError: 超出 max_attempts 仍无法生成唯一编码（调用方应转业务异常）。
    """
    candidate = base_id[:max_len]
    n = 2
    while await exists_fn(candidate):
        suffix = f"_{n}"
        candidate = f"{base_id[: max_len - len(suffix)]}{suffix}"
        n += 1
        if n > max_attempts:
            raise RuntimeError(f"无法生成唯一编码（已尝试 {max_attempts} 次）: {base_id}")
    return candidate
