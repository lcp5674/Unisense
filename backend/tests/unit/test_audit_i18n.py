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

    def test_column_description_entity(self) -> None:
        assert entity_label("column_description") == "字段"

    def test_none_entity(self) -> None:
        assert entity_label(None) == "记录"


class TestDescribeAudit:
    def test_screaming_snake_generic(self) -> None:
        assert describe_audit("CREATE", "metric_definition") == "创建了指标定义"
        assert describe_audit("PUBLISH", "metric_definition") == "发布了指标定义"
        assert describe_audit("DEPRECATE", "metric_definition") == "废弃了指标定义"

    def test_screaming_snake_with_detail_summary(self) -> None:
        desc = describe_audit("PUBLISH", "metric_definition", {"version": 2, "pii_flag": False})
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
        desc = describe_audit("BULK_DEPRECATE", "metric_definition", {"metric_codes": ["a", "b"]})
        assert "批量废弃了指标定义" in desc

    def test_data_source_actions(self) -> None:
        """数据源生命周期/采集/批量/描述动作应产出业务中文描述句。"""
        assert describe_audit("TEST_CONNECTION", "data_source") == "测试了数据源连接"
        assert describe_audit("BATCH_ENABLE", "data_source") == "批量启用了数据源"
        assert describe_audit("BATCH_DISABLE", "data_source") == "批量停用了数据源"
        assert describe_audit("BATCH_DELETE", "data_source") == "批量删除了数据源"
        assert describe_audit("BATCH_PROBE", "data_source") == "批量探活了数据源连接"
        assert describe_audit("BATCH_SCHEDULE", "data_source") == "批量配置了数据源调度"
        assert describe_audit("REFRESH", "db_catalog") == "刷新了数据表目录元数据"
        assert describe_audit("COLLECT_ASYNC", "data_source") == "异步采集了数据源元数据"
        assert describe_audit("COLLECT_NOW", "data_source") == "立即采集了数据源元数据"

    def test_description_actions(self) -> None:
        """字段/表级描述推断与编辑动作应以业务术语呈现。"""
        assert describe_audit("INFER_DESCRIPTION", "column_description") == "推断字段描述"
        assert (
            describe_audit("INFER_DESCRIPTIONS_BATCH", "column_description") == "批量推断字段描述"
        )
        assert describe_audit("UPDATE_DESCRIPTION", "column_description") == "更新了字段描述"
        assert describe_audit("UPDATE_TABLE_DESCRIPTION", "catalog") == "更新了资产目录表级描述"
        assert describe_audit("INFER_TABLE_DESCRIPTION", "catalog") == "推断资产目录表级描述"

    def test_data_source_collect_detail_summary(self) -> None:
        desc = describe_audit(
            "COLLECT", "data_source", {"scanned": 270, "registered": 260, "failed_count": 0}
        )
        assert "采集了数据源元数据" in desc
        assert "扫描数=270" in desc
        assert "失败数=0" in desc

    def test_batch_detail_summary(self) -> None:
        desc = describe_audit("BATCH_ENABLE", "data_source", {"succeeded": 3, "failed": 1})
        assert "批量启用了数据源" in desc
        assert "成功=3" in desc
        assert "失败=1" in desc

    def test_summarize_detail_list_value(self) -> None:
        """detail 中命中摘要键的 list/tuple 值应转为逗号串（对齐 210 行转换分支）。"""
        desc = describe_audit("LINEAGE_PARSE", "lineage_edge", {"table_edges": ["a", "b"]})
        assert "表级血缘边=a,b" in desc


class TestNewDotNaming:
    """统一点号命名（2026-08 根治）：{entity}.{verb} 经动词模板翻译，历史旧名兼容。"""

    def test_basic_crud_new_naming(self) -> None:
        assert describe_audit("metric_definition.create", "metric_definition") == "创建了指标定义"
        assert describe_audit("data_source.update", "data_source") == "更新了数据源"
        assert describe_audit("user.delete", "user") == "删除了用户"

    def test_batch_new_naming(self) -> None:
        assert describe_audit("data_source.batch_enable", "data_source") == "批量启用了数据源"
        assert describe_audit("data_source.batch_probe", "data_source") == "批量探活了数据源连接"
        assert describe_audit("db_catalog.bulk_deprecate", "db_catalog") == "批量废弃了数据表目录"

    def test_special_metric_actions(self) -> None:
        assert (
            describe_audit("metric_definition.mark_source_dropped", "metric_definition")
            == "标记指标定义数据源下线"
        )
        assert (
            describe_audit("metric_definition.recover_source_dropped", "metric_definition")
            == "恢复了指标定义（数据源已恢复）"
        )
        assert (
            describe_audit("metric_definition.confirm_deprecate_dropped", "metric_definition")
            == "确认退役了指标定义"
        )
        assert (
            describe_audit("metric_definition.batch_submit_partial", "metric_definition")
            == "部分提交指标定义评审成功（存在失败项）"
        )

    def test_auth_actions(self) -> None:
        assert describe_audit("auth.login", "user") == "登录了系统"
        assert describe_audit("auth.logout", "user") == "退出了系统"
        assert describe_audit("auth.login_failed", "user") == "登录失败"

    def test_audit_export(self) -> None:
        assert describe_audit("audit.export", "audit_log") == "导出了审计日志"

    def test_legacy_screaming_still_works(self) -> None:
        """历史 WORM 记录（旧 SCREAMING 命名）仍可翻译，不受命名统一影响。"""
        assert describe_audit("CREATE", "metric_definition") == "创建了指标定义"
        assert describe_audit("COLLECT", "data_source") == "采集了数据源元数据"
        assert describe_audit("LINEAGE_PARSE", "lineage") == "解析并写入数据血缘"

    def test_legacy_dot_still_works(self) -> None:
        """历史点号命名（term.create 等）与新旧名同名，仍可翻译。"""
        assert describe_audit("term.create", "term") == "创建了业务术语"
        assert describe_audit("quality_event.detect", "quality_event") == "检测到质量事件异常"

    def test_verb_coverage_for_all_actions(self) -> None:
        """全仓所有静态审计 action 均能经动词模板/旧表翻译（不落兜底英文）。"""
        import pathlib
        import re

        from app.core.audit_i18n import _DOT_ACTION_TEMPLATES, _VERB_TEMPLATES

        pat = re.compile(r"await\s+(?:\w+\.)?_?write_audit\s*\(")
        actions: set[str] = set()
        for p in pathlib.Path("app").rglob("*.py"):
            src = p.read_text()
            for m in pat.finditer(src):
                seg = src[m.end() : m.end() + 600]
                a = re.search(r'action\s*=\s*"([A-Za-z_.]+)"', seg)
                if a:
                    actions.add(a.group(1))

        uncovered: list[str] = []
        for a in actions:
            if a in _DOT_ACTION_TEMPLATES:
                continue
            verb = a.rsplit(".", 1)[-1].lower()
            if verb not in _VERB_TEMPLATES:
                uncovered.append(a)
        assert not uncovered, f"以下 action 无中文模板: {uncovered}"

    def test_verbose_wording_no_redundancy(self) -> None:
        """翻译不应出现连续重复词（如「指标冲突冲突」「用户反馈反馈」的措辞冗余）。"""
        import re

        for action, entity in (
            ("conflict.check", "conflict"),
            ("feedback.submit", "feedback"),
            ("glossary.resolve_conflict", "glossary_conflict"),
        ):
            assert not re.search(r"([\u4e00-\u9fa5]{2,4})\1", describe_audit(action, entity))
