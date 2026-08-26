"""SQL 智能推断评测 CLI（薄入口，核心在 app.services.semantic.sql_infer_eval）。

用法：
- pytest 回归：``pytest backend/tests/eval/test_sql_infer_eval.py``（断言 100% 精确匹配）
- 量化报告：``cd backend && python -m tests.eval.sql_infer_eval``
"""

from __future__ import annotations

from app.services.semantic.sql_infer_eval.runner import format_report, run_eval


def main() -> None:
    print(format_report(run_eval()))


if __name__ == "__main__":
    main()
