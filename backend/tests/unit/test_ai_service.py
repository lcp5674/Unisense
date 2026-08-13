"""AI 问数服务单元测试（TD §12.7 / FR-14）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import UnisenseError
from app.services.ai.service import AiService


async def _svc(vocab: set[str]) -> tuple[AiService, MagicMock]:
    db = MagicMock()
    svc = AiService(db)
    repo = MagicMock()
    repo.vocabulary = AsyncMock(return_value=vocab)
    svc._repo = repo  # noqa: SLF001
    return svc, repo


async def test_nl2sql_anchors_known_metric() -> None:
    svc, repo = await _svc({"活跃用户", "gmv"})
    out = await svc.nl2sql("查看 活跃用户 趋势")
    assert out["safe"] is True
    assert "活跃用户" in out["anchored"]
    assert "SELECT" in out["sql"]
    assert "unified_metric" in out["sql"]


async def test_nl2sql_rejects_unanchored() -> None:
    svc, repo = await _svc({"gmv"})
    # 覆盖 LLM 降级：本地无 LLM 配置 => build_llm_client 返回降级客户端
    # 未锚定到任何词汇表词 => 关键词匹配返回空 sql
    out = await svc.nl2sql("查看 未知指标 趋势")
    assert out["sql"] == ""
    assert out["anchored"] == []


async def test_nl2sql_rejects_select_star() -> None:
    svc, repo = await _svc({"活跃用户"})
    with pytest.raises(UnisenseError):
        await svc.nl2sql("select * from unified_metric")


async def test_nl2sql_rejects_dml() -> None:
    svc, repo = await _svc({"活跃用户"})
    with pytest.raises(UnisenseError):
        await svc.nl2sql("delete from unified_metric")


async def test_ask_execute_false_passthrough() -> None:
    svc, repo = await _svc({"gmv"})
    out = await svc.ask("查看 gmv 趋势", execute=False)
    assert out["execute"] is False
    assert "sql" in out


async def test_ask_execute_true_delegates_to_olap(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, repo = await _svc({"gmv"})

    class _FakeResult:
        rows = [{"metric_code": "gmv", "value": 1}]
        total = 1
        elapsed_ms = 5

    class _FakeExecutor:
        async def execute(self, sql: str, params: dict) -> _FakeResult:
            return _FakeResult()

    monkeypatch.setattr(
        "app.services.consume.olap_executor.OLAPExecutor",
        lambda: _FakeExecutor(),
    )
    out = await svc.ask("查看 gmv 趋势", execute=True)
    assert out["execute"] is True
    assert out["execute_result"]["total"] == 1


async def test_ask_execute_error_graceful(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, repo = await _svc({"gmv"})

    class _BoomExecutor:
        async def execute(self, sql: str, params: dict) -> None:
            raise RuntimeError("OLAP 不可达")

    monkeypatch.setattr(
        "app.services.consume.olap_executor.OLAPExecutor",
        lambda: _BoomExecutor(),
    )
    out = await svc.ask("查看 gmv 趋势", execute=True)
    # 执行失败不抛异常，写入 execute_error
    assert "execute_error" in out
    assert "OLAP 不可达" in out["execute_error"]
