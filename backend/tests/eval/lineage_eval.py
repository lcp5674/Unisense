"""血缘评测运行器——精确率 / 召回率 / 准确率的量化计算（纯函数）。

对 ``golden_dataset.GOLDEN`` 每条用例运行解析器，与人工核对的期望血缘对比，
输出：
- 表级血缘（TE）精确率 / 召回率
- 字段级血缘（FE）精确率 / 召回率
- 上游依赖（UD，纯 SELECT 无落点场景）精确率 / 召回率
- 用例级完全匹配率（exact match rate）

指标定义（标准信息检索口径）：
- 精确率 precision = |预测 ∩ 期望| / |预测|（预测为空且期望为空视为 1.0）
- 召回率 recall    = |预测 ∩ 期望| / |期望|（期望为空且预测为空视为 1.0）
- 用例完全匹配 exact = 预测集合 == 期望集合（TE、FE 均相等；UD 场景则 UD 相等）

用法：
- pytest 回归：``pytest backend/tests/eval``（断言 100% 精确匹配）
- 量化报告：``cd backend && python -m tests.eval.lineage_eval``
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tests.eval.golden_dataset import GOLDEN, GoldenCase

from app.services.lineage.parser import (
    extract_field_lineage,
    extract_table_lineage,
    extract_upstream_deps,
)


@dataclass(frozen=True)
class CaseMetrics:
    """单条用例的评测指标。"""

    case_id: str
    dialect: str
    #: 该用例是否完全匹配（TE+FE 或 UD 全部精确相等）。
    exact: bool
    te_precision: float | None = None
    te_recall: float | None = None
    fe_precision: float | None = None
    fe_recall: float | None = None
    ud_precision: float | None = None
    ud_recall: float | None = None
    #: 诊断明细：多余/缺失的边（用于失败定位）。
    extra_te: frozenset[str] = frozenset()
    missing_te: frozenset[str] = frozenset()
    extra_fe: frozenset[str] = frozenset()
    missing_fe: frozenset[str] = frozenset()
    extra_ud: frozenset[str] = frozenset()
    missing_ud: frozenset[str] = frozenset()


@dataclass
class EvalReport:
    """评测集汇总报告。"""

    total: int
    exact_count: int
    cases: list[CaseMetrics] = field(default_factory=list)
    te_precision: float | None = None
    te_recall: float | None = None
    fe_precision: float | None = None
    fe_recall: float | None = None
    ud_precision: float | None = None
    ud_recall: float | None = None

    @property
    def exact_rate(self) -> float:
        """用例级完全匹配率（0~1）。"""
        return self.exact_count / self.total if self.total else 1.0


def _precision(pred: set[str], expected: set[str]) -> float:
    """精确率：预测边中正确占比（预测与期望均空视为 1.0）。"""
    if not pred:
        return 1.0 if not expected else 0.0
    return len(pred & expected) / len(pred)


def _recall(pred: set[str], expected: set[str]) -> float:
    """召回率：期望边中被预测出的占比（期望与预测均空视为 1.0）。"""
    if not expected:
        return 1.0 if not pred else 0.0
    return len(pred & expected) / len(expected)


def _fmt_te(edges: object) -> set[str]:
    """表级边 → ``{src->tgt}`` 集合。"""
    return {f"{e.source}->{e.target}" for e in edges}


def _fmt_fe(edges: object) -> set[str]:
    """字段级边 → ``{src.col->tgt.col}`` 集合（降级边不含列时以 ``(degraded)`` 记）。"""
    out: set[str] = set()
    for e in edges:
        if not (e.source_table and e.source_column and e.target_table and e.target_column):
            out.add(f"(degraded:{e.target_table}.{e.target_column})")
            continue
        out.add(f"{e.source_table}.{e.source_column}->{e.target_table}.{e.target_column}")
    return out


def _fmt_ud(deps: object) -> tuple[set[str], set[str]]:
    """上游依赖 → ``(tables, fields)`` 集合。"""
    return set(deps.tables), set(deps.fields)


def evaluate_case(case: GoldenCase) -> CaseMetrics:
    """运行解析器并对比期望，产出单条用例指标。

    纯 SELECT 无落点场景（声明了 ``expected_ud_tables``）评测上游依赖；
    否则评测表级与字段级血缘。
    """
    is_upstream = bool(case.expected_ud_tables)
    if is_upstream:
        deps = extract_upstream_deps(case.sql, case.dialect)
        pred_tables, pred_fields = _fmt_ud(deps)
        ud_prec = _precision(pred_tables, case.expected_ud_tables)
        ud_rec = _recall(pred_tables, case.expected_ud_tables)
        ud_prec_f = _precision(pred_fields, case.expected_ud_fields)
        ud_rec_f = _recall(pred_fields, case.expected_ud_fields)
        exact = pred_tables == case.expected_ud_tables and pred_fields == case.expected_ud_fields
        # 上游依赖表/字段的指标合并到 ud_precision/ud_recall（加权平均各半）。
        ud_prec_avg = (ud_prec + ud_prec_f) / 2
        ud_rec_avg = (ud_rec + ud_rec_f) / 2
        return CaseMetrics(
            case_id=case.case_id,
            dialect=case.dialect,
            exact=exact,
            ud_precision=ud_prec_avg,
            ud_recall=ud_rec_avg,
            extra_ud=frozenset(pred_tables - case.expected_ud_tables)
            | frozenset(pred_fields - case.expected_ud_fields),
            missing_ud=frozenset(case.expected_ud_tables - pred_tables)
            | frozenset(case.expected_ud_fields - pred_fields),
        )

    pred_te = _fmt_te(extract_table_lineage(case.sql, case.dialect, target_table=case.target_table))
    pred_fe = _fmt_fe(extract_field_lineage(case.sql, case.dialect, target_table=case.target_table))
    te_prec = _precision(pred_te, case.expected_te)
    te_rec = _recall(pred_te, case.expected_te)
    fe_prec = _precision(pred_fe, case.expected_fe)
    fe_rec = _recall(pred_fe, case.expected_fe)
    exact = pred_te == case.expected_te and pred_fe == case.expected_fe
    return CaseMetrics(
        case_id=case.case_id,
        dialect=case.dialect,
        exact=exact,
        te_precision=te_prec,
        te_recall=te_rec,
        fe_precision=fe_prec,
        fe_recall=fe_rec,
        extra_te=frozenset(pred_te - case.expected_te),
        missing_te=frozenset(case.expected_te - pred_te),
        extra_fe=frozenset(pred_fe - case.expected_fe),
        missing_fe=frozenset(case.expected_fe - pred_fe),
    )


def _macro_avg(values: list[float]) -> float | None:
    """宏平均（空列表返回 None）。"""
    return sum(values) / len(values) if values else None


def run_eval() -> EvalReport:
    """运行整个评测集，汇总指标。"""
    cases = [evaluate_case(c) for c in GOLDEN]
    te_p = [m.te_precision for m in cases if m.te_precision is not None]
    te_r = [m.te_recall for m in cases if m.te_recall is not None]
    fe_p = [m.fe_precision for m in cases if m.fe_precision is not None]
    fe_r = [m.fe_recall for m in cases if m.fe_recall is not None]
    ud_p = [m.ud_precision for m in cases if m.ud_precision is not None]
    ud_r = [m.ud_recall for m in cases if m.ud_recall is not None]
    return EvalReport(
        total=len(cases),
        exact_count=sum(1 for m in cases if m.exact),
        cases=cases,
        te_precision=_macro_avg(te_p),
        te_recall=_macro_avg(te_r),
        fe_precision=_macro_avg(fe_p),
        fe_recall=_macro_avg(fe_r),
        ud_precision=_macro_avg(ud_p),
        ud_recall=_macro_avg(ud_r),
    )


def _pct(value: float | None) -> str:
    """百分比格式化（None 显示 N/A）。"""
    return f"{value * 100:.1f}%" if value is not None else "N/A"


def format_report(report: EvalReport) -> str:
    """生成人类可读的评测报告。"""
    lines = [
        "血缘评测集报告",
        "=" * 40,
        f"用例总数: {report.total}",
        f"完全匹配: {report.exact_count}/{report.total} ({_pct(report.exact_rate)})",
        "",
        "维度      精确率   召回率",
        f"表级 TE   {_pct(report.te_precision):<8} {_pct(report.te_recall)}",
        f"字段级 FE {_pct(report.fe_precision):<8} {_pct(report.fe_recall)}",
        f"上游 UD   {_pct(report.ud_precision):<8} {_pct(report.ud_recall)}",
    ]
    failures = [m for m in report.cases if not m.exact]
    if failures:
        lines.append("")
        lines.append("失败用例:")
        for m in failures:
            lines.append(f"  - {m.case_id} ({m.dialect})")
            if m.extra_te:
                lines.append(f"      多余表级边: {sorted(m.extra_te)}")
            if m.missing_te:
                lines.append(f"      缺失表级边: {sorted(m.missing_te)}")
            if m.extra_fe:
                lines.append(f"      多余字段边: {sorted(m.extra_fe)}")
            if m.missing_fe:
                lines.append(f"      缺失字段边: {sorted(m.missing_fe)}")
            if m.extra_ud:
                lines.append(f"      多余上游依赖: {sorted(m.extra_ud)}")
            if m.missing_ud:
                lines.append(f"      缺失上游依赖: {sorted(m.missing_ud)}")
    else:
        lines.append("")
        lines.append("无失败用例，全部精确匹配。")
    return "\n".join(lines)


def main() -> None:
    """CLI 入口：``cd backend && python -m tests.eval.lineage_eval``。"""
    print(format_report(run_eval()))


if __name__ == "__main__":
    main()
