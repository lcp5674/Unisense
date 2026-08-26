"""SQL 智能推断评测运行器——度量召回率 / 精确率 / 端到端完全匹配率的量化计算。

对 ``sql_infer_dataset.GOLDEN`` 每条用例运行 ``parse_sql_profile``（规则解析），
与人工核对的期望画像对比，输出：
- 度量级召回率 / 精确率（该识别出的度量是否识别出、识别出的是否正确）
- 表级召回率 / 精确率（源表识别）
- 周期匹配率
- 用例级完全匹配率（exact match rate，度量+表+周期全等）

指标定义（标准信息检索口径）：
- 精确率 precision = |预测 ∩ 期望| / |预测|（预测为空且期望为空视为 1.0）
- 召回率 recall    = |预测 ∩ 期望| / |期望|（期望为空且预测为空视为 1.0）
- 用例完全匹配 exact = 预测度量签名集 == 期望度量签名集
                         且 预测源表集 == 期望源表集 且 预测周期 == 期望周期

度量签名：``列名|聚合``（聚合为空按 ``DERIVED``），期望声明了别名/源表时
追加 ``alias:xxx``/``table:xxx`` 参与区分（同列多语义靠别名消歧）。

用法：
- pytest 回归：``pytest backend/tests/eval/test_sql_infer_eval.py``（断言 100% 精确匹配）
- 量化报告：``cd backend && python -m tests.eval.sql_infer_eval``
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.services.semantic.sql_infer import parse_sql_profile
from app.services.semantic.sql_infer_eval.dataset import GOLDEN, SqlInferCase
from app.services.semantic.sql_split import _period_from_profile


@dataclass(frozen=True)
class CaseMetrics:
    """单条用例的评测指标。"""

    case_id: str
    dialect: str
    exact: bool
    measure_precision: float | None = None
    measure_recall: float | None = None
    table_precision: float | None = None
    table_recall: float | None = None
    period_match: bool | None = None
    #: 完整实际解析结果（前端"期望 vs 实际"对照展示用，而非仅差异）。
    pred_measures: frozenset[str] = frozenset()
    pred_tables: frozenset[str] = frozenset()
    #: 结构化实际度量（column/agg/alias/table + 签名），与 ``expected_measures_detail``
    #: 对称供前端逐字段展示（替代 `|` 拼接签名串）。
    pred_measures_detail: tuple[dict[str, object], ...] = ()
    #: 诊断明细（用于失败定位）。
    extra_measures: frozenset[str] = frozenset()
    missing_measures: frozenset[str] = frozenset()
    extra_tables: frozenset[str] = frozenset()
    missing_tables: frozenset[str] = frozenset()
    pred_period: str | None = None
    expected_period: str | None = None


@dataclass
class EvalReport:
    """评测集汇总报告。"""

    total: int
    exact_count: int
    cases: list[CaseMetrics] = field(default_factory=list)
    measure_precision: float | None = None
    measure_recall: float | None = None
    table_precision: float | None = None
    table_recall: float | None = None
    period_match_rate: float | None = None

    @property
    def exact_rate(self) -> float:
        """用例级完全匹配率（0~1）。"""
        return self.exact_count / self.total if self.total else 1.0


def _precision(pred: set[str], expected: set[str]) -> float:
    """精确率：预测中正确占比（预测与期望均空视为 1.0）。"""
    if not pred:
        return 1.0 if not expected else 0.0
    return len(pred & expected) / len(pred)


def _recall(pred: set[str], expected: set[str]) -> float:
    """召回率：期望中被预测出的占比（期望与预测均空视为 1.0）。"""
    if not expected:
        return 1.0 if not pred else 0.0
    return len(pred & expected) / len(expected)


def _measure_signature(m: dict[str, object]) -> str:
    """预测度量 → 签名（与期望同构：列|聚合，别名/源表可选追加）。"""
    col = str(m.get("column") or "").lower()
    agg = m.get("agg") or "DERIVED"
    parts = [col, str(agg)]
    alias = m.get("alias")
    if alias:
        parts.append(f"alias:{str(alias).lower()}")
    table = m.get("table")
    if table:
        parts.append(f"table:{str(table).lower()}")
    return "|".join(parts)


def evaluate_case(case: SqlInferCase) -> CaseMetrics:
    """运行规则解析并对比期望，产出单条用例指标。"""
    profile = parse_sql_profile(case.sql)
    pred_measures = {_measure_signature(m) for m in profile.measures}
    exp_measures = {em.signature() for em in case.expected_measures}
    pred_tables = {t.lower() for t in profile.source_tables}
    exp_tables = {t.lower() for t in case.expected_tables}
    pred_period = _period_from_profile(profile)
    period_match = pred_period == case.expected_period
    mp = _precision(pred_measures, exp_measures)
    mr = _recall(pred_measures, exp_measures)
    tp = _precision(pred_tables, exp_tables)
    tr = _recall(pred_tables, exp_tables)
    exact = (
        pred_measures == exp_measures
        and pred_tables == exp_tables
        and period_match
    )
    pred_measures_detail = tuple(
        {
            "column": str(m.get("column") or ""),
            "agg": m.get("agg") or None,
            "alias": m.get("alias") or None,
            "table": m.get("table") or None,
            "signature": _measure_signature(m),
        }
        for m in profile.measures
    )
    return CaseMetrics(
        case_id=case.case_id,
        dialect=case.dialect,
        exact=exact,
        measure_precision=mp,
        measure_recall=mr,
        table_precision=tp,
        table_recall=tr,
        period_match=period_match,
        pred_measures=frozenset(pred_measures),
        pred_tables=frozenset(pred_tables),
        pred_measures_detail=pred_measures_detail,
        extra_measures=frozenset(pred_measures - exp_measures),
        missing_measures=frozenset(exp_measures - pred_measures),
        extra_tables=frozenset(pred_tables - exp_tables),
        missing_tables=frozenset(exp_tables - pred_tables),
        pred_period=pred_period,
        expected_period=case.expected_period,
    )


def _macro_avg(values: list[float]) -> float | None:
    """宏平均（空列表返回 None）。"""
    return sum(values) / len(values) if values else None


def run_eval(
    samples: Sequence[SqlInferCase] | None = None,
) -> EvalReport:
    """运行评测集，汇总指标。

    Args:
        samples: 参与评测的用例序列。缺省/None 用内置基线 ``GOLDEN``（CLI/pytest
            门禁路径）；传入时按给定序列运行（service 层合并内置 + DB 自定义，
            让成功率随样本持续扩充而度量）。
    """
    cases = [evaluate_case(c) for c in (samples if samples is not None else GOLDEN)]
    mp = [m.measure_precision for m in cases if m.measure_precision is not None]
    mr = [m.measure_recall for m in cases if m.measure_recall is not None]
    tp = [m.table_precision for m in cases if m.table_precision is not None]
    tr = [m.table_recall for m in cases if m.table_recall is not None]
    pm = [m for m in cases if m.period_match is not None]
    return EvalReport(
        total=len(cases),
        exact_count=sum(1 for m in cases if m.exact),
        cases=cases,
        measure_precision=_macro_avg(mp),
        measure_recall=_macro_avg(mr),
        table_precision=_macro_avg(tp),
        table_recall=_macro_avg(tr),
        period_match_rate=sum(1 for m in pm if m.period_match) / len(pm) if pm else None,
    )


def _pct(value: float | None) -> str:
    """百分比格式化（None 显示 N/A）。"""
    return f"{value * 100:.1f}%" if value is not None else "N/A"


def report_to_dict(report: EvalReport) -> dict[str, object]:
    """评测报告 → JSON 可序列化 dict（API 层返回给前端可视化）。"""
    return {
        "total": report.total,
        "exact_count": report.exact_count,
        "exact_rate": round(report.exact_rate, 4),
        "measure_precision": report.measure_precision,
        "measure_recall": report.measure_recall,
        "table_precision": report.table_precision,
        "table_recall": report.table_recall,
        "period_match_rate": report.period_match_rate,
        "cases": [
            {
                "case_id": m.case_id,
                "dialect": m.dialect,
                "exact": m.exact,
                "measure_precision": m.measure_precision,
                "measure_recall": m.measure_recall,
                "table_precision": m.table_precision,
                "table_recall": m.table_recall,
                "period_match": m.period_match,
                "pred_measures": sorted(m.pred_measures),
                "pred_tables": sorted(m.pred_tables),
                "pred_measures_detail": [
                    dict(d) for d in m.pred_measures_detail
                ],
                "extra_measures": sorted(m.extra_measures),
                "missing_measures": sorted(m.missing_measures),
                "extra_tables": sorted(m.extra_tables),
                "missing_tables": sorted(m.missing_tables),
                "pred_period": m.pred_period,
                "expected_period": m.expected_period,
            }
            for m in report.cases
        ],
    }


def dataset_to_dict(
    samples: Sequence[SqlInferCase] | None = None,
) -> list[dict[str, object]]:
    """评测集样本清单（前端逐样本展示 SQL/期望画像/说明/来源标记）。

    Args:
        samples: 待序列化的用例序列；缺省用内置基线 ``GOLDEN``。合并场景由
            service 层传入（内置 + DB 自定义），此处按 case_id 是否属内置基线
            标 ``source``（builtin/custom）——前端据此决定只读/可编辑。
    """
    cases = samples if samples is not None else GOLDEN
    builtin_ids = {c.case_id for c in GOLDEN}
    out: list[dict[str, object]] = []
    for c in cases:
        out.append(
            {
                "case_id": c.case_id,
                "dialect": c.dialect,
                "note": c.note,
                "sql": c.sql,
                "expected_measures": [em.signature() for em in c.expected_measures],
                # 结构化期望度量（CRUD 弹窗回填用）：{column, agg, alias, table}
                "expected_measures_detail": [
                    {
                        "column": em.column,
                        "agg": em.agg,
                        "alias": em.alias,
                        "table": em.table,
                    }
                    for em in c.expected_measures
                ],
                "expected_tables": list(c.expected_tables),
                "expected_period": c.expected_period,
                "source": "builtin" if c.case_id in builtin_ids else "custom",
            }
        )
    return out


def format_report(report: EvalReport) -> str:
    """生成人类可读的评测报告。"""
    lines = [
        "SQL 智能推断评测集报告",
        "=" * 44,
        f"用例总数: {report.total}",
        f"完全匹配: {report.exact_count}/{report.total} ({_pct(report.exact_rate)})",
        "",
        "维度        精确率   召回率",
        f"度量级     {_pct(report.measure_precision):<8} {_pct(report.measure_recall)}",
        f"表级       {_pct(report.table_precision):<8} {_pct(report.table_recall)}",
        f"周期匹配率: {_pct(report.period_match_rate)}",
    ]
    failures = [m for m in report.cases if not m.exact]
    if failures:
        lines.append("")
        lines.append("失败用例:")
        for m in failures:
            lines.append(f"  - {m.case_id} ({m.dialect})")
            if m.missing_measures:
                lines.append(f"      缺失度量: {sorted(m.missing_measures)}")
            if m.extra_measures:
                lines.append(f"      多余度量: {sorted(m.extra_measures)}")
            if m.missing_tables:
                lines.append(f"      缺失表: {sorted(m.missing_tables)}")
            if m.extra_tables:
                lines.append(f"      多余表: {sorted(m.extra_tables)}")
            if not m.period_match:
                lines.append(
                    f"      周期不符: 预测={m.pred_period} 期望={m.expected_period}"
                )
    else:
        lines.append("")
        lines.append("无失败用例，全部精确匹配。")
    return "\n".join(lines)


def main() -> None:
    """CLI 入口：``cd backend && python -m tests.eval.sql_infer_eval``。"""
    print(format_report(run_eval()))


if __name__ == "__main__":
    main()
