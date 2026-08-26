"""SQL 智能推断评测集的 pytest 回归门禁：断言评测集 100% 精确匹配。

任何 ``parse_sql_profile`` 改动导致度量/源表/周期识别退化，本测试即失败——
这是「解析成功率不回归」的自动化保障（对齐血缘 lineage_eval 回归模式）。
"""

from __future__ import annotations

from app.services.semantic.sql_infer_eval.dataset import GOLDEN, get_case
from app.services.semantic.sql_infer_eval.runner import evaluate_case, run_eval


def test_eval_dataset_non_empty() -> None:
    """评测集至少包含 5 个样本（覆盖真实 ETL + 多方言）。"""
    assert len(GOLDEN) >= 5
    dialects = {c.dialect for c in GOLDEN}
    assert len(dialects) >= 5


def test_eval_100_percent_exact_match() -> None:
    """全部用例精确匹配（度量 + 源表 + 周期全等）——成功率基线门禁。"""
    report = run_eval()
    failures = [m for m in report.cases if not m.exact]
    assert not failures, (
        f"评测集 {report.exact_count}/{report.total} 精确匹配，失败用例: "
        + "; ".join(f"{m.case_id}(缺失度量={sorted(m.missing_measures)}, "
                    f"多余度量={sorted(m.extra_measures)})" for m in failures)
    )


def test_doctor_active_month_measures() -> None:
    """真实医生月活 ETL：双度量（含 COALESCE 包裹 CASE 条件去重）同列按别名区分。"""
    case = get_case("doctor_active_month")
    assert case is not None
    m = evaluate_case(case)
    assert m.exact
    assert m.measure_recall == 1.0
    assert m.measure_precision == 1.0


def test_spark_window_derived_measure() -> None:
    """Spark 窗口函数派生列：agg=None 派生度量，与普通聚合并存。"""
    case = get_case("spark_window")
    assert case is not None
    m = evaluate_case(case)
    assert m.exact
    assert "month_id|DERIVED|alias:month_total" not in m.missing_measures


def test_trino_approx_aggregate_normalize() -> None:
    """Trino approx_distinct→COUNT_DISTINCT / approx_percentile→PERCENTILE 归一。"""
    case = get_case("trino_approx")
    assert case is not None
    m = evaluate_case(case)
    assert m.exact
    assert m.missing_measures == frozenset()
