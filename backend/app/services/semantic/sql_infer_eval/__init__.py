"""SQL 智能推断评测核心（数据集 + 运行器 + 运行记录服务）。

评测集是「解析成功率」的可度量载体：
- ``dataset.py``：真实生产 SQL 与方言样本 + 人工核对期望
- ``runner.py``：规则解析 vs 期望 → 精确率/召回率/完全匹配率（纯函数）
- ``service.py``：评测运行记录持久化（趋势可视化数据源）
"""

from app.services.semantic.sql_infer_eval.dataset import GOLDEN, ExpectedMeasure, SqlInferCase
from app.services.semantic.sql_infer_eval.runner import (
    EvalReport,
    evaluate_case,
    format_report,
    report_to_dict,
    run_eval,
)

__all__ = [
    "EvalReport",
    "ExpectedMeasure",
    "GOLDEN",
    "SqlInferCase",
    "evaluate_case",
    "format_report",
    "report_to_dict",
    "run_eval",
]
