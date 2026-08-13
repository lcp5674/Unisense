"""审计日志中文化单测（app/core/audit_i18n.py）。

覆盖：
- SCREAMING_SNAKE 通用动作 → 中文描述句
- 点号风格业务动作 → 中文描述句
- detail_json 摘要追加（版本=、决策= 等）
- 未知 action / 未知 entity_type 兜底（不抛异常）
- entity_label 中文映射与兜底
"""

from __future__ import annotations

from app.core.audit_i18n import describe_audit, entity_label


class TestEntityLabel:
    def test_known_entity(self) -> None:
        assert entity_label("metric_definition") == "指标定义"

    def test_unknown_entity_fallback(self) -> None:
        assert entity_label("unknown_entity_xyz") == "unknown_entity_xyz"

    def test_none_entity(self) -> None:
        assert entity_label(None) == "记录"


class TestDescribeAudit:
    def test_screaming_snake_generic(self) -> None:
        assert describe_audit("CREATE", "metric_definition") == "创建了指标定义"
        assert describe_audit("PUBLISH", "metric_definition") == "发布了指标定义"
        assert describe_audit("DEPRECATE", "metric_definition") == "废弃了指标定义"

    def test_screaming_snake_with_detail_summary(self) -> None:
        desc = describe_audit(
            "PUBLISH", "metric_definition", {"version": 2, "pii_flag": False}
        )
        # 已知摘要键（version）被提取；未知键（pii_flag）忽略
        assert desc == "发布了指标定义（版本=2）"

    def test_dot_style_business_action(self) -> None:
        assert describe_audit("term.create", "term") == "创建了业务术语"
        assert describe_audit("quality_event.detect", "quality_event") == "检测到质量事件异常"
        assert describe_audit("benchmark.import", "benchmark") == "导入了外部基准"

    def test_ai_nl2sql(self) -> None:
        desc = describe_audit("ai.nl2sql", "metric_definition")
        assert desc == "用 AI 将自然语言转为 SQL"

    def test_consume_query_with_detail(self) -> None:
        desc = describe_audit(
            "consume.query", "consumer_client", {"metric_code": "sales_gmv_amount_daily"}
        )
        assert "执行了消费接入方查询" in desc
        assert "指标=sales_gmv_amount_daily" in desc

    def test_unknown_action_fallback(self) -> None:
        desc = describe_audit("WEIRD_ACTION", "metric_definition")
        assert "指标定义" in desc
        assert "weird action" in desc

    def test_unknown_dot_action_fallback(self) -> None:
        desc = describe_audit("weird.module.op", "term")
        assert "对业务术语执行了「op」操作" in desc

    def test_empty_detail(self) -> None:
        assert describe_audit("UPDATE", "term", None) == "更新了业务术语"
        assert describe_audit("UPDATE", "term", {}) == "更新了业务术语"

    def test_detail_with_unrecognized_keys_only(self) -> None:
        desc = describe_audit("UPDATE", "term", {"foo": "bar"})
        assert desc == "更新了业务术语"

    def test_list_detail_summary(self) -> None:
        desc = describe_audit(
            "BULK_DEPRECATE", "metric_definition", {"metric_codes": ["a", "b"]}
        )
        assert "批量废弃了指标定义" in desc
