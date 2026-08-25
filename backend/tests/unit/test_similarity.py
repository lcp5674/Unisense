"""冲突相似度（纯函数）单元测试。"""

from __future__ import annotations

from app.models.conflict import ConflictType
from app.services.conflict.similarity import (
    composite_score,
    definition_similarity,
    detect_conflict,
    lineage_overlap,
    name_similarity,
)


def test_name_similarity_identical() -> None:
    assert name_similarity("gmv_total", "gmv_total") == 1.0


def test_definition_similarity_high_for_similar() -> None:
    assert definition_similarity("sum(amount)", "sum(amount)") == 1.0
    assert definition_similarity("sum(amount)", "count(amount)") < 1.0


def test_lineage_overlap_jaccard() -> None:
    assert lineage_overlap(["a", "b"], ["a", "b"]) == 1.0
    assert lineage_overlap(["a", "b"], ["c"]) == 0.0
    assert lineage_overlap(["a", "b"], ["a", "c"]) == 1 / 3
    # P1-E：双方均无源表 → 0（无同源证据不抬高综合分，避免误报重复建设）
    assert lineage_overlap([], []) == 0.0


def test_composite_score_weighted() -> None:
    # 0.4*1 + 0.4*1 + 0.2*1 = 1.0
    assert composite_score(1.0, 1.0, 1.0) == 1.0


def test_detect_hard_same_name_diff_def() -> None:
    cand = {"metric_code": "gmv_total", "domain": "sales", "definition": "sum(amount)"}
    existing = {"metric_code": "gmv_total", "domain": "finance", "definition": "sum(price)"}
    det = detect_conflict(cand, existing)
    assert det is not None
    assert det.conflict_type == ConflictType.SAME_NAME_DIFF_DEF
    assert det.block_publish is True
    assert det.severity == "hard"


def test_detect_soft_same_def_diff_name() -> None:
    cand = {
        "metric_code": "sales_amt",
        "domain": "sales",
        "definition": "sum(amount) filter where status=1",
    }
    existing = {
        "metric_code": "gmv_total",
        "domain": "sales",
        "definition": "sum(amount) filter where status=1",
    }
    det = detect_conflict(cand, existing)
    assert det is not None
    assert det.conflict_type == ConflictType.SAME_DEF_DIFF_NAME
    assert det.block_publish is False
    assert det.score >= 0.85


def test_detect_pii_routes_to_governance() -> None:
    cand = {"metric_code": "user_pii", "domain": "sales", "definition": "x", "has_pii": True}
    existing = {"metric_code": "user_pii2", "domain": "sales", "definition": "y"}
    det = detect_conflict(cand, existing)
    assert det is not None
    assert det.conflict_type == ConflictType.PII
    assert det.block_publish is True


def test_detect_no_conflict_when_dissimilar() -> None:
    cand = {"metric_code": "orders_cnt", "domain": "sales", "definition": "count(id)"}
    existing = {"metric_code": "refund_amt", "domain": "sales", "definition": "sum(refund)"}
    assert detect_conflict(cand, existing) is None


def test_detect_self_same_metric_id_returns_none() -> None:
    """候选与现有携带同一指标行 ID（同一条真实指标）→ 不构成冲突。

    自我引用防御：指标与自身比对（无论域/定义如何）永远不该产出冲突，
    否则仲裁联动会把"落败方"（=胜方自身）作废，导致数据被误删。
    """
    cand = {
        "metric_code": "gmv_total",
        "domain": "sales",
        "definition": "sum(amount)",
        "metric_id": 7,
    }
    existing = {
        "metric_code": "gmv_total",
        "domain": "finance",
        "definition": "sum(price)",
        "metric_id": 7,
    }
    assert detect_conflict(cand, existing) is None


# ---- P0-C：口径版本冲突（VERSION_CONFLICT 分支从未实现） ----


