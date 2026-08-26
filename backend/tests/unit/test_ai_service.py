"""AI 问数服务单元测试（TD §12.7 / FR-14）。"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import UnisenseError
from app.services.ai.service import AiService

_LLM_SQL = (
    "SELECT metric_code, value FROM unified_metric WHERE metric_code = 'sales_gmv_amount_day'"
)


@pytest.fixture(autouse=True)
def _clear_shared_cache() -> None:
    """AiService._cache 为类属性（跨实例共享），每测试前清空防污染。"""
    AiService._cache.clear()
    yield
    AiService._cache.clear()


def _enabled_llm() -> MagicMock:
    """构造启用且可返回 SQL 的假 LLM（用于缓存行为验证）。"""
    llm = MagicMock()
    llm.enabled = True
    llm.chat = AsyncMock(return_value={"content": _LLM_SQL})
    return llm


async def _svc_with_llm(vocab: set[str], llm: MagicMock) -> tuple[AiService, MagicMock]:
    db = MagicMock()
    svc = AiService(db, llm=llm)
    repo = MagicMock()
    repo.vocabulary = AsyncMock(return_value=vocab)
    svc._repo = repo  # noqa: SLF001
    return svc, repo


async def _svc(vocab: set[str]) -> tuple[AiService, MagicMock]:
    db = MagicMock()
    svc = AiService(db)
    repo = MagicMock()
    repo.vocabulary = AsyncMock(return_value=vocab)
    svc._repo = repo  # noqa: SLF001
    return svc, repo


class TestNl2SqlCache:
    """NL2SQL 结果缓存（避免重复打外部 LLM 网关，perf P95 优化）。"""

    async def test_cache_hit_skips_llm(self) -> None:
        llm = _enabled_llm()
        svc, _repo = await _svc_with_llm({"sales_gmv_amount_day"}, llm)
        first = await svc.nl2sql("查看销售额")
        second = await svc.nl2sql("查看销售额")
        assert first["method"] == "llm"
        assert second["sql"] == first["sql"]
        # 第二次命中缓存，LLM 只被调用一次
        assert llm.chat.await_count == 1

    async def test_cache_miss_on_different_query(self) -> None:
        llm = _enabled_llm()
        svc, _repo = await _svc_with_llm({"sales_gmv_amount_day"}, llm)
        await svc.nl2sql("查看销售额")
        await svc.nl2sql("查看昨日销售额")
        assert llm.chat.await_count == 2

    async def test_cache_expiry_reinvokes_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = _enabled_llm()
        svc, _repo = await _svc_with_llm({"sales_gmv_amount_day"}, llm)
        await svc.nl2sql("查看销售额")
        # 把缓存条目时间戳拨到过期
        key = svc._cache_key("查看销售额", None)  # noqa: SLF001
        _, value = svc._cache[key]  # noqa: SLF001
        svc._cache[key] = (time.monotonic() - 200, value)  # noqa: SLF001
        await svc.nl2sql("查看销售额")
        assert llm.chat.await_count == 2

    async def test_cache_returns_copy_not_shared(self) -> None:
        llm = _enabled_llm()
        svc, _repo = await _svc_with_llm({"sales_gmv_amount_day"}, llm)
        first = await svc.nl2sql("查看销售额")
        first["notes"].append("被调用方修改")
        second = await svc.nl2sql("查看销售额")
        # 浅拷贝隔离：调用方修改不污染缓存
        assert "被调用方修改" not in second["notes"]

    async def test_cache_cleared_by_clear_cache(self) -> None:
        llm = _enabled_llm()
        svc, _repo = await _svc_with_llm({"sales_gmv_amount_day"}, llm)
        await svc.nl2sql("查看销售额")
        svc.clear_cache()
        await svc.nl2sql("查看销售额")
        assert llm.chat.await_count == 2


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


async def test_ask_execute_true_no_direct_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """安全加固（X-1）：execute=True 不再直接执行 LLM 生成的任意 SQL。

    此前执行路径绕过 consume 统一鉴权（PDP/域/白名单/PII 脱敏），低权限用户
    可越权读取任意表；现统一降级为「只生成 SQL」，execute 恒为 False，
    不产出 execute_result/execute_error，并附引导 note。
    """
    svc, repo = await _svc({"gmv"})

    constructed: list[int] = []

    class _FakeExecutor:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            constructed.append(1)

        async def execute(self, sql: str, params: dict) -> None:
            raise AssertionError("不应直接执行 LLM SQL")

    monkeypatch.setattr(
        "app.services.consume.service._get_olap_executor",
        lambda: _FakeExecutor(),
    )
    out = await svc.ask("查看 gmv 趋势", execute=True)
    assert out["execute"] is False
    assert "execute_result" not in out
    assert "execute_error" not in out
    # 引导到查询工作台执行的提示已附加
    assert any("查询工作台" in n for n in out.get("notes", []))
    # 执行路径移除后不应构造 OLAP executor
    assert constructed == []


async def test_ask_execute_true_without_llm_sql_still_safe() -> None:
    """execute=True 但未生成 SQL 时同样安全降级（无执行、无错误）。"""
    svc, repo = await _svc(set())
    out = await svc.ask("查看 未知指标 趋势", execute=True)
    assert out["execute"] is False
    assert "execute_result" not in out
    assert "execute_error" not in out
