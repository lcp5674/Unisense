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
    mock_client.chat = AsyncMock(
        return_value={"content": '{"description": "用户唯一标识ID", "confidence": 0.85}'}
    )
    mock_client.close = AsyncMock()

    with patch(
        "app.services.llm.config_service.LlmConfigService.build_client",
        new=AsyncMock(return_value=mock_client),
    ):
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

    with patch(
        "app.services.llm.config_service.LlmConfigService.build_client",
        new=AsyncMock(return_value=mock_client),
    ):
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

    with patch(
        "app.services.llm.config_service.LlmConfigService.build_client",
        new=AsyncMock(return_value=mock_client),
    ):
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

    with patch(
        "app.services.llm.config_service.LlmConfigService.build_client",
        new=AsyncMock(return_value=mock_client),
    ):
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
    mock_client.chat = AsyncMock(return_value={"content": ""})
    mock_client.close = AsyncMock()

    with patch(
        "app.services.llm.config_service.LlmConfigService.build_client",
        new=AsyncMock(return_value=mock_client),
    ):
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

    with patch(
        "app.services.llm.config_service.LlmConfigService.build_client",
        new=AsyncMock(return_value=mock_client),
    ):
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

    with patch(
        "app.services.llm.config_service.LlmConfigService.build_client",
        new=AsyncMock(return_value=mock_client),
    ):
        result = await svc._llm_infer_column_description(
            entity_name="dwd_order",
            column_name="user_id",
        )

    assert result is None
    mock_client.close.assert_awaited_once()


# ---- 表级业务描述推断（TD §12.1） ----


@pytest.mark.asyncio
async def test_llm_infer_table_description_normal():
    """正常推断：基于表名 + 字段清单返回表级描述。"""
    svc, _ = _svc()

    mock_client = AsyncMock()
    mock_client.enabled = True
    mock_client.chat = AsyncMock(
        return_value={"content": '{"description": "订单明细事实表", "confidence": 0.9}'}
    )
    mock_client.close = AsyncMock()

    columns = [
        {"name": "order_id", "type": "bigint"},
        {"name": "user_id", "type": "bigint"},
    ]
    with patch(
        "app.services.llm.config_service.LlmConfigService.build_client",
        new=AsyncMock(return_value=mock_client),
    ):
        result = await svc._llm_infer_table_description(
            entity_name="dwd_order", columns=columns
        )

    assert result is not None
    assert result["description"] == "订单明细事实表"
    assert result["confidence"] == 0.9
    mock_client.close.assert_awaited_once()
    # 字段清单应传给 LLM 上下文
    call_content = mock_client.chat.await_args.args[0][1]["content"]
    assert "order_id" in call_content


@pytest.mark.asyncio
async def test_llm_infer_table_description_truncates_many_columns():
    """字段超 30 个时截断，避免请求体过长。"""
    svc, _ = _svc()

    mock_client = AsyncMock()
    mock_client.enabled = True
    mock_client.chat = AsyncMock(
        return_value={"content": '{"description": "大宽表", "confidence": 0.8}'}
    )
    mock_client.close = AsyncMock()

    columns = [{"name": f"col_{i}", "type": "varchar"} for i in range(50)]
    with patch(
        "app.services.llm.config_service.LlmConfigService.build_client",
        new=AsyncMock(return_value=mock_client),
    ):
        result = await svc._llm_infer_table_description(
            entity_name="dwd_wide", columns=columns
        )

    assert result is not None
    call_content = mock_client.chat.await_args.args[0][1]["content"]
    assert "col_49" not in call_content  # 超出 30 个被截断


@pytest.mark.asyncio
async def test_llm_infer_table_description_disabled():
    """LLM 不可用：返回 None，不阻断。"""
    svc, _ = _svc()

    mock_client = AsyncMock()
    mock_client.enabled = False
    mock_client.close = AsyncMock()

    with patch(
        "app.services.llm.config_service.LlmConfigService.build_client",
        new=AsyncMock(return_value=mock_client),
    ):
        result = await svc._llm_infer_table_description(
            entity_name="dwd_order", columns=[]
        )

    assert result is None
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_infer_table_description_timeout():
    """LLM 超时：返回 None，降级不阻断。"""
    svc, _ = _svc()

    mock_client = AsyncMock()
    mock_client.enabled = True
    mock_client.chat = AsyncMock(side_effect=TimeoutError("LLM timeout"))
    mock_client.close = AsyncMock()

    with patch(
        "app.services.llm.config_service.LlmConfigService.build_client",
        new=AsyncMock(return_value=mock_client),
    ):
        result = await svc._llm_infer_table_description(
            entity_name="dwd_order", columns=[]
        )

    assert result is None
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_build_client_falls_back_on_db_config_error():
    """DB 配置读取异常时回退 env 静态客户端，推断仍可用（回归：描述推断不因 DB 抖动 500）。"""
    svc, _ = _svc()

    mock_client = AsyncMock()
    mock_client.enabled = True
    mock_client.chat = AsyncMock(
        return_value={"content": '{"description": "用户唯一标识ID", "confidence": 0.85}'}
    )
    mock_client.close = AsyncMock()

    with (
        patch(
            "app.services.llm.config_service.LlmConfigService.build_client",
            side_effect=RuntimeError("db down"),
        ),
        patch("app.services.llm.client.build_llm_client", return_value=mock_client),
    ):
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
async def test_llm_infer_column_description_non_json_content():
    """LLM 返回非 JSON 内容：视为不可用，返回 None（解析健壮性）。"""
    svc, _ = _svc()

    mock_client = AsyncMock()
    mock_client.enabled = True
    mock_client.chat = AsyncMock(return_value={"content": "抱歉，我无法理解"})
    mock_client.close = AsyncMock()

    with patch(
        "app.services.llm.config_service.LlmConfigService.build_client",
        new=AsyncMock(return_value=mock_client),
    ):
        result = await svc._llm_infer_column_description(
            entity_name="dwd_order",
            column_name="user_id",
        )

    assert result is None
    mock_client.close.assert_awaited_once()
