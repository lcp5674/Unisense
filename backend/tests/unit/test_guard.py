"""输入安全守卫单测（补齐覆盖率）。

针对 core/guard.py 的 35% 覆盖率补充：
- _is_suspicious 各类注入模式
- guard_against_injection 依赖：query 参数 / JSON body / 非写方法跳过
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import BusinessError
from app.core.guard import _is_suspicious, guard_against_injection


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
