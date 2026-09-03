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
        # base_url 归一化：openai 预设（含 /v1）落库为干净裸 URL
        assert added.base_url == "https://api.openai.com"

    async def test_create_persists_disable_thinking(self) -> None:
        """create 把 disable_thinking 开关写入实例（本地 Qwen3 关思考，防 token 被思考耗尽）。"""
        s = _session([])
        payload = LlmConfigPayload(
            name="本地 Qwen3",
            provider="custom",
            base_url="http://host.docker.internal:8082",
            model="qwen3-30b-a3b",
            api_key="local",
            timeout=120,
            enabled=True,
            disable_thinking=True,
        )
        await LlmConfigService(s).create(payload, updated_by=7)
        added = s.add.call_args[0][0]
        assert isinstance(added, LlmConfig)
        assert added.disable_thinking is True

    async def test_update_persists_disable_thinking(self) -> None:
        """update 更新 disable_thinking 开关（编辑开启/关闭思考模式生效）。"""
        s = _session()
        existing = _row()
        s.execute.return_value.scalar_one_or_none.return_value = existing
        payload = LlmConfigPayload(
            name="改",
            provider="custom",
            base_url="https://new.example.com",
            model="m1",
            api_key="",
            timeout=30,
            enabled=True,
            disable_thinking=True,
        )
        await LlmConfigService(s).update(1, payload, updated_by=2)
        assert existing.disable_thinking is True

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

    async def test_update_normalizes_full_url(self) -> None:
        """编辑时输入完整 chat/completions 端点，落库应归一化为干净 base_url（与 create 对称）。"""
        s = _session()
        existing = _row()
        s.execute.return_value.scalar_one_or_none.return_value = existing
        payload = LlmConfigPayload(
            name="改",
            provider="custom",
            base_url="http://host.docker.internal:19090/v1/chat/completions",
            model="hy3",
            api_key="",
            timeout=30,
            enabled=True,
        )
        await LlmConfigService(s).update(1, payload, updated_by=2)
        assert existing.base_url == "http://host.docker.internal:19090"
        # 裸 URL 幂等，不破坏原有干净配置
        existing.base_url = "https://api.deepseek.com"
        payload2 = LlmConfigPayload(base_url="https://api.deepseek.com/")
        await LlmConfigService(s).update(1, payload2, updated_by=3)
        assert existing.base_url == "https://api.deepseek.com"

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

    async def test_build_single_client_passes_disable_thinking(self) -> None:
        """build_client 把 disable_thinking 透传给 LlmClient（chat 附加 enable_thinking=false）。"""
        s = _session([_row(disable_thinking=True)])
        with patch("app.services.llm.config_service.settings") as ms:
            ms.llm_base_url = ""
            ms.llm_api_key = ""
            client = await LlmConfigService(s).build_client()
        from app.services.llm.client import LlmClient

        assert isinstance(client, LlmClient)
        assert client._disable_thinking is True

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
        mock_client = AsyncMock()
        mock_models_resp = MagicMock()
        mock_models_resp.status_code = 200
        mock_models_resp.json.return_value = {"data": [{"id": "m1"}, {"id": "m2"}]}
        mock_chat_resp = MagicMock()
        mock_chat_resp.status_code = 200
        mock_chat_resp.json.return_value = {"choices": [{"message": {"content": "pong"}}]}
        mock_client.get.return_value = mock_models_resp
        mock_client.post.return_value = mock_chat_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is True
        assert result.model == "m1"
        assert result.models == ["m1", "m2"]
        assert result.chat is True
        assert result.models_supported is True
        # 方案 B'：两步探测——GET /models 之后真实 POST 极小 chat 验证可推理
        mock_client.post.assert_awaited_once()
        post_body = mock_client.post.call_args.kwargs["json"]
        assert post_body["max_tokens"] <= 5
        assert "response_format" not in post_body

    async def test_http_error(self) -> None:
        """GET /models 返回 401 → 鉴权失败（毫秒级，不触发真实推理）。"""
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="https://api.example.com", api_key="sk-x", model="m1", timeout=30
        )
        mock_client = AsyncMock()
        mock_models_resp = MagicMock()
        mock_models_resp.status_code = 401
        mock_models_resp.text = "unauthorized"
        mock_client.get.return_value = mock_models_resp
        mock_client.post = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is False
        assert "401" in result.error
        mock_client.post.assert_not_awaited()

    async def test_network_error(self) -> None:
        """GET /models 抛 ConnectError → 快速失败，不触发真实推理。"""
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="https://api.example.com", api_key="sk-x", model="m1", timeout=30
        )
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("connection refused")
        mock_client.post = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is False
        assert "connection refused" in result.error
        mock_client.post.assert_not_awaited()

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
        mock_client.get.side_effect = httpx.ConnectError("connection refused")
        mock_client.post = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is False
        assert "host.docker.internal" in result.error
        mock_client.post.assert_not_awaited()

    async def test_connect_error_with_public_base_url_no_hint(self) -> None:
        """非回环地址 ConnectError 不附加容器提示（避免误导公网地址用户）。"""
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="https://api.deepseek.com", api_key="sk-x", model="m1", timeout=30
        )
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("connection refused")
        mock_client.post = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is False
        assert "host.docker.internal" not in result.error
        mock_client.post.assert_not_awaited()

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
        mock_resp.json.return_value = {"choices": [{"message": {"content": "pong"}}]}
        mock_client = AsyncMock()
        mock_models_resp = MagicMock()
        mock_models_resp.status_code = 200
        mock_client.get.return_value = mock_models_resp
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is True  # 回落已保存密钥才可能成功

    async def test_base_url_with_v1_suffix_uses_normalized_endpoint(self) -> None:
        """openai 预设（base_url 含 /v1）时 GET /models 端点不得拼成 /v1/v1（回归 404）。"""
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="https://api.openai.com/v1",
            api_key="sk-x",
            model="gpt-4o-mini",
            timeout=30,
        )
        mock_client = AsyncMock()
        mock_models_resp = MagicMock()
        mock_models_resp.status_code = 200
        mock_models_resp.json.return_value = {"data": [{"id": "gpt-4o-mini"}]}
        mock_chat_resp = MagicMock()
        mock_chat_resp.status_code = 200
        mock_chat_resp.json.return_value = {"choices": [{"message": {"content": "pong"}}]}
        mock_client.get.return_value = mock_models_resp
        mock_client.post.return_value = mock_chat_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is True
        called_url = mock_client.get.call_args[0][0]
        assert called_url == "https://api.openai.com/v1/models"
        assert "/v1/v1/" not in called_url
        # 方案 B'：真实 chat 探测也走正确端点（不得拼成 /v1/v1/chat/completions）
        mock_client.post.assert_awaited_once()
        chat_url = mock_client.post.call_args[0][0]
        assert chat_url == "https://api.openai.com/v1/chat/completions"
        assert "/v1/v1/" not in chat_url

    async def test_test_instance_decrypt_and_probe(self) -> None:
        svc, s = await self._svc()
        s.execute.return_value.scalar_one_or_none.return_value = _row()
        mock_client = AsyncMock()
        mock_models_resp = MagicMock()
        mock_models_resp.status_code = 200
        mock_models_resp.json.return_value = {"data": [{"id": "deepseek-chat"}]}
        mock_chat_resp = MagicMock()
        mock_chat_resp.status_code = 200
        mock_chat_resp.json.return_value = {"choices": [{"message": {"content": "pong"}}]}
        mock_client.get.return_value = mock_models_resp
        mock_client.post.return_value = mock_chat_resp
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

    async def test_chat_probe_http_failure_marks_unavailable(self) -> None:
        """GET /models 通过但真实 chat 探测返回 4xx（如 400 Model unavailable）→ 判不可推理。"""
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="https://api.example.com", api_key="sk-x", model="m1", timeout=30
        )
        mock_client = AsyncMock()
        mock_models_resp = MagicMock()
        mock_models_resp.status_code = 200
        mock_models_resp.json.return_value = {"data": [{"id": "m1"}]}
        mock_chat_resp = MagicMock()
        mock_chat_resp.status_code = 400
        mock_chat_resp.text = '{"error":"Model is unavailable"}'
        mock_client.get.return_value = mock_models_resp
        mock_client.post.return_value = mock_chat_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is False
        assert result.chat is False
        assert "真实推理失败" in result.error
        assert "400" in result.error
        # 第一步（GET /models）通过，仍带回模型列表供排查
        assert result.models == ["m1"]
        mock_client.post.assert_awaited_once()

    async def test_chat_probe_network_error_marks_unavailable(self) -> None:
        """GET /models 通过但 chat 探测网络错误（如超时）→ 判不可推理。"""
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="https://api.example.com", api_key="sk-x", model="m1", timeout=30
        )
        mock_client = AsyncMock()
        mock_models_resp = MagicMock()
        mock_models_resp.status_code = 200
        mock_models_resp.json.return_value = {"data": [{"id": "m1"}]}
        mock_client.get.return_value = mock_models_resp
        mock_client.post.side_effect = httpx.ConnectError("timeout")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is False
        assert result.chat is False
        assert "真实推理失败" in result.error
        mock_client.post.assert_awaited_once()


