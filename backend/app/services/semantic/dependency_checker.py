"""依赖指标递归校验与 DAG 环检测（对齐 TD §12.3 / spec FR-010/FR-011）。

派生/复合指标发布前，递归校验其依赖的所有指标均处于 PUBLISHED 状态且非 DEPRECATED，
并做 DERIVED_FROM 有向图环检测（DFS 三色标记法），防止循环依赖导致查询无限递归。
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric import Metric

logger = structlog.get_logger("unisense.semantic.dependency_checker")


class DependencyChecker:
    """依赖指标递归校验与 DAG 环检测。

    用法::

        checker = DependencyChecker(db)
        # 检查依赖指标是否全部 PUBLISHED 且非 DEPRECATED
        unpublished = await checker.check_dependencies_published(definition_json)
        if unpublished:
            raise BusinessError(f"依赖指标未发布: {unpublished}")

        # 检测循环依赖
        cycle = await checker.detect_cycle(metric_code, definition_json)
        if cycle:
            raise BusinessError(f"检测到循环依赖: {'→'.join(cycle)}")
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def check_dependencies_published(self, definition_json: dict[str, Any]) -> list[str]:
        """递归校验依赖指标均 PUBLISHED 且非 DEPRECATED。

        Args:
            definition_json: 指标口径定义，含 dependencies 列表（依赖指标 metric_code 列表）。

        Returns:
            未发布或已废弃的依赖指标 code 列表（空列表 = 全部通过）。
        """
        dependencies: list[str] = definition_json.get("dependencies", [])
        if not dependencies:
            return []

        # 只校验看起来是 metric_code 的依赖（以小写字母开头的 4 段式编码）
        metric_deps = [d for d in dependencies if self._is_metric_code(d)]
        if not metric_deps:
            return []

        unpublished: list[str] = []
        visited: set[str] = set()

        await self._check_recursive(metric_deps, visited, unpublished)
        return unpublished

    async def _check_recursive(
        self, deps: list[str], visited: set[str], unpublished: list[str]
    ) -> None:
        """递归检查依赖列表。"""
        for dep_code in deps:
            if dep_code in visited:
                continue
            visited.add(dep_code)

            if not self._is_metric_code(dep_code):
                continue

            metric = await self._get_metric_by_code(dep_code)
            if metric is None:
                unpublished.append(dep_code)
                continue
            if metric.status == "DEPRECATED":
                unpublished.append(dep_code)
                continue
            if metric.status not in ("PUBLISHED", "EXPERIMENTAL"):
                # PENDING_CONFIRMATION 的指标也有 PUBLISHED 的 metric.status，
                # 允许依赖（消费的是 CURRENT 版本）
                unpublished.append(dep_code)
                continue

            # 递归检查依赖的依赖
            sub_deps = metric.definition_json.get("dependencies", [])
            metric_sub_deps = [d for d in sub_deps if self._is_metric_code(d)]
            if metric_sub_deps:
                await self._check_recursive(metric_sub_deps, visited, unpublished)

    # 复合公式允许的语法关键字（大小写不敏感，聚合/逻辑/空值等，非指标引用）
    _FORMULA_KEYWORDS: frozenset[str] = frozenset({
        "sum", "avg", "count", "count_distinct", "max", "min", "median", "percentile",
        "distinct", "if", "case", "when", "then", "else", "end", "null", "true", "false",
        "and", "or", "not", "in", "like", "between", "is", "as", "abs", "round", "floor",
        "ceil", "ceiling", "coalesce", "nullif",
    })

    async def validate_composite_formula(self, definition_json: dict[str, Any]) -> list[str]:
        """复合指标公式强校验（对齐界限文档 §1.2/§4.2）。

        复合公式只允许引用已存在的**派生/复合指标 code**（4 段式），
        禁止裸表字段（如 ``amount / head_amount`` 中的 ``amount``）与任意非指标标识符——
        OneData 复合层 = 跨指标聚合，公式里出现物理字段即口径污染。

        SQL 模式（``defn["sql"]``）豁免——完整查询语句不适用表达式 token 解析。

        Returns:
            错误信息列表（空列表 = 通过）。
        """
        if definition_json.get("sql"):
            return []
        expr = definition_json.get("expression")
        if not isinstance(expr, str) or not expr.strip():
            return ["复合指标缺少计算表达式（definition_json.expression）"]

        # 剥离字符串字面量（'x' / "x"），避免引号内文本干扰 token 提取
        text = re.sub(r"'[^']*'|\"[^\"]*\"", " ", expr)
        # 提取所有标识符（字母/下划线开头，可含数字）
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)

        errors: list[str] = []
        for tok in tokens:
            # 函数/语法关键字（含大小写变体）→ 跳过
            if tok.isupper() or tok.lower() in self._FORMULA_KEYWORDS:
                continue
            # 其余小写标识符必须是合法指标 code
            if not self._is_metric_code(tok):
                errors.append(
                    f"公式引用非指标标识符「{tok}」（复合公式仅允许派生/复合指标 code）"
                )
                continue
            metric = await self._get_metric_by_code(tok)
            if metric is None:
                errors.append(f"公式引用不存在的指标「{tok}」")
            elif metric.type not in ("derived", "composite"):
                errors.append(f"公式引用的「{tok}」不是派生/复合指标（当前为 {metric.type}）")
        return errors

    async def detect_cycle(
        self, metric_code: str, definition_json: dict[str, Any]
    ) -> list[str] | None:
        """DFS 三色标记法检测 DERIVED_FROM 有向图环。

        Args:
            metric_code: 待发布指标编码。
            definition_json: 指标口径定义。

        Returns:
            环路径列表（如 ["A", "B", "A"]）或 None（无环）。
        """
        dependencies: list[str] = definition_json.get("dependencies", [])
        metric_deps = [d for d in dependencies if self._is_metric_code(d)]
        if not metric_deps:
            return None

        # 三色标记: white=未访问, gray=正在访问(栈中), black=已完成
        color: dict[str, str] = {}
        path: list[str] = []

        # 从 metric_code 出发，检查其依赖子图中是否有环
        # 注意：环可能不经过 metric_code 本身，但我们需要检查整个可达子图
        cycle = await self._dfs(metric_code, metric_deps, color, path)
        return cycle

    async def _dfs(
        self,
        start_code: str,
        initial_deps: list[str],
        color: dict[str, str],
        path: list[str],
    ) -> list[str] | None:
        """DFS 遍历依赖图检测环。

        从 start_code 出发，沿着 initial_deps 向下遍历，
        若遇到 gray 节点则发现环。
        """
        # 标记 start_code 为 gray
        color[start_code] = "gray"
        path.append(start_code)

        for dep_code in initial_deps:
            if not self._is_metric_code(dep_code):
                continue

            dep_color = color.get(dep_code, "white")

            if dep_color == "gray":
                # 发现环：构造环路径
                cycle_start_idx = path.index(dep_code)
                cycle_path = path[cycle_start_idx:] + [dep_code]
                return cycle_path

            if dep_color == "white":
                # 加载依赖指标的子依赖
                metric = await self._get_metric_by_code(dep_code)
                if metric is None:
                    continue

                sub_deps = metric.definition_json.get("dependencies", [])
                metric_sub_deps = [d for d in sub_deps if self._is_metric_code(d)]

                if metric_sub_deps:
                    cycle = await self._dfs(dep_code, metric_sub_deps, color, path)
                    if cycle is not None:
                        return cycle
                else:
                    # 叶节点，直接标记为 black
                    color[dep_code] = "black"

        # start_code 的所有依赖都已访问完成
        color[start_code] = "black"
        path.pop()
        return None

    async def _get_metric_by_code(self, metric_code: str) -> Metric | None:
        """根据 metric_code 查询指标（缓存敏感，用于递归查询）。"""
        result = await self._db.execute(
            select(Metric).where(
                Metric.metric_code == metric_code,
                Metric.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _is_metric_code(dep: str) -> bool:
        """判断依赖是否是指标编码（4 段式下划线分隔）。

        表名/字段名等依赖不参与环检测。
        """
        parts = dep.split("_")
        return len(parts) == 4 and all(p.isalnum() and p[0].isalpha() for p in parts if p)
