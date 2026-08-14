"""LLM 字段描述推断单测（对齐 DEV_GUIDE §8b / gateways unit）。

覆盖：正常推断、LLM 不可用降级、超时降级、格式错误降级。
无外部依赖（mock LlmClient，验证降级不阻断主流程）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.collector.service import CollectorService
from app.services.llm.client import LlmError


def _svc() -> tuple[CollectorService, MagicMock]:
    """构造服务并替换其仓库为 mock，返回 (service, mock_repo_instance)。"""
    with patch("app.services.collector.service.CollectorRepository") as mock_repo:
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.flush = AsyncMock()
        svc = CollectorService(db=db)
        repo = mock_repo.return_value
        return svc, repo


@pytest.mark.asyncio
async def test_llm_infer_column_description_normal():
    """正常推断：LLM 返回有效描述，返回结构化结果。"""
    svc, _ = _svc()

    mock_client = AsyncMock()
    mock_client.enabled = True
    mock_client.chat = AsyncMock(return_value={"description": "用户唯一标识ID", "confidence": 0.85})
    mock_client.close = AsyncMock()

    with patch("app.services.llm.client.build_llm_client", return_value=mock_client):
        result = await svc._llm_infer_column_description(
            entity_name="dwd_order",
            column_name="user_id",
            column_type="bigint",
        )

    assert result is not None
    assert result["description"] == "用户唯一标识ID"
    assert result["confidence"] == 0.85
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_infer_column_description_disabled():
    """LLM 不可用（enabled=False）：返回 None，不阻断。"""
    svc, _ = _svc()

    mock_client = AsyncMock()
    mock_client.enabled = False
    mock_client.close = AsyncMock()

    with patch("app.services.llm.client.build_llm_client", return_value=mock_client):
        result = await svc._llm_infer_column_description(
            entity_name="dwd_order",
            column_name="user_id",
        )

    assert result is None
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_infer_column_description_timeout():
    """LLM 超时：返回 None，降级不阻断。"""
    svc, _ = _svc()

    mock_client = AsyncMock()
    mock_client.enabled = True
    mock_client.chat = AsyncMock(side_effect=TimeoutError("LLM timeout"))
    mock_client.close = AsyncMock()

    with patch("app.services.llm.client.build_llm_client", return_value=mock_client):
        result = await svc._llm_infer_column_description(
            entity_name="dwd_order",
            column_name="user_id",
        )

    assert result is None
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_infer_column_description_format_error():
    """LLM 返回格式错误（ValueError）：返回 None，降级不阻断。"""
    svc, _ = _svc()

    mock_client = AsyncMock()
    mock_client.enabled = True
    mock_client.chat = AsyncMock(side_effect=ValueError("Invalid JSON"))
    mock_client.close = AsyncMock()

    with patch("app.services.llm.client.build_llm_client", return_value=mock_client):
        result = await svc._llm_infer_column_description(
            entity_name="dwd_order",
            column_name="user_id",
        )

    assert result is None
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_infer_column_description_empty_response():
    """LLM 返回空描述或零置信度：视为不可用，返回 None。"""
    svc, _ = _svc()

    mock_client = AsyncMock()
    mock_client.enabled = True
    mock_client.chat = AsyncMock(return_value={"description": "", "confidence": 0})
    mock_client.close = AsyncMock()

    with patch("app.services.llm.client.build_llm_client", return_value=mock_client):
        result = await svc._llm_infer_column_description(
            entity_name="dwd_order",
            column_name="user_id",
        )

    assert result is None
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_infer_column_description_llm_error():
    """LLM 网关/模型错误：返回 None，降级不阻断。"""
    svc, _ = _svc()

    mock_client = AsyncMock()
    mock_client.enabled = True
    mock_client.chat = AsyncMock(side_effect=LlmError("Model not found"))
    mock_client.close = AsyncMock()

    with patch("app.services.llm.client.build_llm_client", return_value=mock_client):
        result = await svc._llm_infer_column_description(
            entity_name="dwd_order",
            column_name="user_id",
        )

    assert result is None
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_infer_column_description_runtime_error():
    """LLM 客户端初始化失败：返回 None，降级不阻断。"""
    svc, _ = _svc()

    mock_client = AsyncMock()
    mock_client.enabled = True
    mock_client.chat = AsyncMock(side_effect=RuntimeError("Client init failed"))
    mock_client.close = AsyncMock()

    with patch("app.services.llm.client.build_llm_client", return_value=mock_client):
        result = await svc._llm_infer_column_description(
            entity_name="dwd_order",
            column_name="user_id",
        )

    assert result is None
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_infer_column_description_connection_error():
    """LLM 连接错误：返回 None，降级不阻断。"""
    svc, _ = _svc()

    mock_client = AsyncMock()
    mock_client.enabled = True
    mock_client.chat = AsyncMock(side_effect=ConnectionError("Connection refused"))
    mock_client.close = AsyncMock()

    with patch("app.services.llm.client.build_llm_client", return_value=mock_client):
        result = await svc._llm_infer_column_description(
            entity_name="dwd_order",
            column_name="user_id",
        )

    assert result is None
    mock_client.close.assert_awaited_once()
