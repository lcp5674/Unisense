"""Schema Drift 检测器单元测试（对齐 US2 / FR-010/FR-011）。

覆盖：
1. 新增列检测 → change_type=ADD_COLUMN
2. 删除列检测 → change_type=DROP_COLUMN
3. 类型变更检测 → change_type=TYPE_CHANGE
4. 无变更 → 返回 None
5. 首次采集（无旧签名）→ 返回 None
6. diff_json 计算正确性
7. 内容指纹确定性（相同 schema 产生相同指纹）
8. 内容指纹敏感性（不同 schema 产生不同指纹）
"""

from __future__ import annotations

from app.services.collector.drift_detector import (
    DriftDetector,
    DriftResult,
    compute_content_signature,
)


class TestContentSignature:
    """内容指纹计算测试。"""

    def test_same_schema_same_signature(self):
        """相同 schema 产生相同指纹。"""
        schema = {"columns": ["id", "name", "email"]}
        sig1 = compute_content_signature(schema)
        sig2 = compute_content_signature(schema)
        assert sig1 == sig2
        assert len(sig1) == 64  # SHA-256 hex digest

    def test_different_schema_different_signature(self):
        """不同 schema 产生不同指纹。"""
        schema1 = {"columns": ["id", "name"]}
        schema2 = {"columns": ["id", "name", "email"]}
        sig1 = compute_content_signature(schema1)
        sig2 = compute_content_signature(schema2)
        assert sig1 != sig2

    def test_signature_deterministic_with_different_key_order(self):
        """排序 key 保证相同内容不同序列化顺序产生相同指纹。"""
        # JSON 序列化顺序不同但内容相同
        schema1 = {"columns": ["id", "name"], "table": "users"}
        schema2 = {"table": "users", "columns": ["id", "name"]}
        sig1 = compute_content_signature(schema1)
        sig2 = compute_content_signature(schema2)
        assert sig1 == sig2

    def test_signature_length(self):
        """SHA-256 指纹长度为 64 字符。"""
        sig = compute_content_signature({"columns": ["id"]})
        assert len(sig) == 64

    def test_signature_empty_schema(self):
        """空 schema 也产生指纹。"""
        sig = compute_content_signature({})
        assert isinstance(sig, str)
        assert len(sig) == 64