def test_detect_version_conflict_same_code_same_def_same_domain() -> None:
    """同码同义同域但不同指标行（版本/修订并存）→ VERSION_CONFLICT 软冲突。

    枚举与迁移早已声明该冲突类型，但 detect_conflict 从未产生——同一口径被
    不同 Owner 修订/多版本并存的场景不覆盖。自我引用防御在前置拦截同 metric_id
    （同一条行），此处是「不同指标行但同码」的历史数据/灰度版本形态。
    """
    cand = {
        "metric_code": "gmv_total",
        "domain": "sales",
        "definition": "sum(amount)",
        "metric_id": 1,
    }
    existing = {
        "metric_code": "gmv_total",
        "domain": "sales",
        "definition": "sum(amount)",
        "metric_id": 2,
    }
    det = detect_conflict(cand, existing)
    assert det is not None
    assert det.conflict_type == ConflictType.VERSION_CONFLICT
    assert det.severity == "soft"
    assert det.block_publish is False


# ---- P1-H：中文 bigram 分词（修复连续中文单 token 致 Jaccard 失效） ----


def test_definition_similarity_chinese_synonym_detected() -> None:
    """中文"同义异名/措辞差异"口径：bigram Jaccard 补编辑距离，def_sim 显著提升。"""
    sim = definition_similarity("门诊挂号人次（含退号）", "门诊挂号人次（不含退号）")
    assert sim >= 0.85  # 修复前纯编辑距离该场景远低于阈值，中文语义同义漏检


def test_detect_chinese_synonym_same_def_diff_name() -> None:
    """中文同义异名指标 → 命中 SAME_DEF_DIFF_NAME（重复建设，软冲突）。"""
    cand = {
        "metric_code": "his_outp_reg_day",
        "domain": "his",
        "definition": "门诊挂号人次（含退号）",
    }
    existing = {
        "metric_code": "his_outp_regcnt_day",
        "domain": "his",
        "definition": "门诊挂号人次（不含退号）",
    }
    det = detect_conflict(cand, existing)
    assert det is not None
    assert det.conflict_type == ConflictType.SAME_DEF_DIFF_NAME


def test_tokens_splits_underscored_codes() -> None:
    """P1-H/下划线修复：带下划线编码按段切分，粒度/单位 token 可被识别。

    修复前 `_SPLIT_RE` 不含 `_`，编码整体成单 token，name_similarity 的 Jaccard
    半腿失效、_GRAIN_TOKENS 交集恒空（GRAIN_UNIT 从未被编码触发）。
    """
    from app.services.conflict.similarity import _tokens

    assert "day" in _tokens("sales_gmv_amount_day")
    assert "gmv" in _tokens("sales_gmv_amount_day")


# ---- P1-D：口径要素归一（维度/过滤/粒度/聚合/依赖/单位参与比对） ----


def test_detect_dimension_diff_not_false_positive() -> None:
    """维度不同但 expression 文本相同 → 不误判「同义重复建设」（富文本对要素敏感）。

    P1-D 修复前 def_sim 只看 expression 文本（相同→1.0→SAME_DEF_DIFF_NAME 误判
    重复建设）；修复后维度并入比对，不再命中 SAME_DEF_DIFF_NAME。
    """
    cand = {
        "metric_code": "sales_gmv_amount_day",
        "domain": "sales",
        "definition": "sum(amount)",
        "definition_json": {"expression": "sum(amount)", "dimensions": ["store"]},
    }
    existing = {
        "metric_code": "sales_gmv_amt_day",
        "domain": "sales",
        "definition": "sum(amount)",
        "definition_json": {"expression": "sum(amount)", "dimensions": ["region"]},
    }
    det = detect_conflict(cand, existing)
    # 核心：不再判为「同义重复建设」（SAME_DEF_DIFF_NAME）
    assert det is None or det.conflict_type != ConflictType.SAME_DEF_DIFF_NAME


def test_detect_same_definition_with_features_matches() -> None:
    """同义口径带相同要素（维度/依赖）→ 仍命中 SAME_DEF_DIFF_NAME（不误伤）。"""
    cand = {
        "metric_code": "sales_gmv_amount_day",
        "domain": "sales",
        "definition": "sum(amount)",
        "definition_json": {
            "expression": "sum(amount)",
            "dimensions": ["store"],
            "dependencies": ["fct_order"],
        },
    }
    existing = {
        "metric_code": "sales_gmv_amt_day",
        "domain": "sales",
        "definition": "sum(amount)",
        "definition_json": {
            "expression": "sum(amount)",
            "dimensions": ["store"],
            "dependencies": ["fct_order"],
        },
    }
    det = detect_conflict(cand, existing)
    assert det is not None
    assert det.conflict_type == ConflictType.SAME_DEF_DIFF_NAME


