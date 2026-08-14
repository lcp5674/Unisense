"""LLM 配置服务单测（多实例 CRUD / DB 优先 / env 兜底 / 加密落库 / 路由构建 / 连通性测试）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.models.llm_config import LlmConfig
from app.services.llm.config_service import LlmConfigService
from app.services.llm.schemas import LlmConfigPayload


def _session(rows: list[LlmConfig] | None = None) -> MagicMock:
    s = MagicMock()
    s.flush = AsyncMock()
    s.add = MagicMock()
    # 显式让 execute 返回普通 MagicMock（AsyncMock.return_value 默认也是 AsyncMock，
    # 会导致 scalar_one_or_none / scalars().all() 返回 coroutine）
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.all.return_value = list(rows or [])
    s.execute = AsyncMock(return_value=result)
    return s


def _row(**overrides: object) -> LlmConfig:
    from app.core.secrets import SecretManager

    cfg = {
        "id": 1,
        "name": "主用",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key_enc": SecretManager.encrypt({"api_key": "sk-test"}),
        "timeout": 30,
        "enabled": True,
        "priority": 0,
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
        s = _session([_row()])
        eff = await LlmConfigService(s).get_effective()
        assert eff["source"] == "db"
        assert eff["base_url"] == "https://api.deepseek.com"
        assert eff["api_key"] == "sk-test"
        assert eff["provider"] == "deepseek"
        assert eff["name"] == "主用"

    async def test_priority_ordering_picks_primary(self) -> None:
        """多实例时 get_effective 应返回列表首个启用实例（SQL 已按 priority 排序）。"""
        # list_configs 按 priority 升序返回（ORDER BY 由 SQL 保证），此处按该顺序传入
        s = _session(
            [_row(id=1, priority=0), _row(id=2, priority=1, base_url="https://backup.com")]
        )
        eff = await LlmConfigService(s).get_effective()
        assert eff["base_url"] == "https://api.deepseek.com"  # priority=0 优先

    async def test_env_fallback_when_no_db_row(self) -> None:
        s = _session([])
        with patch("app.services.llm.config_service.settings") as ms:
            ms.llm_base_url = "https://api.env.com"
            ms.llm_api_key = "sk-env"
            ms.llm_default_model = "env-model"
            eff = await LlmConfigService(s).get_effective()
        assert eff["source"] == "env"
        assert eff["api_key"] == "sk-env"

    async def test_none_when_no_config(self) -> None:
        s = _session([])
        with patch("app.services.llm.config_service.settings") as ms:
            ms.llm_base_url = ""
            ms.llm_api_key = ""
            eff = await LlmConfigService(s).get_effective()
        assert eff["source"] == "none"
        assert eff["api_key"] == ""

    async def test_env_fallback_when_db_disabled(self) -> None:
        s = _session([_row(enabled=False)])
        with patch("app.services.llm.config_service.settings") as ms:
            ms.llm_base_url = "https://api.env.com"
            ms.llm_api_key = "sk-env"
            eff = await LlmConfigService(s).get_effective()
        assert eff["source"] == "env"

    async def test_env_fallback_when_db_decrypt_fails(self) -> None:
        s = _session([_row(api_key_enc="corrupt-token")])
        with patch("app.services.llm.config_service.settings") as ms:
            ms.llm_base_url = "https://api.env.com"
            ms.llm_api_key = "sk-env"
            eff = await LlmConfigService(s).get_effective()
        assert eff["source"] == "env"


class TestGetSecret:
    async def test_decrypt_returns_plaintext(self) -> None:
        s = _session()
        s.execute.return_value.scalar_one_or_none.return_value = _row()
        secret = await LlmConfigService(s).get_secret(1)
        assert secret == "sk-test"

    async def test_missing_row_returns_none(self) -> None:
        s = _session()
        s.execute.return_value.scalar_one_or_none.return_value = None
        assert await LlmConfigService(s).get_secret(99) is None

    async def test_no_key_returns_none(self) -> None:
        s = _session()
        s.execute.return_value.scalar_one_or_none.return_value = _row(api_key_enc="")
        assert await LlmConfigService(s).get_secret(1) is None

    async def test_decrypt_failure_returns_none(self) -> None:
        s = _session()
        s.execute.return_value.scalar_one_or_none.return_value = _row(api_key_enc="corrupt-token")
        assert await LlmConfigService(s).get_secret(1) is None


class TestCreateUpdateDelete:
    async def test_create_with_encrypted_key(self) -> None:
        s = _session([])
        payload = LlmConfigPayload(
            name="备用",
            provider="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-plain",
            timeout=60,
            enabled=True,
            priority=1,
        )
        await LlmConfigService(s).create(payload, updated_by=7)
        added = s.add.call_args[0][0]
        assert isinstance(added, LlmConfig)
        assert added.provider == "openai"
        assert added.name == "备用"
        assert added.priority == 1
        assert added.api_key_enc  # 已加密非明文
        assert "sk-plain" not in added.api_key_enc

    async def test_update_keeps_key_when_payload_empty(self) -> None:
        s = _session()
        existing = _row()
        existing.api_key_enc = "existing-encrypted-token"
        s.execute.return_value.scalar_one_or_none.return_value = existing
        payload = LlmConfigPayload(
            name="改",
            provider="deepseek",
            base_url="https://new.example.com",
            model="new-model",
            api_key="",
            timeout=30,
            enabled=True,
        )
        await LlmConfigService(s).update(1, payload, updated_by=2)
        assert existing.base_url == "https://new.example.com"
        assert existing.api_key_enc == "existing-encrypted-token"  # 未覆盖
        assert existing.name == "改"

    async def test_update_overwrites_key_when_payload_provided(self) -> None:
        s = _session()
        existing = _row()
        s.execute.return_value.scalar_one_or_none.return_value = existing
        payload = LlmConfigPayload(api_key="sk-new")
        await LlmConfigService(s).update(1, payload, updated_by=3)
        assert existing.api_key_enc != _row().api_key_enc  # 已更新

    async def test_update_returns_none_when_missing(self) -> None:
        s = _session()
        s.execute.return_value.scalar_one_or_none.return_value = None
        payload = LlmConfigPayload()
        result = await LlmConfigService(s).update(99, payload, updated_by=1)
        assert result is None

    async def test_delete_soft(self) -> None:
        s = _session()
        existing = _row()
        s.execute.return_value.scalar_one_or_none.return_value = existing
        deleted = await LlmConfigService(s).delete(1)
        assert deleted is True
        assert existing.deleted_at is not None

    async def test_delete_returns_false_when_missing(self) -> None:
        s = _session()
        s.execute.return_value.scalar_one_or_none.return_value = None
        assert await LlmConfigService(s).delete(99) is False

    async def test_list_configs_ordered_by_priority(self) -> None:
        s = _session([_row(id=2, priority=1), _row(id=1, priority=0)])
        rows = await LlmConfigService(s).list_configs()
        assert len(rows) == 2


class TestBuildClient:
    async def test_build_router_when_multiple_enabled(self) -> None:
        s = _session(
            [
                _row(id=1, priority=0, base_url="https://a.com"),
                _row(id=2, priority=1, base_url="https://b.com"),
            ]
        )
        with patch("app.services.llm.config_service.settings") as ms:
            ms.llm_base_url = ""
            ms.llm_api_key = ""
            client = await LlmConfigService(s).build_client()
        from app.services.llm.client import LlmRouterClient

        assert isinstance(client, LlmRouterClient)
        assert client.instance_count == 2

    async def test_build_single_client_when_one_enabled(self) -> None:
        s = _session([_row()])
        with patch("app.services.llm.config_service.settings") as ms:
            ms.llm_base_url = ""
            ms.llm_api_key = ""
            client = await LlmConfigService(s).build_client()
        from app.services.llm.client import LlmClient

        assert isinstance(client, LlmClient)

    async def test_build_fallback_when_no_config(self) -> None:
        s = _session([])
        with patch("app.services.llm.config_service.settings") as ms:
            ms.llm_base_url = ""
            ms.llm_api_key = ""
            client = await LlmConfigService(s).build_client()
        from app.services.llm.client import DeterministicFallbackLlmClient

        assert isinstance(client, DeterministicFallbackLlmClient)

    async def test_build_env_fallback_participates_in_router(self) -> None:
        """DB 无实例但 env 已配置时，build_client 应返回单实例（env）而非降级。"""
        s = _session([])
        with patch("app.services.llm.config_service.settings") as ms:
            ms.llm_base_url = "https://api.env.com"
            ms.llm_api_key = "sk-env"
            client = await LlmConfigService(s).build_client()
        from app.services.llm.client import LlmClient

        assert isinstance(client, LlmClient)
        assert client.enabled is True


class TestTestConnection:
    async def _svc(self) -> tuple[LlmConfigService, MagicMock]:
        s = _session([])
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

    async def test_connect_error_with_loopback_base_url_hints_host_docker(self) -> None:
        """回环地址（127.0.0.1）ConnectError 时，应提示容器场景改用 host.docker.internal。"""
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="http://127.0.0.1:19090/v1/chat/completions",
            api_key="sk-x",
            model="m1",
            timeout=30,
        )
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ConnectError("connection refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is False
        assert "host.docker.internal" in result.error

    async def test_connect_error_with_public_base_url_no_hint(self) -> None:
        """非回环地址 ConnectError 不附加容器提示（避免误导公网地址用户）。"""
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="https://api.deepseek.com", api_key="sk-x", model="m1", timeout=30
        )
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ConnectError("connection refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is False
        assert "host.docker.internal" not in result.error

    async def test_missing_config(self) -> None:
        svc, _ = await self._svc()
        payload = LlmConfigPayload(base_url="", api_key="", model="m1", timeout=30)
        result = await svc.test_connection(payload)
        assert result.ok is False
        assert "未配置" in result.error

    async def test_empty_api_key_falls_back_to_saved_key(self) -> None:
        """前端表单 api_key 留空（保持原密钥）时，测试应回落已保存密钥。"""
        svc, s = await self._svc()
        s.execute.return_value.scalars.return_value.all.return_value = [_row()]
        payload = LlmConfigPayload(
            base_url="https://api.deepseek.com", api_key="", model="deepseek-chat", timeout=30
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"model": "deepseek-chat"}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is True  # 回落已保存密钥才可能成功

    async def test_base_url_with_v1_suffix_uses_normalized_endpoint(self) -> None:
        """openai 预设（base_url 含 /v1）测试连通性时，端点不得拼成 /v1/v1（回归 404）。"""
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="https://api.openai.com/v1",
            api_key="sk-x",
            model="gpt-4o-mini",
            timeout=30,
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"model": "gpt-4o-mini"}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is True
        called_url = mock_client.post.call_args[0][0]
        assert called_url == "https://api.openai.com/v1/chat/completions"
        assert "/v1/v1/" not in called_url

    async def test_test_instance_decrypt_and_probe(self) -> None:
        svc, s = await self._svc()
        s.execute.return_value.scalar_one_or_none.return_value = _row()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"model": "deepseek-chat"}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_instance(1)
        assert result.ok is True

    async def test_test_instance_missing(self) -> None:
        svc, s = await self._svc()
        s.execute.return_value.scalar_one_or_none.return_value = None
        result = await svc.test_instance(99)
        assert result.ok is False
        assert "不存在" in result.error
