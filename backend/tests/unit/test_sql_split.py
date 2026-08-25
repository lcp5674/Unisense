"""SQL 批量切分与候选推断单元测试（FR-010 批量注册增强，场景A/B）。

覆盖：
- 场景A 三模式切分：semicolon（引号/注释内分号不误切）、statement（CTE/INSERT 单条）、
  custom（delimiters/start_markers 正则 + LLM 语义分段兜底 + 不可用降级单段）
- 场景B 单语句多度量拆分（split_select_measures：共享源表/维度/周期）
- parse_sql_split_result（别名/去重/空段过滤/整体失败 None）
- infer_sql_batch 集成：多语句候选生成、多度量拆分 + 复合合成、skipped、域建议回填
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.services.llm.parse import parse_sql_split_result
from app.services.semantic.sql_infer import parse_sql_profile
from app.services.semantic.sql_split import (
    _split_semicolon,
    _split_statement,
    infer_sql_batch,
    split_select_measures,
    split_sql_statements,
)

# 多语句 SQL（场景A）：注释 + 两条 SELECT
_MULTI_SQL = """
-- 指标1：订单金额
SELECT dt, SUM(amount) AS gmv FROM dwd_order_di WHERE dt >= '2026-01-01' GROUP BY dt;

-- 指标2：去重用户数
WITH base AS (SELECT user_id, dt FROM dwd_user_di WHERE dt >= '2026-01-01')
SELECT dt, COUNT(DISTINCT user_id) AS uv FROM base GROUP BY dt
"""
# 单语句多度量（场景B）
_MULTI_MEASURE_SQL = (
    "SELECT dt, SUM(amount) AS gmv, COUNT(DISTINCT user_id) AS uv "
    "FROM dwd_order_di GROUP BY dt"
)


# ---------------------------------------------------------------- 场景A：切分


def test_split_semicolon_basic_and_quote_safe() -> None:
    """分号切分：普通多语句 + 引号内分号不切断。"""
    segments = _split_semicolon("SELECT 'a;b' AS x FROM t; SELECT 2")
    assert len(segments) == 2
    assert "'a;b'" in segments[0]
    # 反引号/双引号内分号同样不切断
    assert len(_split_semicolon('SELECT `a;b` FROM t; SELECT `c`')) == 2
    assert len(_split_semicolon('SELECT "a;b" FROM t; SELECT "c"')) == 2


def test_split_semicolon_comment_semicolon() -> None:
    """行注释内分号不切断（引号扫描对 -- 注释天然忽略）。"""
    segments = _split_semicolon("-- a;b\nSELECT 1; SELECT 2")
    assert len(segments) == 2
    assert "-- a;b" in segments[0]


def test_split_semicolon_no_trailing_semicolon() -> None:
    """末段无分号也应返回（尾段）。"""
    segments = _split_semicolon("SELECT 1; SELECT 2")
    assert segments == ["SELECT 1", "SELECT 2"]


def test_split_statement_cte_and_insert() -> None:
    """statement 模式：CTE 单条、INSERT INTO...SELECT 单条、普通多 SELECT。"""
    sql = "WITH a AS (SELECT 1) SELECT * FROM a; INSERT INTO tgt SELECT * FROM src"
    segments = _split_statement(sql)
    assert len(segments) == 2
    assert "WITH a AS" in segments[0]
    assert "INSERT INTO" in segments[1]


def test_split_statement_invalid_sql_returns_empty() -> None:
    """statement 模式非法 SQL → 空列表（上层回退分号切分）。"""
    assert _split_statement("NOT A VALID SQL") == []


async def test_split_statements_statement_mode_fallback() -> None:
    """statement 模式：语义切分失败时回退分号切分（非法 SQL 至少 1 段）。"""
    segments = await split_sql_statements("SELECT 1; SELECT 2", mode="statement")
    assert len(segments) == 2


async def test_split_statements_semicolon_mode() -> None:
    segments = await split_sql_statements("SELECT 1; SELECT 2", mode="semicolon")
    assert len(segments) == 2


async def test_split_statements_custom_delimiters() -> None:
    """custom 模式：delimiters 正则切分。"""
    sql = "SELECT 1 /*---*/ SELECT 2 /*---*/ SELECT 3"
    segments = await split_sql_statements(
        sql, mode="custom", custom_rules={"delimiters": [r"/\*---\*/"]}
    )
    assert len(segments) == 3


async def test_split_statements_custom_start_markers() -> None:
    """custom 模式：start_markers 正则切分（标记命中位置为段起点）。"""
    sql = "-- 指标A\nSELECT 1\n-- 指标B\nSELECT 2"
    segments = await split_sql_statements(
        sql, mode="custom", custom_rules={"start_markers": [r"-- 指标"]}
    )
    assert len(segments) == 2
    assert "指标A" in segments[0]
    assert "指标B" in segments[1]


async def test_split_statements_custom_llm_fallback() -> None:
    """custom 模式规则未生效（≤1 段）→ LLM 语义分段兜底。"""
    llm_segments = ["SELECT SUM(a) FROM t1", "SELECT COUNT(b) FROM t2"]
    with patch(
        "app.services.semantic.sql_split._llm_split",
        new=AsyncMock(return_value=llm_segments),
    ):
        segments = await split_sql_statements(
            "SELECT SUM(a) FROM t1; SELECT COUNT(b) FROM t2",
            mode="custom",
            custom_rules={"delimiters": ["---"]},  # 未命中 → LLM
            db=MagicMock(),
        )
    assert segments == llm_segments


async def test_split_statements_custom_llm_unavailable_degrade() -> None:
    """LLM 兜底不可用（返回 None）→ 降级整段单候选。"""
    with patch(
        "app.services.semantic.sql_split._llm_split",
        new=AsyncMock(return_value=None),
    ):
        segments = await split_sql_statements(
            "SELECT SUM(a) FROM t1; SELECT COUNT(b) FROM t2",
            mode="custom",
            custom_rules={"delimiters": ["---"]},
            db=MagicMock(),
        )
    assert segments == ["SELECT SUM(a) FROM t1; SELECT COUNT(b) FROM t2"]


async def test_split_statements_custom_no_db_skips_llm() -> None:
    """db 缺省（None）时 custom 模式不触发 LLM（纯规则兜底）。"""
    with patch(
        "app.services.semantic.sql_split._llm_split",
        new=AsyncMock(return_value=["x", "y"]),
    ) as m:
        segments = await split_sql_statements(
            "SELECT 1; SELECT 2",
            mode="custom",
            custom_rules={"delimiters": ["---"]},
        )
    m.assert_not_awaited()
    assert segments == ["SELECT 1; SELECT 2"]


def test_split_statement_multi_sql_preserves_statements() -> None:
    """真实多指标脚本：statement 模式切出 2 条独立语句（CTE 单条正确）。"""
    segments = _split_statement(_MULTI_SQL)
    assert len(segments) == 2
    assert "SUM(amount)" in segments[0]
    assert "COUNT(DISTINCT user_id)" in segments[1]


# ---------------------------------------------------------------- 场景B：多度量拆分


def test_split_select_measures_multi() -> None:
    """单语句多度量 → N 个候选（共享源表/维度/周期，group_key 一致）。"""
    profile = parse_sql_profile(_MULTI_MEASURE_SQL)
    measures = split_select_measures(_MULTI_MEASURE_SQL, profile)
    assert len(measures) == 2
    assert {m["measure_column"] for m in measures} == {"amount", "user_id"}
    assert all(m["source_table"] == "dwd_order_di" for m in measures)
    assert all(m["period"] == "day" for m in measures)
    assert measures[0]["group_key"] == measures[1]["group_key"] == "amountuserid"
    assert measures[0]["aggregation"] == "SUM"
    assert measures[1]["aggregation"] == "COUNT_DISTINCT"


def test_split_select_measures_single() -> None:
    """单度量语句 → 1 个候选。"""
    measures = split_select_measures(
        "SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt"
    )
    assert len(measures) == 1
    assert measures[0]["aggregation"] == "SUM"


# ---------------------------------------------------------------- parse_sql_split_result


def test_parse_sql_split_result_valid() -> None:
    parsed = parse_sql_split_result(
        '{"statements": [{"sql": "SELECT 1", "name": "a"}, {"sql": "SELECT 2", "name": "b"}]}'
    )
    assert parsed == [
        {"sql": "SELECT 1", "name": "a", "reason": None},
        {"sql": "SELECT 2", "name": "b", "reason": None},
    ]


def test_parse_sql_split_result_aliases_and_dedup() -> None:
    """sql 别名（text/segment）+ 重复/空段过滤。"""
    parsed = parse_sql_split_result(
        '{"statements": [{"text": "SELECT 1"}, {"segment": "SELECT 1"}, {"sql": "  "}]}'
    )
    assert parsed == [{"sql": "SELECT 1", "name": None, "reason": None}]


def test_parse_sql_split_result_none_on_invalid() -> None:
    """整体无有效片段 / 非 JSON / 缺 statements → None。"""
    assert parse_sql_split_result("不是 JSON") is None
    assert parse_sql_split_result('{"statements": []}') is None
    assert parse_sql_split_result('{"foo": 1}') is None


# ---------------------------------------------------------------- infer_sql_batch


def _fake_db() -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    return db


async def test_infer_sql_batch_multi_statement_candidates() -> None:
    """场景A 多语句：每条语句各自产出原子候选（CTE 别名不误当源表）。"""
    result = await infer_sql_batch(
        _fake_db(), sql=_MULTI_SQL, split_mode="statement", domain_code="sales"
    )
    assert len(result["statements"]) == 2
    assert len(result["candidates"]) == 2
    codes = {c["metric_code"] for c in result["candidates"]}
    assert "sales_order_amount_day" in codes
    # CTE 语句取物理表 dwd_user_di（非 CTE 别名 base）
    assert "sales_user_userid_day" in codes
    # 原子候选完整字段
    first = result["candidates"][0]
    assert first["type"] == "atomic"
    assert first["aggregation"] in ("SUM", "COUNT_DISTINCT")
    assert first["definition_json"]["expression"]
    assert first["source_table"] == "dwd_order_di"
    assert first["period"] == "day"


async def test_infer_sql_batch_synthesize_composite() -> None:
    """场景B 多度量 + synthesize_composite → N 原子 + 1 复合（依赖组内原子）。"""
    result = await infer_sql_batch(
        _fake_db(),
        sql=_MULTI_MEASURE_SQL,
        split_mode="statement",
        domain_code="sales",
        synthesize_composite=True,
    )
    composites = [c for c in result["candidates"] if c["type"] == "composite"]
    atoms = [c for c in result["candidates"] if c["type"] == "atomic"]
    assert len(atoms) == 2
    assert len(composites) == 1
    comp = composites[0]
    assert comp["key"] == "0:composite"
    assert comp["metric_code"].startswith("sales_order_")
    assert set(comp["dependencies"]) == {a["metric_code"] for a in atoms}
    assert comp["definition_json"]["sql"] == _MULTI_MEASURE_SQL


async def test_infer_sql_batch_single_measure_no_composite() -> None:
    """单度量语句即使 synthesize_composite=True 也不合成复合（需 ≥2 原子）。"""
    result = await infer_sql_batch(
        _fake_db(),
        sql="SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt",
        split_mode="statement",
        domain_code="sales",
        synthesize_composite=True,
    )
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["type"] == "atomic"


async def test_infer_sql_batch_skipped_statement() -> None:
    """无聚合度量列的语句进 skipped（候选不产出）。"""
    sql = "SELECT 1; SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt"
    result = await infer_sql_batch(_fake_db(), sql=sql, split_mode="semicolon", domain_code="sales")
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "未解析到聚合度量列"
    assert len(result["candidates"]) == 1


async def test_infer_sql_batch_domain_suggestion_applied() -> None:
    """域缺省时整段建议：unique 命中自动回填候选域。"""
    suggestion = {
        "status": "unique",
        "domain": {"code": "sales", "name": "销售", "confidence": 0.9, "source": "catalog"},
        "candidates": [],
        "matched_tables": ["dwd_order_di"],
    }
    with patch(
        "app.services.semantic.domain_suggest.suggest_domain",
        new=AsyncMock(return_value=suggestion),
    ):
        result = await infer_sql_batch(
            _fake_db(), sql=_MULTI_SQL, split_mode="statement", synthesize_composite=True
        )
    assert result["domain"]["code"] == "sales"
    assert result["domain"]["status"] == "unique"
    # 候选 metric_code 使用建议域
    assert any(c["metric_code"].startswith("sales_") for c in result["candidates"])


async def test_infer_sql_batch_explicit_domain_no_suggest() -> None:
    """显式指定域 → 不触发建议（domain.status=user）。"""
    with patch(
        "app.services.semantic.domain_suggest.suggest_domain",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        result = await infer_sql_batch(
            _fake_db(), sql=_MULTI_SQL, split_mode="statement", domain_code="sales"
        )
    assert result["domain"]["status"] == "user"
    assert result["domain"]["code"] == "sales"


async def test_infer_sql_batch_empty_sql() -> None:
    """空 SQL → 空结果不抛异常。"""
    result = await infer_sql_batch(_fake_db(), sql="   ")
    assert result["candidates"] == []
    assert result["statements"] == []
