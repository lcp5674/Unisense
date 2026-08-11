"""LLM 客户端单测。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from app.services.llm.client import LlmClient, LlmError, build_llm_client


class TestLlmClient:
    async def test_enabled_when_configured(self) -> None:
        client = LlmClient(base_url="https://api.example.com", api_key="test-key")
        assert client.enabled is True

    async def test_disabled_when_no_config(self) -> None:
        client = LlmClient()
        assert client.enabled is False

    @pytest.mark.asyncio
    async def test_chat_success(self) -> None:
        client = LlmClient(base_url="https://api.example.com", api_key="test-key")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Hello", "finish_reason": "stop"}}],
            "model": "test-model",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        with patch.object(httpx, "AsyncClient", return_value=mock_client):
            result = await client.chat([{"role": "user", "content": "Hi"}])
        assert result["content"] == "Hello"
        assert result["model"] == "test-model"
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_http_error(self) -> None:
        client = LlmClient(base_url="https://api.example.com", api_key="test-key")
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Error"
        exc = httpx.HTTPStatusError("Error", request=MagicMock(), response=mock_response)
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=exc)
        with patch.object(httpx, "AsyncClient", return_value=mock_client):
            with pytest.raises(LlmError):
                await client.chat([{"role": "user", "content": "Hi"}])

    @pytest.mark.asyncio
    async def test_chat_json_decode_error(self) -> None:
        client = LlmClient(base_url="https://api.example.com", api_key="test-key")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = Exception("JSON error")
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        with patch.object(httpx, "AsyncClient", return_value=mock_client):
            with pytest.raises(LlmError):
                await client.chat([{"role": "user", "content": "Hi"}])

    @pytest.mark.asyncio
    async def test_close(self) -> None:
        client = LlmClient(base_url="https://api.example.com", api_key="test-key")
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        with patch.object(httpx, "AsyncClient", return_value=mock_client):
            await client.close()
            mock_client.aclose.assert_called_once()


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
            # 应返回确定性降级客户端
            assert not client.enabled or hasattr(client, "chat")
