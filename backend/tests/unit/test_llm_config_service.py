"""LLM 配置服务单测（DB 优先 / env 兜底 / 加密落库 / 连通性测试）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.models.llm_config import LlmConfig
from app.services.llm.config_service import LlmConfigService
from app.services.llm.schemas import LlmConfigPayload


def _session() -> MagicMock:
    s = MagicMock()
    s.flush = AsyncMock()
    s.add = MagicMock()
    # 显式让 execute 返回普通 MagicMock（AsyncMock.return_value 默认也是 AsyncMock，
    # 会导致 scalar_one_or_none 返回 coroutine）
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    s.execute = AsyncMock(return_value=result)
    return s


def _row(**overrides: object) -> LlmConfig:
    from app.core.secrets import SecretManager

    cfg = {
        "id": 1,
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key_enc": SecretManager.encrypt({"api_key": "sk-test"}),
        "timeout": 30,
        "enabled": True,
        "updated_by": 1,
        "updated_at": None,
        "deleted_at": None,
    }
    cfg.update(overrides)
    row = LlmConfig()
    for k, v in cfg.items():
        setattr(row, k, v)
    return row


class TestGetEffective:
    async def test_db_config_when_enabled(self) -> None:
        s = _session()
        s.execute.return_value.scalar_one_or_none.return_value = _row()
        eff = await LlmConfigService(s).get_effective()
        assert eff["source"] == "db"
        assert eff["base_url"] == "https://api.deepseek.com"
        assert eff["api_key"] == "sk-test"
        assert eff["provider"] == "deepseek"

    async def test_env_fallback_when_no_db_row(self) -> None:
        s = _session()
        s.execute.return_value.scalar_one_or_none.return_value = None
        with patch("app.services.llm.config_service.settings") as ms:
            ms.llm_base_url = "https://api.env.com"
            ms.llm_api_key = "sk-env"
            ms.llm_default_model = "env-model"
            eff = await LlmConfigService(s).get_effective()
        assert eff["source"] == "env"
        assert eff["api_key"] == "sk-env"

    async def test_none_when_no_config(self) -> None:
        s = _session()
        s.execute.return_value.scalar_one_or_none.return_value = None
        with patch("app.services.llm.config_service.settings") as ms:
            ms.llm_base_url = ""
            ms.llm_api_key = ""
            eff = await LlmConfigService(s).get_effective()
        assert eff["source"] == "none"
        assert eff["api_key"] == ""

    async def test_env_fallback_when_db_disabled(self) -> None:
        s = _session()
        s.execute.return_value.scalar_one_or_none.return_value = _row(enabled=False)
        with patch("app.services.llm.config_service.settings") as ms:
            ms.llm_base_url = "https://api.env.com"
            ms.llm_api_key = "sk-env"
            eff = await LlmConfigService(s).get_effective()
        assert eff["source"] == "env"

    async def test_env_fallback_when_db_decrypt_fails(self) -> None:
        s = _session()
        s.execute.return_value.scalar_one_or_none.return_value = _row(api_key_enc="corrupt-token")
        with patch("app.services.llm.config_service.settings") as ms:
            ms.llm_base_url = "https://api.env.com"
            ms.llm_api_key = "sk-env"
            eff = await LlmConfigService(s).get_effective()
        assert eff["source"] == "env"


class TestSave:
    async def test_create_with_encrypted_key(self) -> None:
        s = _session()
        s.execute.return_value.scalar_one_or_none.return_value = None
        payload = LlmConfigPayload(
            provider="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-plain",
            timeout=60,
            enabled=True,
        )
        await LlmConfigService(s).save(payload, updated_by=7)
        added = s.add.call_args[0][0]
        assert isinstance(added, LlmConfig)
        assert added.provider == "openai"
        assert added.api_key_enc  # 已加密非明文
        assert "sk-plain" not in added.api_key_enc

    async def test_update_keeps_key_when_payload_empty(self) -> None:
        s = _session()
        existing = _row()
        existing.api_key_enc = "existing-encrypted-token"
        s.execute.return_value.scalar_one_or_none.return_value = existing
        payload = LlmConfigPayload(
            provider="deepseek",
            base_url="https://new.example.com",
            model="new-model",
            api_key="",
            timeout=30,
            enabled=True,
        )
        await LlmConfigService(s).save(payload, updated_by=2)
        assert existing.base_url == "https://new.example.com"
        assert existing.api_key_enc == "existing-encrypted-token"  # 未覆盖

    async def test_update_overwrites_key_when_payload_provided(self) -> None:
        s = _session()
        existing = _row()
        s.execute.return_value.scalar_one_or_none.return_value = existing
        payload = LlmConfigPayload(api_key="sk-new")
        await LlmConfigService(s).save(payload, updated_by=3)
        assert existing.api_key_enc != _row().api_key_enc  # 已更新


class TestBuildClient:
    async def test_build_from_db_config(self) -> None:
        s = _session()
        s.execute.return_value.scalar_one_or_none.return_value = _row()
        with patch("app.services.llm.config_service.LlmClient") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            client = await LlmConfigService(s).build_client()
        mock_client_cls.assert_called_once()
        assert client is mock_client_cls.return_value

    async def test_build_fallback_when_no_config(self) -> None:
        s = _session()
        s.execute.return_value.scalar_one_or_none.return_value = None
        with patch("app.services.llm.config_service.settings") as ms:
            ms.llm_base_url = ""
            ms.llm_api_key = ""
            client = await LlmConfigService(s).build_client()
        from app.services.llm.client import DeterministicFallbackLlmClient

        assert isinstance(client, DeterministicFallbackLlmClient)


class TestTestConnection:
    async def _svc(self) -> tuple[LlmConfigService, MagicMock]:
        s = _session()
        return LlmConfigService(s), s

    async def test_success(self) -> None:
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="https://api.example.com", api_key="sk-x", model="m1", timeout=30
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"model": "m1"}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is True
        assert result.model == "m1"

    async def test_http_error(self) -> None:
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="https://api.example.com", api_key="sk-x", model="m1", timeout=30
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "unauthorized"
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is False
        assert "401" in result.error

    async def test_network_error(self) -> None:
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="https://api.example.com", api_key="sk-x", model="m1", timeout=30
        )
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ConnectError("connection refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is False
        assert "connection refused" in result.error

    async def test_missing_config(self) -> None:
        svc, _ = await self._svc()
        payload = LlmConfigPayload(base_url="", api_key="", model="m1", timeout=30)
        result = await svc.test_connection(payload)
        assert result.ok is False
        assert "未配置" in result.error
