"""dp 调度血缘 arq 周期任务模块单元测试。

覆盖：
- ``_make_llm_chat`` LLM 故障降级：异常应返回空 content（协议层建单），
  而不是因 logger kwargs 误用抛 TypeError（P0-1 回归）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.lineage.dp_sync_tasks import _make_llm_chat


@pytest.mark.asyncio
async def test_make_llm_chat_llm_exception_returns_empty_content() -> None:
    """LLM 调用抛异常 → 返回 {'content': ''}，不因日志格式抛 TypeError。

    回归：此前 logger.warning("msg", error=...) 为 structlog kwargs 风格，
    stdlib logging 收到会抛 TypeError，使降级链断裂（P0-1）。
    """
    llm_chat = _make_llm_chat(MagicMock())
    with patch(
        "app.services.llm.client.LlmClient.chat",
        new=AsyncMock(side_effect=RuntimeError("gateway down")),
    ):
        result = await llm_chat([{"role": "user", "content": "hi"}])
    assert result == {"content": ""}


@pytest.mark.asyncio
async def test_make_llm_chat_success_returns_content() -> None:
    """LLM 正常返回 → 透传 content（max_tokens 默认 2000）。"""
    llm_chat = _make_llm_chat(MagicMock())
    with patch(
        "app.services.llm.client.LlmClient.chat",
        new=AsyncMock(return_value={"content": "ok", "finish_reason": "stop"}),
    ) as mock_chat:
        result = await llm_chat([{"role": "user", "content": "hi"}])
    assert result["content"] == "ok"
    assert mock_chat.await_args.kwargs.get("temperature") == 0.0
    assert mock_chat.await_args.kwargs.get("max_tokens") == 2000
