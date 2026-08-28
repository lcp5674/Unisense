"""SQL 解析 LLM 校验层 + 一致性仲裁的单元测试。

覆盖：
- ``parse_sql_validation_result``：JSON 解析、聚合/周期白名单、列名合法性、非法项丢弃
- ``merge_validation``：聚合纠正（高/低置信度）、非度量剔除、漏检补充、周期覆盖、表回映
- ``_apply_candidate_validation``：批量候选的校验收敛（纠正/剔除/漏检报告/周期回映）
- ``llm_validate_measures``：LLM 不可用降级、正常调用解析
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.semantic.sql_split import (
    _apply_candidate_validation,
    _candidate_measure_view,
)
from app.services.semantic.sql_validation import (
    merge_validation,
    parse_sql_validation_result,
)

# ---------------------------------------------------------------- parse_sql_validation_result


def test_parse_validation_valid() -> None:
    """合法 items + missed + period 解析。"""
    parsed = parse_sql_validation_result(
        '{"items": [{"key": "0", "is_measure": true, "agg": "SUM", "table": "ods.t",'
        ' "period": "day", "confidence": 0.9, "reason": "ok"}],'
        ' "missed": [{"column": "cnt", "agg": "COUNT", "alias": "visit_cnt"}],'
        ' "period": "month"}'
    )
    assert parsed is not None
    assert parsed["items"][0]["agg"] == "SUM"
    assert parsed["items"][0]["period"] == "day"
    assert parsed["missed"] == [
        {"column": "cnt", "agg": "COUNT", "alias": "visit_cnt"}
    ]
    assert parsed["period"] == "month"


def test_parse_validation_illegal_agg_and_column_dropped() -> None:
    """非法聚合（count/sumif）与列名（含空格/函数形）被丢弃。"""
    parsed = parse_sql_validation_result(
        '{"items": [{"key": "0", "agg": "sumif"},'
        ' {"key": "1", "agg": "SUM", "period": "decade"}],'
        ' "missed": [{"column": "sum(x)", "agg": "SUM"},'
        ' {"column": "ok_col", "agg": "BOGUS"},'
        ' {"column": "  ", "agg": "COUNT"}]}'
    )
    assert parsed is not None
    # items[0] 聚合非法仅不收录 agg；items[1] 聚合合法保留、period 非法仅不收录
    assert parsed["items"] == [
        {"key": "0", "is_measure": True},
        {"key": "1", "is_measure": True, "agg": "SUM"},
    ]
    assert parsed["missed"] == []  # 列不合法 + 聚合非白名单 + 空白列 全丢


def test_parse_validation_period_chinese_normalize() -> None:
    """中文周期「月」→ month。"""
    parsed = parse_sql_validation_result(
        '{"items": [{"key": "0", "is_measure": true, "agg": "COUNT_DISTINCT",'
        ' "period": "月", "confidence": 0.8}]}'
    )
    assert parsed is not None
    assert parsed["items"][0]["period"] == "month"


def test_parse_validation_empty_returns_none() -> None:
    """无 items 无 missed → None（上层保持规则结果）。"""
    assert parse_sql_validation_result("{}") is None
    assert parse_sql_validation_result("not json") is None


# ---------------------------------------------------------------- merge_validation


def _m(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {"column": "amount", "agg": "SUM"}
    base.update(kwargs)
    return base


def test_merge_agg_corrected_high_confidence() -> None:
    """高置信度聚合纠正采纳。"""
    measures = [_m(column="amount", agg="SUM", alias="gmv")]
    val = {
        "items": [
            {"key": "0", "is_measure": True, "agg": "COUNT_DISTINCT",
             "confidence": 0.9}
        ],
        "missed": [],
    }
    merged, summary = merge_validation(measures, val, ["ods.t"])
    assert merged[0]["agg"] == "COUNT_DISTINCT"
    assert merged[0]["llm_validated"] is True
    assert summary["agg_corrected"][0]["to"] == "COUNT_DISTINCT"


def test_merge_agg_not_applied_low_conf_or_illegal() -> None:
    """低置信度 / 非法聚合 → 保留规则值。"""
    measures = [_m(column="amount", agg="SUM")]
    for agg, conf in (("MAX", 0.5), ("BOGUS", 0.9)):
        val = {"items": [{"key": "0", "is_measure": True, "agg": agg,
                          "confidence": conf}], "missed": []}
        merged, _ = merge_validation(measures, val, ["ods.t"])
        assert merged[0]["agg"] == "SUM"


def test_merge_non_measure_dropped_high_conf_kept_low_conf() -> None:
    """is_measure=false 高置信度剔除；低置信度保留 + needs_review。"""
    measures = [_m(column="status", agg="SUM", alias="st")]
    val = {"items": [{"key": "0", "is_measure": False, "confidence": 0.9}],
           "missed": []}
    merged, summary = merge_validation(measures, val, ["ods.t"])
    assert merged == []
    assert summary["dropped"][0]["column"] == "status"

    val2 = {"items": [{"key": "0", "is_measure": False, "confidence": 0.4}],
            "missed": []}
    merged2, summary2 = merge_validation(measures, val2, ["ods.t"])
    assert len(merged2) == 1
    assert any(m["column"] == "status" for m in summary2["needs_review"])


def test_merge_missed_added_valid_skipped_dup() -> None:
    """漏检度量：合法加入；别名与既有冲突跳过。"""
    measures = [_m(column="amount", agg="SUM", alias="gmv")]
    val = {
        "items": [],
        "missed": [
            {"column": "buyer_cnt", "agg": "COUNT_DISTINCT", "alias": "buyer_cnt"},
            {"column": "gmv", "agg": "SUM", "alias": "gmv"},  # 与既有别名冲突
            {"column": "bad col", "agg": "SUM"},  # 列名不合法
        ],
    }
    merged, summary = merge_validation(measures, val, ["ods.t"])
    added = [m for m in merged if m.get("llm_added")]
    assert len(added) == 1
    assert added[0]["column"] == "buyer_cnt"
    assert added[0]["table"] == "ods.t"  # 回退首个源表
    assert len(summary["added"]) == 1


def test_merge_table_only_from_parsed_tables() -> None:
    """表纠正：LLM 选的表必须命中解析源表，否则保留。"""
    measures = [_m(column="amount", agg="SUM", table="ods.a")]
    val = {"items": [{"key": "0", "is_measure": True, "agg": "SUM",
                      "table": "evil.dict", "confidence": 0.95}], "missed": []}
    merged, _ = merge_validation(measures, val, ["ods.a"])
    assert merged[0]["table"] == "ods.a"


def test_merge_period_override() -> None:
    """周期覆盖随摘要返回。"""
    measures = [_m(column="amount", agg="SUM")]
    val = {"items": [], "missed": [], "period": "month"}
    _, summary = merge_validation(measures, val, [])
    assert summary["period_override"] == "month"


# ---------------------------------------------------------------- _apply_candidate_validation


def _cand(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "key": "0:gmv",
        "metric_code": "sales_gmv_amount_day",
        "name": "日成交额",
        "type": "atomic",
        "source_table": "ods.t",
        "measure_column": "amount",
        "aggregation": "SUM",
        "period": "day",
        "statement_index": 0,
    }
    base.update(kwargs)
    return base


def test_apply_candidate_validation_agg_and_period() -> None:
    """批量候选：聚合纠正 + 周期覆盖（4 段编码末段回映）。"""
    cands = [_cand()]
    val = {
        "items": [{"key": "0", "is_measure": True, "agg": "COUNT_DISTINCT",
                   "period": "month", "confidence": 0.9}],
        "missed": [],
    }
    kept, summary = _apply_candidate_validation(cands, val, ["ods.t"])
    assert kept[0]["aggregation"] == "COUNT_DISTINCT"
    assert kept[0]["period"] == "month"
    assert kept[0]["metric_code"] == "sales_gmv_amount_month"  # 4 段末段回映
    assert summary["agg_corrected"][0]["to"] == "COUNT_DISTINCT"


def test_apply_candidate_validation_drop_non_measure() -> None:
    """is_measure=false 高置信度 → 移出候选 + skipped。"""
    cands = [_cand(), _cand(key="0:cnt", measure_column="cnt", name="次数")]
    val = {"items": [
        {"key": "0", "is_measure": True, "confidence": 0.9},
        {"key": "1", "is_measure": False, "confidence": 0.95},
    ], "missed": []}
    kept, summary = _apply_candidate_validation(cands, val, ["ods.t"])
    assert len(kept) == 1
    assert summary["dropped"][0]["reason"] == "llm_not_measure"


def test_apply_candidate_validation_keeps_composite() -> None:
    """B4.1：复合候选是刻意合成的多指标聚合体——LLM 判非度量/改聚合均不采纳。

    此前复合候选经 _candidate_measure_view 交给 LLM 校验，is_measure=false 高置信
    被剔除 → 复合指标在真实 API 链路不可见。复合必须保留（依赖 + 口径 SQL 承载），
    也不对其做聚合纠正（聚合为空是复合的固有属性）。
    """
    comp = _cand(
        key="0:composite",
        name="日成交额、日用户数复合",
        type="composite",
        measure_column=None,
        aggregation=None,
        metric_code="sales_order_amount_day",
        definition_json={
            "sql": (
                "SELECT dt, SUM(amount) AS gmv, "
                "SUM(amount)/COUNT(DISTINCT user_id) AS arpu "
                "FROM dwd_order_di GROUP BY dt"
            ),
            "dependencies": ["sales_gmv_amount_day"],
        },
    )
    cands = [comp]
    val = {
        "items": [
            {
                "key": "0",
                "is_measure": False,
                "agg": "COUNT_DISTINCT",
                "period": "month",
                "confidence": 0.95,
            }
        ],
        "missed": [],
    }
    kept, summary = _apply_candidate_validation(cands, val, ["ods.t"])
    assert len(kept) == 1  # 复合不被剔除
    assert summary["dropped"] == []
    assert kept[0]["type"] == "composite"
    # 聚合不被纠正（复合聚合为空是固有属性）
    assert kept[0].get("aggregation") is None
    # 周期覆盖仍回映（复合周期是其属性）
    assert kept[0]["period"] == "month"


def test_apply_candidate_validation_missed_reported_not_added() -> None:
    """漏检扫描：报告给前端（missed），不自动加候选。"""
    cands = [_cand()]
    val = {
        "items": [{"key": "0", "is_measure": True, "confidence": 0.9}],
        "missed": [{"column": "buyer_cnt", "agg": "COUNT_DISTINCT"}],
    }
    kept, summary = _apply_candidate_validation(cands, val, ["ods.t"])
    assert len(kept) == 1  # 未自动加候选
    assert summary["missed"][0]["column"] == "buyer_cnt"


def test_candidate_measure_view_maps_fields() -> None:
    """候选 → 度量视图字段映射。"""
    view = _candidate_measure_view(_cand())
    assert view["column"] == "amount"
    assert view["agg"] == "SUM"
    assert view["alias"] == "amount"
    assert view["table"] == "ods.t"
    assert view["derived"] is False


# ---------------------------------------------------------------- llm_validate_measures


async def test_llm_validate_measures_client_disabled_returns_none() -> None:
    """LLM 客户端不可用 → None（上层保持规则结果）。"""
    from app.services.semantic.sql_validation import llm_validate_measures

    db = MagicMock()
    fake_client = MagicMock()
    fake_client.enabled = False
    with patch("app.services.llm.config_service.LlmConfigService") as mock_svc:
        mock_svc.return_value.build_client = AsyncMock(return_value=fake_client)
        result = await llm_validate_measures(
            db, "SELECT SUM(x) AS v FROM t", [{"column": "x", "agg": "SUM"}], ["t"]
        )
    assert result is None


async def test_llm_validate_measures_parses_response() -> None:
    """LLM 返回校验 JSON → 解析结果。"""
    from app.services.semantic.sql_validation import llm_validate_measures

    db = MagicMock()

    async def _fake_chat(**kwargs: object) -> dict[str, object]:
        return {
            "content": '{"items": [{"key": "0", "is_measure": true, "agg": "SUM",'
            ' "confidence": 0.9}], "missed": [{"column": "cnt", "agg": "COUNT"}]}'
        }

    fake_client = MagicMock()
    fake_client.enabled = True
    fake_client.chat = AsyncMock(side_effect=_fake_chat)
    with patch("app.services.llm.config_service.LlmConfigService") as mock_svc:
        mock_svc.return_value.build_client = AsyncMock(return_value=fake_client)
        result = await llm_validate_measures(
            db, "SELECT SUM(x) AS v FROM t", [{"column": "x", "agg": "SUM"}], ["t"]
        )
    assert result is not None
    assert result["items"][0]["agg"] == "SUM"
    assert result["missed"][0]["column"] == "cnt"


@pytest.mark.anyio
async def test_infer_sql_batch_validation_default_on_and_degrades() -> None:
    """批量规则模式默认触发校验；LLM 不可用降级规则候选不阻断。"""
    from app.services.semantic.sql_split import infer_sql_batch

    db = MagicMock()
    fake_client = MagicMock()
    fake_client.enabled = False
    with patch("app.services.llm.config_service.LlmConfigService") as mock_svc:
        mock_svc.return_value.build_client = AsyncMock(return_value=fake_client)
        result = await infer_sql_batch(
            db,
            sql="SELECT SUM(amount) AS gmv FROM dwd_order_di",
            split_mode="statement",
            domain_code="sales",
        )
    assert result["validation"] == {}  # LLM 不可用 → 无校验摘要，候选照常
    # 方案 A：SQL 物理口径候选一律派生（原子只从逻辑度量目录创建）
    cands = [c for c in result["candidates"] if c["type"] == "derived"]
    assert cands and cands[0]["measure_column"] == "amount"
