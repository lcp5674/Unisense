"""系统字典批量操作 API 契约测试（按 tab 批量新增/启停/删除，207 语义）。

覆盖（ASGI + 模拟 service 层，快速契约校验）：
- POST /dicts/{dict_type}/batch：合法 200；items 空 422；审计 action=dict.batch_create
- POST /dicts/{dict_type}/batch-status：activate→dict.batch_enable、deactivate→dict.batch_disable；
  非法 action / 非法编码 422
- POST /dicts/{dict_type}/batch-delete：合法 200；codes 空 422；审计 action=dict.batch_delete
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps, system_dict
from app.main import app
from app.services.system_dict.schemas import DictBatchItem, DictBatchResult

_RESULT_OK = DictBatchResult(
    succeeded=[DictBatchItem(code="minute", label="分钟", ok=True)],
    failed=[],
)


@pytest.fixture
async def dict_client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户依赖（platform_admin，多角色方案）。"""

    async def fake_db() -> AsyncIterator[MagicMock]:
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock())
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        domain=None,
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


def _install_svc(result: DictBatchResult) -> MagicMock:
    """覆盖 _get_service 依赖：service 方法 AsyncMock + _db.commit 可 await。"""
    fake_svc = MagicMock()
    fake_svc.batch_create_items = AsyncMock(return_value=result)
    fake_svc.batch_toggle_items = AsyncMock(return_value=result)
    fake_svc.batch_delete_items = AsyncMock(return_value=result)
    fake_svc._db = MagicMock()
    fake_svc._db.commit = AsyncMock()
    fake_svc._db.rollback = AsyncMock()
    app.dependency_overrides[system_dict._get_service] = lambda: fake_svc
    return fake_svc


# ---------------------------------------------------------------- batch（批量新增）


async def test_batch_create_success(dict_client: httpx.AsyncClient) -> None:
    """合法多行 → 200，返回 207 分桶结果，审计 action=dict.batch_create。"""
    fake_svc = _install_svc(_RESULT_OK)
    with patch("app.api.system_dict.write_audit", new=AsyncMock()) as audit:
        resp = await dict_client.post(
            "/api/v1/dicts/granularity/batch",
            json={
                "items": [
                    {"label": "分钟", "sort_order": 7},
                    {"label": "秒", "sort_order": 8},
                ]
            },
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["succeeded"]) == 1
    assert data["succeeded"][0]["code"] == "minute"
    assert len(data["failed"]) == 0
    fake_svc.batch_create_items.assert_awaited_once()
    kwargs = fake_svc.batch_create_items.await_args
    assert kwargs.args[0] == "granularity"
    assert len(kwargs.args[1]) == 2
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "dict.batch_create"
    assert audit.await_args.kwargs["detail"]["dict_type"] == "granularity"


async def test_batch_create_empty_items_422(dict_client: httpx.AsyncClient) -> None:
    """items 为空 → 422（min_length=1）。"""
    resp = await dict_client.post("/api/v1/dicts/granularity/batch", json={"items": []})
    assert resp.status_code == 422


