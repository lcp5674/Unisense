"""输入安全守卫单测（补齐覆盖率）。

针对 core/guard.py 的 35% 覆盖率补充：
- _is_suspicious 各类注入模式
- guard_against_injection 依赖：query 参数 / JSON body / 非写方法跳过
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import BusinessError
from app.core.guard import (
    _is_suspicious,
    guard_against_injection,
    guard_against_injection_exempt,
    guard_against_injection_exempt_paths,
)


class TestIsSuspicious:
    def test_plain_text_not_suspicious(self) -> None:
        assert _is_suspicious("查询上月销售额") is False
        assert _is_suspicious("sales_gmv_daily") is False
        assert _is_suspicious("domain=sales&page=1") is False

    def test_single_quote_or_detected(self) -> None:
        assert _is_suspicious("' or '1'='1") is True

    def test_or_equals_detected(self) -> None:
        assert _is_suspicious("1 or 1=1") is True

    def test_double_dash_comment_detected(self) -> None:
        assert _is_suspicious("-- comment") is True

    def test_semicolon_ddl_detected(self) -> None:
        assert _is_suspicious("; drop table users") is True

    def test_union_select_detected(self) -> None:
        assert _is_suspicious("union select password from users") is True

    def test_block_comment_detected(self) -> None:
        assert _is_suspicious("/* x */") is True

    def test_cron_slash_star_not_injection(self) -> None:
        """*/ 单独出现（cron 表达式，如 */5 * * * *）不应被误判为注入。"""
        assert _is_suspicious("*/5 * * * *") is False

    def test_xp_cmdshell_detected(self) -> None:
        assert _is_suspicious("xp_cmdshell") is True

    def test_sleep_detected(self) -> None:
        assert _is_suspicious("sleep(5)") is True

    def test_waitfor_detected(self) -> None:
        assert _is_suspicious("waitfor delay '0:0:5'") is True


class TestGuardAgainstInjection:
    async def test_clean_query_passes(self) -> None:
        request = MagicMock()
        request.query_params = {"domain": "sales", "page": "1"}
        request.method = "GET"
        await guard_against_injection(request)  # 不应抛异常

    async def test_suspicious_query_raises(self) -> None:
        request = MagicMock()
        request.query_params = {"keyword": "' or '1'='1"}
        request.method = "GET"
        with pytest.raises(BusinessError):
            await guard_against_injection(request)

    async def test_suspicious_json_body_raises(self) -> None:
        request = MagicMock()
        request.query_params = {}
        request.method = "POST"
        request.json = AsyncMock(return_value={"name": "x'; DROP TABLE users--"})
        with pytest.raises(BusinessError):
            await guard_against_injection(request)

    async def test_clean_json_body_passes(self) -> None:
        request = MagicMock()
        request.query_params = {}
        request.method = "POST"
        request.json = AsyncMock(return_value={"name": "销售指标", "domain": "sales"})
        await guard_against_injection(request)  # 不应抛异常

    async def test_body_json_error_ignored(self) -> None:
        request = MagicMock()
        request.query_params = {}
        request.method = "POST"
        request.json = AsyncMock(side_effect=ValueError("bad json"))
        await guard_against_injection(request)  # 不应抛异常

    async def test_non_body_method_skips_json(self) -> None:
        request = MagicMock()
        request.query_params = {}
        request.method = "GET"
        request.json = AsyncMock()
        await guard_against_injection(request)
        request.json.assert_not_called()


class TestScanDeep:
    """_scan_deep 递归扫描分支（深度截断 / list 嵌套 / dict 嵌套）。"""

    def test_deep_nested_beyond_max_depth_returns_false(self) -> None:
        from app.core.guard import _scan_deep

        # 深度超限：实现升级为显式拒绝（抛 BusinessError），而非静默截断返回 False
        with pytest.raises(BusinessError):
            _scan_deep({"a": {"b": {"c": "value"}}}, depth=1, max_depth=0)

    def test_list_nested_injection_detected(self) -> None:
        from app.core.guard import _scan_deep

        assert _scan_deep(["normal", "a' or '1'='1"]) is True

    def test_list_plain_values_not_suspicious(self) -> None:
        from app.core.guard import _scan_deep

        assert _scan_deep(["sales", "gmv", "amount"]) is False

    def test_dict_nested_injection_detected(self) -> None:
        from app.core.guard import _scan_deep

        assert _scan_deep({"filters": [{"expr": "x = 1; drop table t"}]}) is True

    def test_scalar_types_not_suspicious(self) -> None:
        from app.core.guard import _scan_deep

        assert _scan_deep(42) is False
        assert _scan_deep(3.14) is False
        assert _scan_deep(None) is False
        assert _scan_deep(True) is False


class TestGuardAgainstInjectionExempt:
    """guard_against_injection_exempt 字段级豁免（SQL 血缘解析端点场景）。"""

    @staticmethod
    def _request(body: dict | None = None, query: dict | None = None) -> MagicMock:
        request = MagicMock()
        request.query_params = query or {}
        request.method = "POST"
        request.json = AsyncMock(return_value=body)
        return request

    async def test_exempt_field_skips_body_scan(self) -> None:
        """豁免的 sql 字段含注释/UNION/块注释等合法 SQL，不应被拦截。"""
        guard = guard_against_injection_exempt("sql")
        await guard(
            self._request(
                {
                    "sql": (
                        "SELECT u.id, o.amount FROM db1.users u -- 取用户与订单\n"
                        "LEFT JOIN db2.orders o /* +SET_VAR(enable_vectorized_engine=false) */\n"
                        "ON u.id = o.uid\n"
                        "UNION ALL SELECT id, amount FROM db3.archive"
                    ),
                    "dialect": "doris",
                }
            )
        )  # 不应抛异常

    async def test_multistatement_etl_passes(self) -> None:
        """多语句 ETL SQL（; INSERT INTO ... SELECT ...）也应放行。"""
        guard = guard_against_injection_exempt("sql")
        await guard(
            self._request(
                {
                    "sql": (
                        "DROP TABLE IF EXISTS tmp_stage;\n"
                        "INSERT INTO dwd.user_daily SELECT id, name FROM ods.users;"
                    ),
                    "dialect": "hive",
                }
            )
        )  # 不应抛异常

    async def test_other_fields_still_scanned(self) -> None:
        """豁免 sql 后，其余字段仍应被注入扫描拦截。"""
        guard = guard_against_injection_exempt("sql")
        with pytest.raises(BusinessError):
            await guard(self._request({"sql": "SELECT 1", "provenance": "x'; drop table users--"}))

    async def test_exempt_only_applies_to_top_level(self) -> None:
        """豁免仅作用于顶层键，嵌套同名键不豁免（防深层绕过）。"""
        guard = guard_against_injection_exempt("sql")
        with pytest.raises(BusinessError):
            await guard(self._request({"data": {"sql": "-- hidden"}}))

    async def test_query_params_still_blocked(self) -> None:
        """query 参数不参与豁免，命中仍拦截。"""
        guard = guard_against_injection_exempt("sql")
        with pytest.raises(BusinessError):
            await guard(
                self._request(
                    body={"sql": "SELECT 1"},
                    query={"node": "' OR 1=1 -- "},
                )
            )

    async def test_multiple_exempt_fields(self) -> None:
        """多个豁免字段同时生效。"""
        guard = guard_against_injection_exempt("sql", "dialect")
        await guard(self._request({"sql": "-- comment", "dialect": "doris; drop"}))  # 不应抛异常

    async def test_plain_injection_still_blocked_without_exempt(self) -> None:
        """未豁免 sql 时，含注释的 SQL 仍按注入拦截（守卫默认行为不变）。"""
        guard = guard_against_injection_exempt("dialect")
        with pytest.raises(BusinessError):
            await guard(self._request({"sql": "-- comment"}))


class TestGuardAgainstInjectionExemptPaths:
    """guard_against_injection_exempt_paths 嵌套路径豁免（SQL 批量注册候选口径场景）。"""

    @staticmethod
    def _request(body: dict | None = None, query: dict | None = None) -> MagicMock:
        request = MagicMock()
        request.query_params = query or {}
        request.method = "POST"
        request.json = AsyncMock(return_value=body)
        return request

    def test_parse_path_syntax(self) -> None:
        from app.core.guard import _parse_exempt_path

        assert _parse_exempt_path("candidates[].definition_json") == (
            "candidates",
            "[*]",
            "definition_json",
        )
        assert _parse_exempt_path("sql") == ("sql",)
        assert _parse_exempt_path("a[].b[].c") == ("a", "[*]", "b", "[*]", "c")
        with pytest.raises(ValueError):
            _parse_exempt_path("a..b")

    async def test_nested_sql_subtree_exempted(self) -> None:
        """candidates[].definition_json 子树含合法 ETL SQL（-- 注释/UNION/多语句）应放行。"""
        guard = guard_against_injection_exempt_paths("candidates[].definition_json")
        await guard(
            self._request(
                {
                    "domain": "sales",
                    "candidates": [
                        {
                            "key": "0:composite",
                            "metric_code": "s_gmv_day",
                            "name": "GMV",
                            "definition_json": {
                                "sql": (
                                    "SELECT dt, SUM(amount) FROM dwd_order_di -- 取当日\n"
                                    "UNION ALL SELECT dt, amount FROM ods.archive;\n"
                                    "/* 上日全量 */ SELECT dt, SUM(amount) FROM dwd_order_di"
                                )
                            },
                        },
                        {
                            "key": "1:amount",
                            "metric_code": "s_amount_day",
                            "name": "金额",
                            "definition_json": {"expression": "sum(amount)"},
                        },
                    ],
                }
            )
        )  # 不应抛异常

    async def test_sibling_fields_still_scanned(self) -> None:
        """豁免 definition_json 后，候选同层字段（name）注入仍应拦截。"""
        guard = guard_against_injection_exempt_paths("candidates[].definition_json")
        with pytest.raises(BusinessError):
            await guard(
                self._request(
                    {
                        "domain": "sales",
                        "candidates": [
                            {
                                "key": "x",
                                "metric_code": "s_a_day",
                                "name": "x'; DROP TABLE users--",
                                "definition_json": {"sql": "SELECT 1"},
                            }
                        ],
                    }
                )
            )

    async def test_unrelated_nested_key_not_auto_exempted(self) -> None:
        """非豁免路径的嵌套同名键不自动豁免（保守：data.sql 仍拦截）。"""
        guard = guard_against_injection_exempt_paths("candidates[].definition_json")
        with pytest.raises(BusinessError):
            await guard(self._request({"data": {"definition_json": {"sql": "-- hidden"}}}))

    async def test_other_fields_still_scanned(self) -> None:
        """顶层非豁免字段（dialect 含注入）仍拦截。"""
        guard = guard_against_injection_exempt_paths("candidates[].definition_json")
        with pytest.raises(BusinessError):
            await guard(
                self._request(
                    {
                        "candidates": [{"definition_json": {"sql": "SELECT 1"}}],
                        "dialect": "hive'; drop table users--",
                    }
                )
            )

    async def test_conflict_check_definition_paths_exempt(self) -> None:
        """conflicts/check 的 candidate/existing 口径字段（含 -- 注释 SQL）应放行。

        注册向导"冲突预检"把 sqlText 放进 candidate.definition、完整口径放进
        definition_json——合法 ETL 的 --/UNION/块注释不得被注入正则误伤。
        """
        guard = guard_against_injection_exempt_paths(
            "candidate.definition",
            "candidate.definition_json",
            "existing[].definition",
            "existing[].definition_json",
        )
        await guard(
            self._request(
                {
                    "candidate": {
                        "metric_code": "doc_active_cnt_month",
                        "definition": (
                            "SELECT month_id, COUNT(DISTINCT doctor_code) -- 月活医生\n"
                            "FROM wedw_dw.doctor_visit_agent_info_da"
                        ),
                        "definition_json": {
                            "sql": (
                                "SELECT a.month_id, a.hosp_code -- 医院维度\n"
                                "UNION ALL SELECT id, amount FROM ods.archive"
                            )
                        },
                    },
                    "existing": [
                        {
                            "metric_code": "doc_active_cnt_month",
                            "definition": "/* 块注释 */ SELECT 1",
                            "definition_json": {"expression": "sum(amount) -- 金额"},
                        }
                    ],
                }
            )
        )  # 不应抛异常

    async def test_conflict_check_other_fields_still_scanned(self) -> None:
        """conflicts/check 豁免口径后，metric_code 等其余字段仍拦截。"""
        guard = guard_against_injection_exempt_paths(
            "candidate.definition",
            "candidate.definition_json",
        )
        with pytest.raises(BusinessError):
            await guard(
                self._request(
                    {
                        "candidate": {
                            "metric_code": "x'; DROP TABLE users--",
                            "definition": "SELECT 1",
                        }
                    }
                )
            )

    async def test_regex_fields_exempt(self) -> None:
        """sensitive_rules 的 name_re/sample_re/pattern 正则文本（含 --.* / /* 等）应放行。"""
        guard = guard_against_injection_exempt("name_re", "sample_re")
        await guard(
            self._request(
                {
                    "label": "匹配 SQL 注释",
                    "name_re": "--.*",
                    "sample_re": r"/\*.*\*/",
                }
            )
        )  # 不应抛异常
        guard_pattern = guard_against_injection_exempt("pattern")
        await guard_pattern(self._request({"pattern": r"--.*|\/\*.*\*\/"}))  # 不应抛异常

    async def test_regex_other_fields_still_scanned(self) -> None:
        """sensitive_rules 豁免正则后，label 等其余字段仍拦截。"""
        guard = guard_against_injection_exempt("name_re")
        with pytest.raises(BusinessError):
            await guard(
                self._request(
                    {"label": "x'; DROP TABLE users--", "name_re": "[0-9]+"}
                )
            )

    async def test_mapping_expression_exempt(self) -> None:
        """dimension 映射 expression（含 SQL 注释/函数）应放行，其余字段仍拦截。"""
        guard = guard_against_injection_exempt("expression")
        await guard(
            self._request(
                {
                    "source_dim_code": "dept",
                    "target_dim_code": "org",
                    "mapping_type": "sql",
                    "expression": "CASE WHEN code LIKE 'A%' THEN '甲' END -- 映射规则",
                }
            )
        )  # 不应抛异常
        with pytest.raises(BusinessError):
            await guard(
                self._request(
                    {
                        "source_dim_code": "x'; DROP TABLE users--",
                        "mapping_type": "sql",
                        "expression": "SELECT 1",
                    }
                )
            )

    async def test_query_params_still_blocked(self) -> None:
        """query 参数不参与路径豁免，命中仍拦截。"""
        guard = guard_against_injection_exempt_paths("candidates[].definition_json")
        with pytest.raises(BusinessError):
            await guard(
                self._request(
                    body={"candidates": [{"definition_json": {"sql": "SELECT 1"}}]},
                    query={"node": "' OR 1=1 -- "},
                )
            )

    async def test_multiple_paths(self) -> None:
        """多个豁免路径同时生效。"""
        guard = guard_against_injection_exempt_paths(
            "candidates[].definition_json",
            "statements[].sql",
        )
        await guard(
            self._request(
                {
                    "statements": [{"sql": "SELECT 1 -- c", "measure_count": 1}],
                    "candidates": [{"definition_json": {"expression": "sum(1)"}}],
                }
            )
        )  # 不应抛异常
