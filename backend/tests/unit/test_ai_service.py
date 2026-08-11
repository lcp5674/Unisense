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
    out = await svc.nl2sql("查看 未知指标 趋势")
    assert out["safe"] is False
    assert out["sql"] == ""


async def test_nl2sql_rejects_select_star() -> None:
    svc, repo = await _svc({"活跃用户"})
    with pytest.raises(UnisenseError):
        await svc.nl2sql("select * from unified_metric")


async def test_nl2sql_rejects_dml() -> None:
    svc, repo = await _svc({"活跃用户"})
    with pytest.raises(UnisenseError):
        await svc.nl2sql("delete from unified_metric")
