"""血缘评测集回归测试——断言 parser 在全部 golden 用例上精确匹配。

评测集是「人工核对语义的真实生产场景」，此处把「100% 精确率 + 召回率」固化为
回归门槛：任何 parser 改动若引入血缘回归（漏边/多边/错挂），测试立即标红并
在断言消息中给出精确的失败用例与多余/缺失边清单（经 ``format_report``）。
"""

from __future__ import annotations

from tests.eval.golden_dataset import GOLDEN
from tests.eval.lineage_eval import format_report, run_eval


def test_eval_all_cases_exact_match() -> None:
    """全部 golden 用例必须精确匹配（TE+FE 或 UD 均相等）。

    失败时断言消息输出完整评测报告，精确定位：哪个场景、多余哪些边、
    缺失哪些边。
    """
    report = run_eval()
    failures = [m for m in report.cases if not m.exact]
    assert not failures, (
        f"血缘评测集存在 {len(failures)}/{report.total} 个用例未精确匹配：\n{format_report(report)}"
    )


def test_eval_report_shape_and_perfect_scores() -> None:
    """报告形状完整且各维度指标均为 100%。"""
    report = run_eval()
    assert report.total == len(GOLDEN) == report.exact_count
    assert report.exact_rate == 1.0
    # 有边场景（非 UD）必须产出 TE/FE 维度指标
    assert report.te_precision == 1.0
    assert report.te_recall == 1.0
    assert report.fe_precision == 1.0
    assert report.fe_recall == 1.0
    # 纯 SELECT 场景必须产出 UD 维度指标
    assert report.ud_precision == 1.0
    assert report.ud_recall == 1.0