# ---- P2-J：粒度/单位差异从 definition_json 识别（编码不带粒度词也可） ----


def test_detect_grain_unit_diff_from_definition() -> None:
    """编码不带粒度词、但 definition_json.granularity 不同 → GRAIN_UNIT 软冲突。"""
    cand = {
        "metric_code": "sales_gmv_amount",
        "domain": "sales",
        "definition": "sum(amount)",
        "definition_json": {"expression": "sum(amount)", "granularity": "day"},
    }
    existing = {
        "metric_code": "sales_gmv_amount_m",
        "domain": "sales",
        "definition": "sum(amount)",
        "definition_json": {"expression": "sum(amount)", "granularity": "month"},
    }
    det = detect_conflict(cand, existing)
    assert det is not None
    assert det.conflict_type == ConflictType.GRAIN_UNIT
    assert det.severity == "soft"
    assert det.block_publish is False


def test_detect_grain_unit_diff_from_code_token() -> None:
    """编码带粒度词：_grain_unit_diff 能识别（修复前下划线不切分、交集恒空）。

    修复前 `_SPLIT_RE` 不含 `_`，编码整体成单 token，`_GRAIN_TOKENS` 交集
    永远为空——GRAIN_UNIT 分支从未被编码真正触发（仅靠 definition 补充后生效）。
    端到端触发仍需综合分 ≥0.6（既有门槛），此处验证 token 粒度识别本身已修复。
    """
    from app.services.conflict.similarity import _grain_unit_diff

    assert _grain_unit_diff("sales_gmv_amount_day", "sales_gmv_amount_month") is True
    assert _grain_unit_diff("sales_gmv_amount_day", "sales_gmv_amount_day") is False


def test_detect_no_grain_diff_when_same_granularity() -> None:
    """两侧粒度一致（且无其它冲突）→ 不误报 GRAIN_UNIT。"""
    cand = {
        "metric_code": "sales_gmv_amount",
        "domain": "sales",
        "definition": "sum(amount)",
        "definition_json": {"expression": "sum(amount)", "granularity": "day"},
    }
    existing = {
        "metric_code": "sales_gmv_amount2",
        "domain": "sales",
        "definition": "sum(amount)",
        "definition_json": {"expression": "sum(amount)", "granularity": "day"},
    }
    # 同粒度 + 定义一致 → 实际命中 SAME_DEF_DIFF_NAME（同义不同名），非 GRAIN_UNIT
    det = detect_conflict(cand, existing)
    assert det is not None
    assert det.conflict_type != ConflictType.GRAIN_UNIT


# ---- P2-K：术语/度量同义词接入 name 比对 ----


def test_detect_synonym_equivalence_matches() -> None:
    """编码不同但互为同义词（gmv↔成交总额）→ 名称等价，命中同义软冲突。"""
    cand = {
        "metric_code": "gmv_total",
        "domain": "sales",
        "definition": "sum(amount)",
        "synonyms": ["成交总额", "销售总额"],
    }
    existing = {
        "metric_code": "sales_total_amount",
        "domain": "sales",
        "definition": "sum(amount)",
        "synonyms": ["成交总额"],
    }
    det = detect_conflict(cand, existing)
    assert det is not None
    assert det.conflict_type == ConflictType.SAME_DEF_DIFF_NAME


def test_synonym_equivalence_boosts_name_sim() -> None:
    """同义词等价把 name_sim 抬到 0.95 以上——即使编码字面差异大。"""
    from app.services.conflict.similarity import _name_equivalent

    assert _name_equivalent("gmv_total", "sales_total_amount", ["成交总额"], ["成交总额"])
    # 一方编码命中对方同义词集
    assert _name_equivalent("gmv_total", "sales_total_amount", [], ["gmv_total"])
    assert _name_equivalent("gmv_total", "sales_total_amount", ["sales_total_amount"], [])
    # 无同义词 → 不认为等价
    assert not _name_equivalent("gmv_total", "sales_total_amount", [], [])


