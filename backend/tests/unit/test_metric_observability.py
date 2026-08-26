"""指标埋点接线锚定测试（T-2，第七轮技术债）。

observe_llm_call / observe_metric_publish / observe_query_result 此前「定义但零断言」——
调用点存在（llm/client.py / semantic/service.py / consume/service.py）但全 tests/ 无锚定，
埋点一旦被后续重构误删不会暴露。本文件为三处接线各补真实触发断言：
- LLM 调用成功/失败 → observe_llm_call(success=...)
- 指标标准发布 → observe_metric_publish
- 查询引擎执行成功/降级失败 → observe_query_result(success=...)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.governance.policy import Decision


@pytest.fixture(autouse=True)
def _reset_shared_llm_breaker() -> None:
    """每个测试前重置共享 LLM 熔断器单例（失败测试会 record_failure 打满阈值）。"""
    from app.services.llm.client import _LLM_BREAKER

    _LLM_BREAKER._open = False
    _LLM_BREAKER._failures = 0
    _LLM_BREAKER._opened_at = None
    _LLM_BREAKER._probing = False
    _LLM_BREAKER._probing_since = None
    _LLM_BREAKER._recent_outcomes.clear()


# ---- observe_llm_call：成功/失败两路接线 ----

async def test_observe_llm_call_on_success() -> None:
    """LLM chat 成功 → observe_llm_call(success=True) 真实触发。"""
    from app.services.llm.client import LlmClient

    client = LlmClient(base_url="https://api.example.com", api_key="test-key")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "Hello", "finish_reason": "stop"}}],
        "model": "test-model",
        "usage": {},
    }
    client._client = MagicMock()
    client._client.post = AsyncMock(return_value=mock_response)

    with patch("app.core.metrics.store.observe_llm_call") as observe:
        await client.chat([{"role": "user", "content": "Hi"}])

    observe.assert_called_once_with(success=True)


async def test_observe_llm_call_on_http_failure() -> None:
    """LLM HTTP 5xx（非重试上限前最后一次）→ observe_llm_call(success=False) 触发。"""
    import httpx

    from app.services.llm.client import LlmClient, LlmError

    client = LlmClient(base_url="https://api.example.com", api_key="test-key")
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    response = httpx.Response(500, request=request, text="boom")
    http_err = httpx.HTTPStatusError("500 Server Error", request=request, response=response)
    client._client = MagicMock()
    # 每次尝试都 500：重试耗尽后走到失败出口
    client._client.post = AsyncMock(side_effect=http_err)

    with patch("app.core.metrics.store.observe_llm_call") as observe, pytest.raises(LlmError):
        await client.chat([{"role": "user", "content": "Hi"}])

    observe.assert_called_with(success=False)


# ---- observe_metric_publish：标准发布接线 ----

def _approve_ready_svc() -> tuple[object, MagicMock]:
    """构造 approve_metric 标准发布所需的 mock 环境（对齐 test_semantic_service 模式）。"""
    from tests.conftest import make_metric

    from app.services.semantic.service import MetricService

    mock_gov_svc = MagicMock()
    mock_gov_svc.check_metric_permission = AsyncMock(
        return_value=Decision(allow=True, reason="mocked_allowed")
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    with patch("app.services.semantic.service.MetricRepository") as mock_repo_cls:
        repo = MagicMock()
        metric = make_metric(
            status="REVIEW",
            submitted_by=1,
            owner_id=2,
            reviewer_id=99,
            reviewer_type="user",
        )
        repo.get_by_code = AsyncMock(return_value=metric)
        repo.get_version = AsyncMock(return_value=MagicMock(id=1, version=1))
        repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
        repo.mark_version_published = AsyncMock(return_value=None)
        mock_repo_cls.return_value = repo
        svc = MetricService(db=db, governance_svc=mock_gov_svc)
        svc._cache.invalidate = AsyncMock(return_value=None)
        svc._publish_event = AsyncMock(return_value=None)
        return svc, repo


async def test_observe_metric_publish_on_standard_approve() -> None:
    """标准发布（mode=standard）→ observe_metric_publish 真实触发。"""
    from app.services.semantic.schemas import MetricApproveRequest

    svc, _repo = _approve_ready_svc()

    with patch("app.core.metrics.store.observe_metric_publish") as observe:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=99,
            role="reviewer",
        )

    observe.assert_called_once()


# ---- observe_query_result：引擎执行成功/降级接线 ----

async def test_observe_query_result_on_mysql_success() -> None:
    """OLAP 未配置 → MySQL 降级路径执行成功 → observe_query_result(True) 触发。"""
    from types import SimpleNamespace

    from app.services.consume.service import ConsumeService, _get_mysql_executor

    svc = ConsumeService(MagicMock())
    mysql_executor = MagicMock()
    mysql_executor.enabled = True
    mysql_executor.execute = AsyncMock(return_value=MagicMock(rows=[], total=0))

    with (
        patch.object(_get_mysql_executor.__globals__["settings"], "olap_url", ""),
        patch("app.services.consume.service._get_mysql_executor", return_value=mysql_executor),
        patch("app.core.metrics.store.observe_query_result") as observe,
    ):
        result, engine = await svc._execute_with_fallback(
            SimpleNamespace(accept_stale=False),
            "SELECT 1",
            {},
        )

    assert engine == "mysql"
    observe.assert_called_once_with(True)


async def test_observe_query_result_on_engine_failure() -> None:
    """OLAP 未配置 + MySQL 也不可用 → 抛降级错误，observe_query_result(False) 触发。"""
    from types import SimpleNamespace

    from app.core.error_codes import ErrorCode
    from app.core.exceptions import BusinessError
    from app.services.consume.service import ConsumeService

    svc = ConsumeService(MagicMock())
    mysql_executor = MagicMock()
    mysql_executor.enabled = True
    mysql_executor.execute = AsyncMock(side_effect=RuntimeError("mysql down"))

    with (
        patch.object(ConsumeService.__init__.__globals__["settings"], "olap_url", ""),
        patch("app.services.consume.service._get_mysql_executor", return_value=mysql_executor),
        patch("app.core.metrics.store.observe_query_result") as observe,
        pytest.raises(BusinessError) as exc_info,
    ):
        await svc._execute_with_fallback(SimpleNamespace(accept_stale=False), "SELECT 1", {})

    assert exc_info.value.error_code == ErrorCode.DEPENDENCY_DEGRADED_ENGINE
    observe.assert_called_once_with(False)