class TestFetchModels:
    """一键获取模型列表（fetch_models / fetch_models_for_instance）。"""

    async def _svc(self) -> tuple[LlmConfigService, MagicMock]:
        s = _session([])
        return LlmConfigService(s), s

    async def test_fetch_models_ok(self) -> None:
        svc, _ = await self._svc()
        models_resp = MagicMock()
        models_resp.status_code = 200
        models_resp.json.return_value = {
            "data": [{"id": "m1"}, {"id": "m2"}, {"id": ""}, {"name": "no-id"}]
        }
        mock_client = AsyncMock()
        mock_client.get.return_value = models_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.fetch_models("https://api.deepseek.com", "sk-x", 30)
        assert result.supported is True
        assert result.models == ["m1", "m2"]  # 空/缺 id 的条目被过滤
        assert result.error == ""

    async def test_fetch_models_unsupported_returns_false(self) -> None:
        """网关不支持 /models（404）→ supported=False + 错误信息，不判为连通失败。"""
        svc, _ = await self._svc()
        models_resp = MagicMock()
        models_resp.status_code = 404
        models_resp.text = "not found"
        mock_client = AsyncMock()
        mock_client.get.return_value = models_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.fetch_models("https://api.example.com", "sk-x", 30)
        assert result.supported is False
        assert "404" in result.error

    async def test_fetch_models_network_error_with_loopback_hint(self) -> None:
        svc, _ = await self._svc()
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("conn refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.fetch_models("http://127.0.0.1:19090", "sk-x", 30)
        assert result.supported is False
        assert "conn refused" in result.error
        assert "host.docker.internal" in result.error  # 回环自诊断提示

    async def test_fetch_models_empty_api_key_falls_back_to_saved(self) -> None:
        svc, s = await self._svc()
        s.execute.return_value.scalars.return_value.all.return_value = [_row()]
        models_resp = MagicMock()
        models_resp.status_code = 200
        models_resp.json.return_value = {"data": [{"id": "deepseek-chat"}]}
        mock_client = AsyncMock()
        mock_client.get.return_value = models_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.fetch_models("https://api.deepseek.com", "", 30)
        assert result.supported is True
        assert result.models == ["deepseek-chat"]

    async def test_fetch_models_missing_base_url(self) -> None:
        svc, _ = await self._svc()
        result = await svc.fetch_models("", "sk-x", 30)
        assert result.supported is False
        assert "base_url" in result.error

    async def test_fetch_models_for_instance(self) -> None:
        svc, s = await self._svc()
        s.execute.return_value.scalar_one_or_none.return_value = _row()
        models_resp = MagicMock()
        models_resp.status_code = 200
        models_resp.json.return_value = {"data": [{"id": "deepseek-chat"}]}
        mock_client = AsyncMock()
        mock_client.get.return_value = models_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.fetch_models_for_instance(1)
        assert result.supported is True
        assert result.models == ["deepseek-chat"]

    async def test_fetch_models_for_instance_missing(self) -> None:
        svc, s = await self._svc()
        s.execute.return_value.scalar_one_or_none.return_value = None
        result = await svc.fetch_models_for_instance(99)
        assert result.supported is False
        assert "不存在" in result.error

    async def test_fetch_models_catalog_fallback_ark(self) -> None:
        """火山方舟兼容网关不支持 /models（404）→ 回退内置常用模型目录（source=catalog）。"""
        from app.services.llm.config_service import PROVIDER_MODEL_CATALOG

        svc, _ = await self._svc()
        models_resp = MagicMock()
        models_resp.status_code = 404
        models_resp.text = "not found"
        mock_client = AsyncMock()
        mock_client.get.return_value = models_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.fetch_models(
                "https://ark.cn-beijing.volces.com/api/coding/v3", "sk-x", 30, provider="ark"
            )
        assert result.supported is True
        assert result.source == "catalog"
        assert result.models == PROVIDER_MODEL_CATALOG["ark"]
        assert result.error == ""
        assert "不支持 GET /models" in result.note

    async def test_fetch_models_catalog_fallback_tencent(self) -> None:
        """腾讯云混元兼容网关不支持 /models（404）→ 回退内置常用模型目录。"""
        from app.services.llm.config_service import PROVIDER_MODEL_CATALOG

        svc, _ = await self._svc()
        models_resp = MagicMock()
        models_resp.status_code = 404
        models_resp.text = "not found"
        mock_client = AsyncMock()
        mock_client.get.return_value = models_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.fetch_models(
                "https://api.hunyuan.cloud.tencent.com/v1", "sk-x", 30, provider="tencent"
            )
        assert result.supported is True
        assert result.source == "catalog"
        assert result.models == PROVIDER_MODEL_CATALOG["tencent"]

    async def test_fetch_models_custom_no_catalog_fallback(self) -> None:
        """未知/自定义 provider 的网关不支持 /models（404）→ 保持不支持，不套目录。"""
        svc, _ = await self._svc()
        models_resp = MagicMock()
        models_resp.status_code = 404
        models_resp.text = "not found"
        mock_client = AsyncMock()
        mock_client.get.return_value = models_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.fetch_models(
                "https://api.example.com", "sk-x", 30, provider="custom"
            )
        assert result.supported is False
        assert result.source == "live"
        assert result.models == []

    async def test_fetch_models_live_does_not_mix_catalog(self) -> None:
        """网关支持 /models（200）时即使 provider 有目录也返回实时结果（source=live）。"""
        svc, _ = await self._svc()
        models_resp = MagicMock()
        models_resp.status_code = 200
        models_resp.json.return_value = {"data": [{"id": "real-model"}]}
        mock_client = AsyncMock()
        mock_client.get.return_value = models_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.fetch_models(
                "https://ark.cn-beijing.volces.com/api/coding/v3", "sk-x", 30, provider="ark"
            )
        assert result.supported is True
        assert result.source == "live"
        assert result.models == ["real-model"]

    async def test_fetch_models_auth_error_no_catalog(self) -> None:
        """鉴权失败（401）即使 provider 有目录也不回退——目录与鉴权无关，须报错提示。"""
        svc, _ = await self._svc()
        models_resp = MagicMock()
        models_resp.status_code = 401
        models_resp.text = "unauthorized"
        mock_client = AsyncMock()
        mock_client.get.return_value = models_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.fetch_models(
                "https://ark.cn-beijing.volces.com/api/coding/v3", "sk-bad", 30, provider="ark"
            )
        assert result.supported is False
        assert result.source == "live"
        assert result.models == []
        assert "401" in result.error

    async def test_fetch_models_for_instance_ark_catalog(self) -> None:
        """已保存实例 provider=ark 且网关 404 → 回退目录（provider 从落库行读取）。"""
        from app.services.llm.config_service import PROVIDER_MODEL_CATALOG

        svc, s = await self._svc()
        s.execute.return_value.scalar_one_or_none.return_value = _row(
            provider="ark", base_url="https://ark.cn-beijing.volces.com/api/coding/v3"
        )
        models_resp = MagicMock()
        models_resp.status_code = 404
        models_resp.text = "not found"
        mock_client = AsyncMock()
        mock_client.get.return_value = models_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.fetch_models_for_instance(1)
        assert result.supported is True
        assert result.source == "catalog"
        assert result.models == PROVIDER_MODEL_CATALOG["ark"]


class TestQuickProbe:
    """两步探测的第一步（GET /models）快速失败路径：

    连通失败/鉴权失败/网关 5xx 毫秒级短路返回，不进入真实 chat 探测；网关未实现
    /models（404/405/501，如火山方舟/腾讯混元兼容网关）时回落第二步 chat 探测——
    该端点可选，真实推理才是连通的最终判据（方案 B' 增强）。
    """

    async def _svc(self) -> tuple[LlmConfigService, MagicMock]:
        s = _session([])
        return LlmConfigService(s), s

    async def test_auth_failure_returns_immediately_without_post(self) -> None:
        """GET /models 返回 401 → 立即失败，不应再触发真实推理（POST 不应被调用）。"""
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="https://api.example.com", api_key="sk-bad", model="m1", timeout=30
        )
        models_resp = MagicMock()
        models_resp.status_code = 401
        models_resp.text = "invalid key"
        mock_client = AsyncMock()
        mock_client.get.return_value = models_resp
        mock_client.post = AsyncMock()  # 快速失败路径不应调用真实推理
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is False
        assert "鉴权失败" in result.error
        assert "401" in result.error
        mock_client.post.assert_not_awaited()

    async def test_connect_error_returns_fast(self) -> None:
        """GET /models 抛 ConnectError → 快速失败，POST 不被调用（无需等真实推理超时）。"""
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="http://127.0.0.1:19090", api_key="sk-x", model="m1", timeout=30
        )
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("conn refused")
        mock_client.post = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is False
        assert "conn refused" in result.error
        assert "host.docker.internal" in result.error
        mock_client.post.assert_not_awaited()

    async def test_unsupported_models_falls_back_to_chat_probe_success(self) -> None:
        """GET /models 返回 404（网关未实现，如火山方舟/腾讯混元）→ 回落真实 chat 探测。

        chat 探测通过则整体判连通成功（models_supported=False 标记 /models 不可用，
        models 为空），不再误报「连通失败」。
        """
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
            api_key="sk-x",
            model="deepseek-v3.1",
            timeout=30,
        )
        models_resp = MagicMock()
        models_resp.status_code = 404
        models_resp.text = "not found"
        chat_resp = MagicMock()
        chat_resp.status_code = 200
        chat_resp.json.return_value = {"choices": [{"message": {"content": "pong"}}]}
        mock_client = AsyncMock()
        mock_client.get.return_value = models_resp
        mock_client.post.return_value = chat_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is True
        assert result.chat is True
        assert result.models_supported is False
        assert result.models == []
        mock_client.post.assert_awaited_once()
        # chat 探测仍走正确的 OpenAI 兼容端点（/v1/chat/completions）
        chat_url = mock_client.post.call_args[0][0]
        assert chat_url.endswith("/chat/completions")

    async def test_unsupported_models_chat_failure_returns_clear_error(self) -> None:
        """GET /models 404 且 chat 探测失败（如 400 Model unavailable）→ 明确报错。

        网关可达但无 /models 端点，且模型不可推理——错误信息须同时说明两点，
        避免误导为「鉴权失败」（/models 不可用时无法验证鉴权）。
        """
        svc, _ = await self._svc()
        payload = LlmConfigPayload(
            base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
            api_key="sk-x",
            model="deepseek-v3.1",
            timeout=30,
        )
        models_resp = MagicMock()
        models_resp.status_code = 404
        models_resp.text = "not found"
        chat_resp = MagicMock()
        chat_resp.status_code = 400
        chat_resp.text = '{"error":"Model is unavailable"}'
        mock_client = AsyncMock()
        mock_client.get.return_value = models_resp
        mock_client.post.return_value = chat_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection(payload)
        assert result.ok is False
        assert result.chat is False
        assert result.models_supported is False
        assert "未实现 GET /models" in result.error
        assert "真实推理失败" in result.error
        assert "400" in result.error
        mock_client.post.assert_awaited_once()
