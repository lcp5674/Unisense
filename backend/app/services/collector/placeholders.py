"""采集元数据「无注释」占位串（连接器归一化 + 推断跳过共用）。

Spark Thrift Server 对无 COMMENT 的列，在 ``DESCRIBE`` 第三列返回占位串
``"from deserializer"``（Spark StructType 反序列化时的默认 comment）。若不识别，
这些假注释会被当作真实 DDL 注释：批量字段推断会误判「已有注释」而全部跳过，
描述缺失治理也出现「面板显示缺失、推断却全跳过」的矛盾。

连接器（hive.py）把占位串归一化为空串，避免污染 schema_json；
批量推断（collector.py infer_descriptions_batch）把占位串视为「无注释」不跳过，
防御历史遗留数据。
"""

from __future__ import annotations

#: 已知的「无注释」占位串（比对前 strip + lower）。
PLACEHOLDER_COMMENTS: frozenset[str] = frozenset({"from deserializer"})


def is_effective_comment(comment: str | None) -> bool:
    """comment 是否为「有效 DDL 注释」（排除空/纯空白与采集占位串）。

    纯空白（如 ``"  "``）也视为无效：docstring 声称排除纯空白，但旧实现漏了
    ``strip()`` 后为空的情形，会把纯空格注释当成有效（描述缺失治理计数偏差）。
    SQL 端（描述覆盖汇总 JSON_TABLE 判定）用 ``TRIM(cm) <> ''`` 表达同一语义。
    """
    if not comment or not comment.strip():
        return False
    return comment.strip().lower() not in PLACEHOLDER_COMMENTS
