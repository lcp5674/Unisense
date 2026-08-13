"""Schema Drift 检测器（对齐 TD §12.1 / spec FR-010/FR-011）。

检测两次采集间的 Schema 变更（新增列/删除列/类型变更），
计算差异详情 diff_json ({added:[], removed:[], changed:[]})，
返回 DriftResult 或 None（无变更时）。

内容指纹算法（Decision 6）：SHA-256(canonical_json(schema_json))，
canonical_json 为排序 key 后的 JSON 序列化。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DriftResult:
    """Schema Drift 检测结果。"""

    change_type: str
    diff_json: dict[str, Any] = field(default_factory=dict)
    before_schema: dict[str, Any] | None = None
    after_schema: dict[str, Any] | None = None


def compute_content_signature(schema_json: dict[str, Any]) -> str:
    """计算内容指纹 SHA-256(canonical_json(schema_json))。

    canonical_json: 排序 key 后的 JSON 序列化（确保相同 schema 不同序列化顺序产生相同指纹）。

    Args:
        schema_json: Schema JSON 字典。

    Returns:
        SHA-256 十六进制字符串（64 字符）。
    """
    import hashlib

    canonical = json.dumps(schema_json, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DriftDetector:
    """Schema Drift 检测器。"""

    @staticmethod
    def detect(
        source_id: str,
        entity_name: str,
        old_signature: str | None,
        new_signature: str,
        old_schema: dict[str, Any] | None,
        new_schema: dict[str, Any],
    ) -> DriftResult | None:
        """检测 Schema 变更。

        Args:
            source_id: 数据源标识。
            entity_name: 实体名。
            old_signature: 旧内容指纹（首次采集时为 None）。
            new_signature: 新内容指纹。
            old_schema: 旧 schema（首次采集时为 None）。
            new_schema: 新 schema。

        Returns:
            DriftResult 如果检测到变更；None 如果无变更或首次采集（无旧签名）。
        """
        # 首次采集（无旧签名）不视为 Drift
        if old_signature is None:
            return None

        # 指纹相同 = 无变更
        if old_signature == new_signature:
            return None

        # 指纹不同 = 检测到变更，计算 diff
        diff = _compute_column_diff(old_schema, new_schema)

        # 确定变更类型
        change_type = _determine_change_type(diff)

        return DriftResult(
            change_type=change_type,
            diff_json=diff,
            before_schema=old_schema,
            after_schema=new_schema,
        )


def _compute_column_diff(
    old_schema: dict[str, Any] | None,
    new_schema: dict[str, Any],
) -> dict[str, Any]:
    """计算新旧 Schema 的列级差异。

    Args:
        old_schema: 旧 Schema。
        new_schema: 新 Schema。

    Returns:
        差异字典 {added: [...], removed: [...], changed: [...]}。
    """
    old_columns = _extract_columns(old_schema)
    new_columns = _extract_columns(new_schema)

    old_names = set(old_columns.keys())
    new_names = set(new_columns.keys())

    added = sorted(new_names - old_names)
    removed = sorted(old_names - new_names)

    # 检测类型变更（同名列但类型不同）
    changed: list[dict[str, Any]] = []
    for name in sorted(old_names & new_names):
        old_col = old_columns[name]
        new_col = new_columns[name]
        if old_col != new_col:
            changed.append(
                {
                    "name": name,
                    "before": old_col,
                    "after": new_col,
                }
            )

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def _extract_columns(schema: dict[str, Any] | None) -> dict[str, Any]:
    """从 schema_json 中提取列名→列信息的映射。

    支持两种格式：
    - 简单格式：{"columns": ["col1", "col2"]}
    - 详细格式：{"columns": [{"name": "col1", "type": "int"}, ...]}
    """
    if schema is None:
        return {}

    columns = schema.get("columns", [])
    result: dict[str, Any] = {}

    for col in columns:
        if isinstance(col, str):
            result[col] = col
        elif isinstance(col, dict):
            name = col.get("name", "")
            if name:
                result[name] = col

    return result


def _determine_change_type(diff: dict[str, Any]) -> str:
    """根据 diff 确定变更类型。"""
    added = diff.get("added", [])
    removed = diff.get("removed", [])
    changed = diff.get("changed", [])

    if removed and not added and not changed:
        return "DROP_COLUMN"
    if added and not removed and not changed:
        return "ADD_COLUMN"
    if changed and not added and not removed:
        return "TYPE_CHANGE"
    return "SCHEMA_CHANGED"
