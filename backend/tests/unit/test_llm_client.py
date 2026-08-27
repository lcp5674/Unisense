"""LLM 客户端单测。

修复策略：LlmClient 在 __init__ 内即创建 httpx.AsyncClient，
测试需在 __init__ 之前 patch，或直接替换 client._client。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.llm.client import (
    LlmClient,
    LlmError,
    build_llm_client,
    chat_completions_url,
    models_url,
    normalize_base_url,
)


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


class _FakeBreaker:
    """最小熔断器替身：仅记录 record_failure/record_success 调用次数。

    用于验证「空/垃圾内容必须走 record_failure 而非 record_success」——这是坏实例
    永不熔断、批量解析墙钟拖到几百秒的根因修复。
    """

    def __init__(self) -> None:
        self.failures = 0
        self.successes = 0
        self._open = False
        self._probing = False
        self._probing_since = None
        self._opened_at = None
        self._recent_outcomes: list[bool] = []

    def allow(self) -> bool:
        return True

    def record_failure(self) -> None:
        self.failures += 1

    def record_success(self) -> None:
        self.successes += 1


class TestChatCompletionsUrl:
    """base_url 三种合法形态的端点规范化（修复 /v1/v1/... 404 的回归证据）。"""

    def test_bare_domain_appends_v1_path(self) -> None:
        # deepseek 预设：裸域名 → /v1/chat/completions
        assert (
            chat_completions_url("https://api.deepseek.com")
            == "https://api.deepseek.com/v1/chat/completions"
        )

    def test_base_url_with_v1_suffix(self) -> None:
        # openai 预设：base_url 已含 /v1 → 只追加 /chat/completions，避免 /v1/v1
        assert (
            chat_completions_url("https://api.openai.com/v1")
            == "https://api.openai.com/v1/chat/completions"
        )

    def test_full_endpoint_passthrough(self) -> None:
        # 用户直接填完整端点 → 原样返回，不重复拼接
        assert (
            chat_completions_url("https://api.example.com/v1/chat/completions")
            == "https://api.example.com/v1/chat/completions"
        )

    def test_trailing_slash_stripped(self) -> None:
        assert (
            chat_completions_url("https://api.deepseek.com/")
            == "https://api.deepseek.com/v1/chat/completions"
        )

    def test_empty_base_url(self) -> None:
        assert chat_completions_url("") == ""
        assert chat_completions_url("   ") == ""


class TestNormalizeBaseUrl:
    """base_url 归一化：完整端点/含 /v1/裸 URL 统一存为干净 base_url（幂等）。"""

    def test_full_endpoint_strips_chat_suffix(self) -> None:
        # 用户填完整 chat/completions 端点 → 归一化为干净 base URL
        assert (
            normalize_base_url("http://host.docker.internal:19090/v1/chat/completions")
            == "http://host.docker.internal:19090"
        )

    def test_v1_suffix_stripped(self) -> None:
        assert normalize_base_url("http://host.docker.internal:19090/v1") == (
            "http://host.docker.internal:19090"
        )

    def test_bare_url_unchanged(self) -> None:
        # 裸 URL 归一化幂等（不破坏原有配置）
        assert normalize_base_url("http://host.docker.internal:19090") == (
            "http://host.docker.internal:19090"
        )

    def test_full_models_endpoint_stripped(self) -> None:
        assert normalize_base_url("https://api.example.com/v1/models") == "https://api.example.com"

    def test_chat_without_v1_stripped(self) -> None:
        assert (
            normalize_base_url("https://api.example.com/chat/completions")
            == "https://api.example.com"
        )

    def test_trailing_slash_stripped(self) -> None:
        assert normalize_base_url("https://api.deepseek.com/") == "https://api.deepseek.com"

    def test_empty_base_url(self) -> None:
        assert normalize_base_url("") == ""
        assert normalize_base_url("   ") == ""


class TestModelsUrl:
    """models_url 端点规范化（一键获取模型/快速探测用，与 chat 端点形态对齐）。"""

    def test_bare_domain_appends_v1_path(self) -> None:
        assert models_url("https://api.deepseek.com") == "https://api.deepseek.com/v1/models"

    def test_base_url_with_v1_suffix(self) -> None:
        assert models_url("https://api.openai.com/v1") == "https://api.openai.com/v1/models"

    def test_chat_endpoint_replaced_with_models(self) -> None:
        # 用户填了完整 chat 端点 → 替换为同前缀 /models
        assert (
            models_url("https://api.example.com/v1/chat/completions")
            == "https://api.example.com/v1/models"
        )

    def test_models_endpoint_passthrough(self) -> None:
        assert (
            models_url("https://api.example.com/v1/models") == "https://api.example.com/v1/models"
        )

    def test_trailing_slash_stripped(self) -> None:
        assert models_url("https://api.deepseek.com/") == "https://api.deepseek.com/v1/models"

    def test_empty_base_url(self) -> None:
        assert models_url("") == ""
        assert models_url("   ") == ""


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
    async def test_chat_default_forces_json_object(self) -> None:
        """缺省 response_format 时默认强制 json_object（既有 JSON 结构化调用向后兼容）。"""
        client = LlmClient(base_url="https://api.example.com", api_key="test-key")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Hello", "finish_reason": "stop"}}],
            "model": "test-model",
        }
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)
        await client.chat([{"role": "user", "content": "Hi"}])
        payload = client._client.post.call_args[1]["json"]
        assert payload["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_chat_text_format_passed_through(self) -> None:
        """显式 {"type": "text"} 时原样传给网关（自由文本，避免纯文本被 json_object 污染）。"""
        client = LlmClient(base_url="https://api.example.com", api_key="test-key")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "纯文本口径", "finish_reason": "stop"}}],
            "model": "test-model",
        }
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)
        result = await client.chat(
            [{"role": "user", "content": "Hi"}], response_format={"type": "text"}
        )
        payload = client._client.post.call_args[1]["json"]
        assert payload["response_format"] == {"type": "text"}
        assert result["content"] == "纯文本口径"

    @pytest.mark.asyncio
    async def test_chat_uses_normalized_url_when_base_has_v1(self) -> None:
        """base_url 已含 /v1（openai 预设）时，请求端点不得拼出 /v1/v1（回归 404）。"""
        client = LlmClient(base_url="https://api.openai.com/v1", api_key="test-key")
        assert client._chat_url == "https://api.openai.com/v1/chat/completions"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Hello", "finish_reason": "stop"}}],
            "model": "test-model",
        }
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)
        await client.chat([{"role": "user", "content": "Hi"}])
        called_url = client._client.post.call_args[0][0]
        assert called_url == "https://api.openai.com/v1/chat/completions"
        assert "/v1/v1/" not in called_url

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
    async def test_chat_empty_content_records_failure(self) -> None:
        """空 content 必须计为失败并抛错——否则坏实例永不熔断，每次请求白等其完整返回。

        回归保护：此前无条件 record_success() 复位熔断计数，空/垃圾返回被当成功，
        坏实例永不熔断，多语句批量解析墙钟拖到几百秒（实测 230s）。
        """
        breaker = _FakeBreaker()
        client = LlmClient(
            base_url="https://api.example.com", api_key="test-key", breaker=breaker
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "", "finish_reason": "stop"}}],
            "model": "test-model",
        }
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)
        with pytest.raises(LlmError):
            await client.chat([{"role": "user", "content": "Hi"}])
        assert breaker.failures == 1  # record_failure 而非 record_success
        assert breaker.successes == 0

    @pytest.mark.asyncio
    async def test_chat_stream_dump_content_records_failure(self) -> None:
        """流式协议原文垃圾（SSE 信封）必须计为失败并抛错。"""
        breaker = _FakeBreaker()
        client = LlmClient(
            base_url="https://api.example.com", api_key="test-key", breaker=breaker
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '[\\n {"type":"message","content":"xx"}]' * 300,
                        "finish_reason": "stop",
                    }
                }
            ],
            "model": "test-model",
        }
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)
        with pytest.raises(LlmError):
            await client.chat([{"role": "user", "content": "Hi"}])
        assert breaker.failures == 1
        assert breaker.successes == 0

    @pytest.mark.asyncio
    async def test_chat_good_content_records_success(self) -> None:
        """正常内容仍走 record_success（不误伤）。"""
        breaker = _FakeBreaker()
        client = LlmClient(
            base_url="https://api.example.com", api_key="test-key", breaker=breaker
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "正常内容", "finish_reason": "stop"}}],
            "model": "test-model",
        }
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)
        result = await client.chat([{"role": "user", "content": "Hi"}])
        assert result["content"] == "正常内容"
        assert breaker.successes == 1
        assert breaker.failures == 0

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
