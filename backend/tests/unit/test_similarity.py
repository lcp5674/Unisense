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