class TestDriftDetector:
    """Drift 检测器测试。"""

    def test_no_drift_same_signature(self):
        """指纹相同 = 无变更，返回 None。"""
        schema = {"columns": ["id", "name"]}
        sig = compute_content_signature(schema)
        result = DriftDetector.detect(
            source_id="s1",
            entity_name="users",
            old_signature=sig,
            new_signature=sig,
            old_schema=schema,
            new_schema=schema,
        )
        assert result is None

    def test_no_drift_first_collection(self):
        """首次采集（无旧签名）不视为 Drift。"""
        schema = {"columns": ["id", "name"]}
        sig = compute_content_signature(schema)
        result = DriftDetector.detect(
            source_id="s1",
            entity_name="users",
            old_signature=None,
            new_signature=sig,
            old_schema=None,
            new_schema=schema,
        )
        assert result is None

    def test_add_column_detected(self):
        """新增列检测 → change_type=ADD_COLUMN。"""
        old_schema = {"columns": ["id", "name"]}
        new_schema = {"columns": ["id", "name", "email"]}
        old_sig = compute_content_signature(old_schema)
        new_sig = compute_content_signature(new_schema)

        result = DriftDetector.detect(
            source_id="s1",
            entity_name="users",
            old_signature=old_sig,
            new_signature=new_sig,
            old_schema=old_schema,
            new_schema=new_schema,
        )

        assert result is not None
        assert isinstance(result, DriftResult)
        assert result.change_type == "ADD_COLUMN"
        assert "email" in result.diff_json["added"]
        assert len(result.diff_json["removed"]) == 0
        assert len(result.diff_json["changed"]) == 0

    def test_drop_column_detected(self):
        """删除列检测 → change_type=DROP_COLUMN。"""
        old_schema = {"columns": ["id", "name", "email"]}
        new_schema = {"columns": ["id", "name"]}
        old_sig = compute_content_signature(old_schema)
        new_sig = compute_content_signature(new_schema)

        result = DriftDetector.detect(
            source_id="s1",
            entity_name="users",
            old_signature=old_sig,
            new_signature=new_sig,
            old_schema=old_schema,
            new_schema=new_schema,
        )

        assert result is not None
        assert result.change_type == "DROP_COLUMN"
        assert "email" in result.diff_json["removed"]
        assert len(result.diff_json["added"]) == 0
        assert len(result.diff_json["changed"]) == 0

    def test_type_change_detected(self):
        """类型变更检测 → change_type=TYPE_CHANGE。"""
        old_schema = {
            "columns": [
                {"name": "id", "type": "int"},
                {"name": "name", "type": "varchar(50)"},
            ]
        }
        new_schema = {
            "columns": [
                {"name": "id", "type": "bigint"},
                {"name": "name", "type": "varchar(50)"},
            ]
        }
        old_sig = compute_content_signature(old_schema)
        new_sig = compute_content_signature(new_schema)

        result = DriftDetector.detect(
            source_id="s1",
            entity_name="users",
            old_signature=old_sig,
            new_signature=new_sig,
            old_schema=old_schema,
            new_schema=new_schema,
        )

        assert result is not None
        assert result.change_type == "TYPE_CHANGE"
        assert len(result.diff_json["changed"]) == 1
        assert result.diff_json["changed"][0]["name"] == "id"
        assert result.diff_json["changed"][0]["before"]["type"] == "int"
        assert result.diff_json["changed"][0]["after"]["type"] == "bigint"
        assert len(result.diff_json["added"]) == 0
        assert len(result.diff_json["removed"]) == 0

    def test_mixed_changes_detected(self):
        """混合变更（新增+删除）→ change_type=SCHEMA_CHANGED。"""
        old_schema = {"columns": ["id", "name", "old_col"]}
        new_schema = {"columns": ["id", "name", "new_col"]}
        old_sig = compute_content_signature(old_schema)
        new_sig = compute_content_signature(new_schema)

        result = DriftDetector.detect(
            source_id="s1",
            entity_name="users",
            old_signature=old_sig,
            new_signature=new_sig,
            old_schema=old_schema,
            new_schema=new_schema,
        )

        assert result is not None
        assert result.change_type == "SCHEMA_CHANGED"
        assert "new_col" in result.diff_json["added"]
        assert "old_col" in result.diff_json["removed"]

    def test_drift_result_contains_before_after_schemas(self):
        """DriftResult 包含变更前后 schema。"""
        old_schema = {"columns": ["id"]}
        new_schema = {"columns": ["id", "name"]}
        old_sig = compute_content_signature(old_schema)
        new_sig = compute_content_signature(new_schema)

        result = DriftDetector.detect(
            source_id="s1",
            entity_name="users",
            old_signature=old_sig,
            new_signature=new_sig,
            old_schema=old_schema,
            new_schema=new_schema,
        )

        assert result is not None
        assert result.before_schema == old_schema
        assert result.after_schema == new_schema

    def test_diff_json_sorted(self):
        """diff_json 中的 added/removed 列表已排序。"""
        old_schema = {"columns": ["z_col", "a_col"]}
        new_schema = {"columns": ["z_col", "b_col", "c_col"]}
        old_sig = compute_content_signature(old_schema)
        new_sig = compute_content_signature(new_schema)

        result = DriftDetector.detect(
            source_id="s1",
            entity_name="test",
            old_signature=old_sig,
            new_signature=new_sig,
            old_schema=old_schema,
            new_schema=new_schema,
        )

        assert result is not None
        assert result.diff_json["added"] == ["b_col", "c_col"]  # sorted
        assert result.diff_json["removed"] == ["a_col"]  # sorted

    def test_empty_old_schema(self):
        """旧 schema 为空时，所有列视为新增。"""
        old_schema = {}
        new_schema = {"columns": ["id", "name"]}
        old_sig = compute_content_signature(old_schema)
        new_sig = compute_content_signature(new_schema)

        result = DriftDetector.detect(
            source_id="s1",
            entity_name="users",
            old_signature=old_sig,
            new_signature=new_sig,
            old_schema=old_schema,
            new_schema=new_schema,
        )

        assert result is not None
        assert result.change_type == "ADD_COLUMN"

    def test_simple_and_detailed_column_formats(self):
        """简单格式和详细格式的列混合场景。"""
        # 旧 schema 用简单格式，新 schema 用详细格式
        old_schema = {"columns": ["id", "name"]}
        new_schema = {
            "columns": [
                {"name": "id", "type": "int"},
                {"name": "name", "type": "varchar(50)"},
            ]
        }
        old_sig = compute_content_signature(old_schema)
        new_sig = compute_content_signature(new_schema)

        # 由于格式不同，指纹一定不同
        result = DriftDetector.detect(
            source_id="s1",
            entity_name="users",
            old_signature=old_sig,
            new_signature=new_sig,
            old_schema=old_schema,
            new_schema=new_schema,
        )

        assert result is not None
