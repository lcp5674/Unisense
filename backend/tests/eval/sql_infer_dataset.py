"""SQL 智能推断评测集数据（薄 re-export，核心在 app.services.semantic.sql_infer_eval.dataset）。

保留 ``tests.eval.sql_infer_dataset`` 导入路径兼容（历史 CLI/测试引用）。
"""

from app.services.semantic.sql_infer_eval.dataset import (
    GOLDEN,
    ExpectedMeasure,
    SqlInferCase,
    get_case,
)

__all__ = ["ExpectedMeasure", "GOLDEN", "SqlInferCase", "get_case"]
