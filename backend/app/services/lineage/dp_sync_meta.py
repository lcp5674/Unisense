"""dp 调度血缘同步元数据目录（任务/节点类型枚举 + 排除默认规则）。

类型枚举的真实全集以 dp 元库为准——本模块只承载**已实据内置映射**
（来自对 dp 生产数据的观测），并在配置的数据源可达时通过 DISTINCT+COUNT
探测把真实存在的类型合并进目录（未内置的类型显式标注「未识别」，不编造
语义），保证配置选项框覆盖全部枚举值。

排除规则语义（修掉「自定义空列表关闭默认」的历史 bug）：
    ``merged_exclude_table_patterns`` = 内置默认恒生效 + 自定义追加去重。
    配置留空（None/[]）≠ 关闭默认，而是「仅用内置默认」。
"""

from __future__ import annotations

import re
from typing import Any

from app.services.lineage.dp_sync_parser import DEFAULT_EXCLUDE_TABLE_PATTERNS

#: dispatch_task.type 任务开发类型映射（依据：列注释 + 节点构成实据，2026-09 连库观测）。
#: 列注释：`任务开发类型 0：同步类型. 1：数据抽取`。
#: 其余值语义由该类型任务的节点构成（dispatch_task_step.task_step_type 分布）归纳：
#:   - 3：节点几乎全为 Shell(step 3) → Shell 驱动任务
#:   - 4：节点混合 Hive/直连 SQL/Oracle 稽核(step 4/5/6/7) → 混合加工任务
#:   - 10：节点全为 DataX(step 2) → DataX 同步任务
#:   - 15：节点含 HTTP 接口同步(step 15) → 接口同步/上传任务
DP_TASK_TYPES: dict[int, str] = {
    0: "同步任务",
    1: "数据抽取（SQL 加工）",
    3: "Shell 任务",
    4: "混合加工任务",
    10: "DataX 同步任务",
    15: "接口同步任务",
}

#: dispatch_task_step.task_step_type 节点类型映射（依据：列注释 + script 形态实据，
#: 2026-09 连库观测）。解析支持见 dp_sync_typed（2/3/9/15 非 SQL 形态均有
#: 类型化解析器；4/5/6/7 走 SQL 解析并切方言）。
#: 列注释：`任务类型 2:datax; 3:shell; 7:hive`。
#: 其余值语义由 script 内容归纳：
#:   - 4：直连库 DML 语句（delete/insert 非 Hive） → SQL 执行脚本（mysql 方言）
#:   - 5：TRUNCATE 清表语句 → 清表脚本
#:   - 6：Oracle 语法（declare/dba_views/PLSQL） → Oracle SQL/PLSQL 脚本（oracle 方言）
#:   - 9：script 为纯数字（上报配置 ID），task_node_type=6(上报) → 上报配置节点（no_flow）
#:   - 15：JSON 接口同步配置（hiveDbName/mysqlDbName/url） → 接口同步配置（mysql↔hive 边）
DP_STEP_TYPES: dict[int, str] = {
    2: "DataX 同步",
    3: "Shell 脚本",
    4: "SQL 执行脚本",
    5: "清表脚本（TRUNCATE）",
    6: "Oracle SQL/PLSQL 脚本",
    7: "Hive/Spark SQL",
    9: "上报配置节点",
    15: "接口同步配置",
}


def merged_exclude_table_patterns(custom: list[str] | None) -> list[str]:
    """内置默认排除恒生效 + 自定义追加（去重，保留顺序）。"""
    merged = list(DEFAULT_EXCLUDE_TABLE_PATTERNS)
    for p in custom or []:
        s = str(p).strip()
        if s and s not in merged:
            merged.append(s)
    return merged


def catalog_with_counts(
    builtin: dict[int, str], counts: dict[int, int]
) -> list[dict[str, Any]]:
    """合并内置类型映射与探测计数，产出选项目录。

    探测到但未内置的值标注「未识别（可在此范围补充过滤或按需扩展映射）」；
    内置但探测未出现（0 条）仍保留（历史类型可能当前无数据）。
    """
    keys = sorted(set(builtin) | set(counts))
    items: list[dict[str, Any]] = []
    for k in keys:
        label = builtin.get(k)
        items.append(
            {
                "value": k,
                "label": label if label is not None else f"类型 {k}（未识别）",
                "known": label is not None,
                "count": counts.get(k, 0),
            }
        )
    return items


def validate_regex(pattern: str) -> str | None:
    """校验单条正则语法，返回错误信息（合法返回 None）。"""
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"正则不合法：{exc}"
    return None


def count_regex_matches(
    tables: list[str],
    patterns: list[str],
    max_samples: int = 8,
) -> dict[str, Any]:
    """对表名集合统计命中任一正则的表（去重样本）。

    返回 ``{total, matched, matched_tables, samples, invalid_patterns}``。
    ``invalid_patterns`` 为 ``[{pattern, error}]``；语法非法的正则不参与匹配。
    """
    compiled: list[tuple[str, re.Pattern]] = []
    invalid: list[dict[str, str]] = []
    for p in patterns:
        err = validate_regex(p)
        if err is not None:
            invalid.append({"pattern": p, "error": err})
            continue
        compiled.append((p, re.compile(p)))
    matched_tables: list[str] = []
    samples: list[dict[str, str]] = []
    for t in sorted(set(tables)):
        hit = None
        for pat, rx in compiled:
            if rx.search(t):
                hit = pat
                break
        if hit is not None:
            matched_tables.append(t)
            if len(samples) < max_samples:
                samples.append({"table": t, "pattern": hit})
    return {
        "total": len(set(tables)),
        "matched": len(matched_tables),
        "matched_tables": matched_tables,
        "samples": samples,
        "invalid_patterns": invalid,
    }
