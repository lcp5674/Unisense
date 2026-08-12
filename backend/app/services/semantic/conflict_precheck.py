"""冲突预检与命名规范校验（对齐 TD §12.3 / spec FR-012/FR-013）。

指标注册时：
1. metric_code 严格校验"域_业务对象_度量_统计周期"4 段格式
2. 保留词检测（test/temp/dummy/demo/tmp/sample/staging/todo）
3. 异步调 conflict 服务预检相似口径（命中→挂 pending_conflict 标记）
"""

from __future__ import annotations

import re
from typing import Any

import structlog

logger = structlog.get_logger("unisense.semantic.conflict_precheck")

# 4 段式 metric_code 正则: 域_业务对象_度量_统计周期
# 每段: 小写字母开头，后跟小写字母或数字
CODE_PATTERN = re.compile(r"^([a-z][a-z0-9]*)(_[a-z][a-z0-9]*){3}$")

# 保留词：命中后软提醒（非硬阻断），但用于命名规范校验
RESERVED_WORDS: frozenset[str] = frozenset({
    "test", "temp", "dummy", "demo", "tmp", "sample", "staging", "todo",
})


class ConflictPrechecker:
    """冲突预检与命名规范校验。

    用法::

        checker = ConflictPrechecker()
        valid, error = checker.validate_code_format("sales_gmv_amount_day")
        if not valid:
            raise ValidationError(error)

        conflict = await checker.precheck("sales_gmv_amount_day", definition_json)
        if conflict:
            # 挂 pending_conflict 标记
    """

    #: 保留词集合（类级暴露，供命名规范校验与外部断言引用）
    RESERVED_WORDS: frozenset[str] = RESERVED_WORDS

    @staticmethod
    def validate_code_format(code: str) -> tuple[bool, str | None]:
        """校验 metric_code 格式：4 段式"域_业务对象_度量_统计周期"。

        Args:
            code: 指标编码。

        Returns:
            (合法, 错误信息): 合法为 True 时错误信息为 None。
        """
        if not code:
            return False, "metric_code 不能为空"

        if not CODE_PATTERN.match(code):
            parts = code.split("_")
            if len(parts) < 4:
                return (
                    False,
                    f"metric_code 须符合 4 段格式（域_业务对象_度量_统计周期），"
                    f"当前仅 {len(parts)} 段",
                )
            if len(parts) > 4:
                return (
                    False,
                    f"metric_code 须符合 4 段格式（域_业务对象_度量_统计周期），"
                    f"当前 {len(parts)} 段过多",
                )
            return False, "metric_code 每段须以小写字母开头，仅含小写字母和数字"

        # 检查保留词（软提醒：不硬阻断，但在校验中提示）
        segments = code.split("_")
        reserved_hits = [s for s in segments if s.lower() in RESERVED_WORDS]
        if reserved_hits:
            hits = ", ".join(reserved_hits)
            return False, f"metric_code 含保留词: {hits}，请使用业务含义明确的命名"

        return True, None

    async def precheck(
        self, metric_code: str, definition_json: dict[str, Any]
    ) -> dict[str, Any] | None:
        """异步调 conflict 服务预检相似口径。

        命中相似口径→返回冲突详情 dict；无冲突→返回 None。

        Args:
            metric_code: 指标编码。
            definition_json: 口径定义。

        Returns:
            冲突详情或 None。
        """
        # TODO: 调用 conflict 服务 check 接口（当前为占位实现，后续由 US4 补全）
        return None