async def test_batch_create_invalid_code_422(dict_client: httpx.AsyncClient) -> None:
    """含非法编码（如带横线）→ 422。"""
    resp = await dict_client.post(
        "/api/v1/dicts/granularity/batch",
        json={"items": [{"code": "bad-code", "label": "非法"}]},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------- batch-status（批量启停）


async def test_batch_status_activate_audit_enable(dict_client: httpx.AsyncClient) -> None:
    """action=activate → 200，审计 action=dict.batch_enable。"""
    fake_svc = _install_svc(_RESULT_OK)
    with patch("app.api.system_dict.write_audit", new=AsyncMock()) as audit:
        resp = await dict_client.post(
            "/api/v1/dicts/granularity/batch-status",
            json={"codes": ["minute", "second"], "action": "activate"},
        )
    assert resp.status_code == 200
    fake_svc.batch_toggle_items.assert_awaited_once()
    assert fake_svc.batch_toggle_items.await_args.args[1] == ["minute", "second"]
    assert fake_svc.batch_toggle_items.await_args.args[2] == "activate"
    assert audit.await_args.kwargs["action"] == "dict.batch_enable"


async def test_batch_status_deactivate_audit_disable(dict_client: httpx.AsyncClient) -> None:
    """action=deactivate → 200，审计 action=dict.batch_disable。"""
    _install_svc(_RESULT_OK)
    with patch("app.api.system_dict.write_audit", new=AsyncMock()) as audit:
        resp = await dict_client.post(
            "/api/v1/dicts/granularity/batch-status",
            json={"codes": ["minute"], "action": "deactivate"},
        )
    assert resp.status_code == 200
    assert audit.await_args.kwargs["action"] == "dict.batch_disable"
    assert audit.await_args.kwargs["detail"]["action"] == "deactivate"


async def test_batch_status_invalid_action_422(dict_client: httpx.AsyncClient) -> None:
    """非法 action → 422（Literal 枚举收严）。"""
    resp = await dict_client.post(
        "/api/v1/dicts/granularity/batch-status",
        json={"codes": ["minute"], "action": "freeze"},
    )
    assert resp.status_code == 422


async def test_batch_status_invalid_code_422(dict_client: httpx.AsyncClient) -> None:
    """含非法编码 → 422。"""
    resp = await dict_client.post(
        "/api/v1/dicts/granularity/batch-status",
        json={"codes": ["minute", "bad-code"], "action": "activate"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------- batch-delete（批量删除）


async def test_batch_delete_success(dict_client: httpx.AsyncClient) -> None:
    """合法编码列表 → 200，审计 action=dict.batch_delete。"""
    fake_svc = _install_svc(_RESULT_OK)
    with patch("app.api.system_dict.write_audit", new=AsyncMock()) as audit:
        resp = await dict_client.post(
            "/api/v1/dicts/unit/batch-delete",
            json={"codes": ["CNY", "USD"]},
        )
    assert resp.status_code == 200
    fake_svc.batch_delete_items.assert_awaited_once()
    assert fake_svc.batch_delete_items.await_args.args[1] == ["CNY", "USD"]
    assert audit.await_args.kwargs["action"] == "dict.batch_delete"
    assert audit.await_args.kwargs["detail"]["dict_type"] == "unit"


async def test_batch_delete_empty_codes_422(dict_client: httpx.AsyncClient) -> None:
    """codes 为空 → 422（min_length=1）。"""
    resp = await dict_client.post("/api/v1/dicts/unit/batch-delete", json={"codes": []})
    assert resp.status_code == 422


# ---------------------------------------------------------------- infer-description（描述 LLM）


class _FakeLlmService:
    """模拟 LlmConfigService：build_client 返回可配置的 client。"""

    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def build_client(self) -> MagicMock:
        return self._client


def _fake_llm_client(enabled: bool = True, content: str = "该取值的含义与用途") -> MagicMock:
    client = MagicMock()
    client.enabled = enabled
    client.chat = AsyncMock(return_value={"content": content})
    return client


def _install_llm(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    monkeypatch.setattr(
        "app.services.llm.config_service.LlmConfigService",
        lambda db: _FakeLlmService(client),
    )


async def test_infer_description_success(
    dict_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """合法显示名 → 200 返回 LLM 描述，纯文本调用（text），审计 dict.infer_description。"""
    client = _fake_llm_client()
    _install_llm(monkeypatch, client)
    with patch("app.api.system_dict.write_audit", new=AsyncMock()) as audit:
        resp = await dict_client.post(
            "/api/v1/dicts/infer-description",
            json={"dict_type": "unit", "label": "元", "dict_type_label": "单位"},
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["description"] == "该取值的含义与用途"
    client.chat.assert_awaited_once()
    kwargs = client.chat.await_args.kwargs
    # 描述是纯文本：显式 text 避免被 chat 缺省 json_object 约束污染为空 JSON
    assert kwargs["response_format"] == {"type": "text"}
    prompt = kwargs["messages"][0]["content"]
    assert "元" in prompt
    assert "单位" in prompt
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "dict.infer_description"
    assert audit.await_args.kwargs["entity_id"] == "unit:元"


async def test_infer_description_llm_disabled(
    dict_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM 不可用（enabled=False）→ 400 LLM_INFER_UNAVAILABLE。"""
    _install_llm(monkeypatch, _fake_llm_client(enabled=False))
    resp = await dict_client.post(
        "/api/v1/dicts/infer-description",
        json={"dict_type": "unit", "label": "元"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "LLM_INFER_UNAVAILABLE"


async def test_infer_description_empty_content(
    dict_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM 返回空内容 → 400 LLM_INFER_UNAVAILABLE。"""
    _install_llm(monkeypatch, _fake_llm_client(content=""))
    resp = await dict_client.post(
        "/api/v1/dicts/infer-description",
        json={"dict_type": "unit", "label": "元"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "LLM_INFER_UNAVAILABLE"


async def test_infer_description_invalid_422(dict_client: httpx.AsyncClient) -> None:
    """label 为空 → 422（min_length=1）。"""
    resp = await dict_client.post(
        "/api/v1/dicts/infer-description",
        json={"dict_type": "unit", "label": ""},
    )
    assert resp.status_code == 422
