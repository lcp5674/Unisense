"""冲突检测 LLM 补位客户端（TD §12.4 / FR-09 语义补位）。

使用统一的 LlmClient 协议，支持 OpenAI 兼容接口。
"""

from __future__ import annotations

import json
import logging
from typing import Protocol, runtime_checkable

from app.services.llm.client import LlmClient, LlmError

logger = logging.getLogger("unisense.conflict.llm")


@runtime_checkable
class ConflictLlmClient(Protocol):
    """语义同义判定协议。"""

    async def judge_same_semantics(self, candidate_def: str, existing_def: str) -> bool | None:
        """判定两个口径定义是否语义同义。

        Returns:
            ``True`` 同义；``False`` 不同义；``None`` 弃权 / 不可用。
        """
        ...


class UnifiedConflictLlmClient:
    """基于统一 LlmClient 的语义判定客户端。"""

    def __init__(self, llm: LlmClient | None = None) -> None:
        # 从统一 LLM 工厂构建，避免与本模块的 build_llm_client 递归
        from app.services.llm.client import build_llm_client as _build_unified_llm

        self._llm = llm or _build_unified_llm()

    @property
    def enabled(self) -> bool:
        return self._llm.enabled

    async def judge_same_semantics(self, candidate_def: str, existing_def: str) -> bool | None:
        if not self._llm.enabled:
            return None

        prompt = (
            "你是数据治理平台的语义判定引擎。下面是两个指标口径的业务定义，"
            "请判断它们是否描述同一个业务度量（同义口径，仅表述/命名不同）。\n"
            f"口径A：{candidate_def}\n口径B：{existing_def}\n"
            '仅回复 JSON：{"same": true} 或 {"same": false}。'
        )

        try:
            result = await self._llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=100,
                response_format={"type": "json_object"},
            )
            content = result.get("content", "").strip()
            parsed = json.loads(content)
            val = parsed.get("same")
            if isinstance(val, bool):
                return val
            return None
        except (LlmError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("冲突 LLM 判定失败（降级为词法判定）: %s", exc)
            return None

    async def close(self) -> None:
        if isinstance(self._llm, LlmClient):
            await self._llm.close()


# 保持向后兼容的别名
DeterministicFallbackLlmClient = UnifiedConflictLlmClient
HttpLlmClient = UnifiedConflictLlmClient


def build_conflict_llm_client() -> ConflictLlmClient:
    """构建冲突检测 LLM 客户端。"""
    return UnifiedConflictLlmClient()