# ---- 口径双字段扩展（缺口2）：伪代码/数仓详细口径/下游表差异应敏感 ----


def test_detect_dw_definition_diff_raises_def_sim() -> None:
    """数仓详细口径（dw_definition）不同 → 主体文本相同也不判同义（数仓实现口径敏感）。"""
    cand = {
        "metric_code": "outp_register_cnt_day",
        "domain": "outpatient",
        "definition": "count(register_id)",
        "definition_json": {
            "expression": "count(register_id)",
            "dw_definition": "从 ods_his_register 按 register_id 去重计数",
        },
    }
    existing = {
        "metric_code": "outp_register_person_cnt_day",
        "domain": "outpatient",
        "definition": "count(register_id)",
        "definition_json": {
            "expression": "count(register_id)",
            "dw_definition": "从 ods_his_register 按 patient_id 去重统计就诊人数",
        },
    }
    det = detect_conflict(cand, existing)
    # 主体 expression 相同但数仓实现口径不同 → 不判同义（否则漏判数仓口径冲突）；
    # 可能降级为更弱的「口径相似」软冲突，但绝不判「同义建议合并」。
    assert det is None or det.conflict_type != ConflictType.SAME_DEF_DIFF_NAME


def test_detect_dw_definition_same_still_matches() -> None:
    """数仓详细口径相同 → 主体同义仍命中（不误伤）。"""
    cand = {
        "metric_code": "outp_register_cnt_day",
        "domain": "outpatient",
        "definition": "count(register_id)",
        "definition_json": {
            "expression": "count(register_id)",
            "dw_definition": "从 ods_his_register 按 register_id 去重计数",
        },
    }
    existing = {
        "metric_code": "outp_register_person_cnt_day",
        "domain": "outpatient",
        "definition": "count(register_id)",
        "definition_json": {
            "expression": "count(register_id)",
            "dw_definition": "从 ods_his_register 按 register_id 去重计数",
        },
    }
    det = detect_conflict(cand, existing)
    assert det is not None
    assert det.conflict_type == ConflictType.SAME_DEF_DIFF_NAME


def test_detect_pseudo_definition_diff_raises_def_sim() -> None:
    """伪代码口径（pseudo_definition）不同 → 不判同义（系统开发口径敏感）。"""
    cand = {
        "metric_code": "outp_fee_amount_day",
        "domain": "outpatient",
        "definition": "sum(fee_amount)",
        "definition_json": {
            "expression": "sum(fee_amount)",
            "pseudo_definition": "取门诊收费主表实收金额，按就诊去重汇总",
        },
    }
    existing = {
        "metric_code": "outp_charge_amount_day",
        "domain": "outpatient",
        "definition": "sum(fee_amount)",
        "definition_json": {
            "expression": "sum(fee_amount)",
            "pseudo_definition": "取住院结算表应收金额，按住院号分组累加",
        },
    }
    det = detect_conflict(cand, existing)
    # 伪代码口径不同 → 不再判同义（可降级为弱软冲突）
    assert det is None or det.conflict_type != ConflictType.SAME_DEF_DIFF_NAME


def test_detect_downstream_tables_diff_raises_def_sim() -> None:
    """下游使用表不同 → 不判同义（消费范围敏感）。"""
    cand = {
        "metric_code": "outp_register_cnt_day",
        "domain": "outpatient",
        "definition": "count(register_id)",
        "definition_json": {
            "expression": "count(register_id)",
            "downstream_tables": ["app_rpt_mz_day"],
        },
    }
    existing = {
        "metric_code": "outp_register_cnt_day2",
        "domain": "outpatient",
        "definition": "count(register_id)",
        "definition_json": {
            "expression": "count(register_id)",
            "downstream_tables": ["app_rpt_hospital_wide"],
        },
    }
    det = detect_conflict(cand, existing)
    # 下游使用表不同 → 不再判同义（可降级为弱软冲突）
    assert det is None or det.conflict_type != ConflictType.SAME_DEF_DIFF_NAME
