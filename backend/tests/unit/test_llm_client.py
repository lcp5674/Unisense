"""LLM 客户端单测。

修复策略：LlmClient 在 __init__ 内即创建 httpx.AsyncClient，
测试需在 __init__ 之前 patch，或直接替换 client._client。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.llm.client import LlmClient, LlmError, build_llm_client


@pytest.fixture(autouse=True)
def _reset_shared_llm_breaker() -> None:
    """每个测试前重置共享 LLM 熔断器单例。

    同文件的失败测试（http_error/网络错误）会调用 ``_LLM_BREAKER.record_failure()``，
    把模块级单例打满后，``test_chat_success`` 的开头 ``if not _LLM_BREAKER.allow()``
    会直接拒绝（测试顺序依赖）。此处显式恢复关闭态，保证测试相互独立。
    """
    from app.services.llm.client import _LLM_BREAKER

    _LLM_BREAKER._open = False
    _LLM_BREAKER._failures = 0
    _LLM_BREAKER._opened_at = None
    _LLM_BREAKER._probing = False
    _LLM_BREAKER._probing_since = None
    _LLM_BREAKER._recent_outcomes.clear()


class TestLlmClient:
    async def test_enabled_when_configured(self) -> None:
        client = LlmClient(base_url="https://api.example.com", api_key="test-key")
        assert client.enabled is True
        await client.close()

    async def test_disabled_when_no_config(self) -> None:
        # 隔离 settings：无 base_url / api_key 时 disabled
        with patch("app.services.llm.client.settings") as mock_settings:
            mock_settings.llm_base_url = ""
            mock_settings.llm_api_key = ""
            client = LlmClient()
            assert client.enabled is False
            await client.close()

    @pytest.mark.asyncio
    async def test_chat_success(self) -> None:
        client = LlmClient(base_url="https://api.example.com", api_key="test-key")
        # 替换内部 _client 为 mock
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Hello", "finish_reason": "stop"}}],
            "model": "test-model",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)
        result = await client.chat([{"role": "user", "content": "Hi"}])
        assert result["content"] == "Hello"
        assert result["model"] == "test-model"
        client._client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_http_error(self) -> None:
        client = LlmClient(base_url="https://api.example.com", api_key="test-key")
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Error"
        exc = httpx.HTTPStatusError("Error", request=MagicMock(), response=mock_response)
        client._client = MagicMock()
        client._client.post = AsyncMock(side_effect=exc)
        with pytest.raises(LlmError):
            await client.chat([{"role": "user", "content": "Hi"}])

    @pytest.mark.asyncio
    async def test_chat_bad_response_shape(self) -> None:
        client = LlmClient(base_url="https://api.example.com", api_key="test-key")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        # 缺少 choices 字段
        mock_response.json.return_value = {"model": "test"}
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)
        with pytest.raises(LlmError):
            await client.chat([{"role": "user", "content": "Hi"}])

    @pytest.mark.asyncio
    async def test_chat_not_enabled(self) -> None:
        client = LlmClient()
        with pytest.raises(LlmError):
            await client.chat([{"role": "user", "content": "Hi"}])

    @pytest.mark.asyncio
    async def test_close(self) -> None:
        client = LlmClient(base_url="https://api.example.com", api_key="test-key")
        client._client = MagicMock()
        client._client.aclose = AsyncMock()
        await client.close()
        client._client.aclose.assert_called_once()


class TestBuildLlmClient:
    def test_build_with_config(self) -> None:
        with patch("app.services.llm.client.settings") as mock_settings:
            mock_settings.llm_base_url = "https://api.example.com"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_default_model = "test-model"
            client = build_llm_client()
            assert isinstance(client, LlmClient)
            assert client.enabled is True

    def test_build_without_config(self) -> None:
        with patch("app.services.llm.client.settings") as mock_settings:
            mock_settings.llm_base_url = ""
            mock_settings.llm_api_key = ""
            mock_settings.llm_default_model = ""
            client = build_llm_client()
            # 无配置时返回确定性降级客户端（DeterministicFallbackLlmClient）
            assert hasattr(client, "chat")
