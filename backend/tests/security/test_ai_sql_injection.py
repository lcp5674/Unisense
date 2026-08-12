"""AI 服务 SQL 注入安全测试（对齐 US1 / FR-001, FR-002, FR-003, FR-004）。

测试场景：
1. 嵌套 JSON 注入守卫拦截（guard.py 递归扫描）
2. AI 关键词分支 SQL 参数化（ai/service.py 不再 f-string 拼接）
3. 弱密钥启动拒绝（jwt_secret < 32 字符）
4. Fernet 降级拒绝（生产环境未配置 FERNET_KEY）
"""

from __future__ import annotations

import os

import pytest

from app.core.guard import _scan_deep

# ============================================================
# T018-1: 嵌套 JSON 注入守卫拦截
# ============================================================


class TestNestedJsonInjectionGuard:
    """测试 guard.py 递归扫描嵌套 JSON 中的注入向量。"""

    def test_deeply_nested_dict_injection_detected(self) -> None:
        """嵌套 dict 中含注入向量应被检测。"""
        body = {
            "data": {
                "inner": {
                    "name": "'; DROP TABLE metrics; --"
                }
            }
        }
        assert _scan_deep(body) is True

    def test_nested_list_injection_detected(self) -> None:
        """嵌套 list 中含注入向量应被检测。"""
        body = {
            "items": [
                "normal_value",
                ["nested_list", "UNION SELECT * FROM users"]
            ]
        }
        assert _scan_deep(body) is True

    def test_dict_in_list_injection_detected(self) -> None:
        """list 中 dict 的注入向量应被检测。"""
        body = [
            {"name": "normal"},
            {"payload": "1 OR 1=1"},
        ]
        assert _scan_deep(body) is True

    def test_deeply_nested_list_injection(self) -> None:
        """多层嵌套 list 中的注入向量应被检测。"""
        body = [[[["'; DELETE FROM metrics; --"]]]]
        assert _scan_deep(body) is True

    def test_max_depth_limit_not_exceeded(self) -> None:
        """超过最大递归深度时应截断（不报错）。"""
        # 15 层嵌套，超过 max_depth=10
        body: object = "safe_value"
        for _ in range(15):
            body = {"nested": body}
        # 超过最大深度后不会检测到，但也不会报错
        assert _scan_deep(body) is False

    def test_safe_nested_structure_passes(self) -> None:
        """安全的嵌套结构应通过。"""
        body = {
            "data": {
                "name": "sales_gmv_daily",
                "dimensions": ["city", "category"],
                "filters": [{"field": "date", "value": "2024-01-01"}]
            }
        }
        assert _scan_deep(body) is False

    def test_or_injection_in_nested_value(self) -> None:
        """嵌套值中的 OR 注入应被检测。"""
        body = {"query": {"condition": "' OR '1'='1"}}
        assert _scan_deep(body) is True

    def test_sleep_injection_in_nested_list(self) -> None:
        """嵌套 list 中的 SLEEP 注入应被检测。"""
        body = {"params": ["normal", ["SLEEP(5)"]]}
        assert _scan_deep(body) is True

    def test_comment_injection_deep(self) -> None:
        """深层注释注入应被检测。"""
        body = {"a": {"b": {"c": {"d": "value -- comment"}}}}
        assert _scan_deep(body) is True


# ============================================================
# T018-2: AI 关键词分支 SQL 参数化
# ============================================================


class TestAiKeywordSqlParameterization:
    """测试 AI 服务关键词匹配生成参数化 SQL。"""

    @pytest.mark.asyncio
    async def test_keyword_sql_uses_parameterized_placeholders(self) -> None:
        """关键词匹配应生成参数化 SQL（:param 占位符），而非 f-string 拼接。"""
        from unittest.mock import AsyncMock, MagicMock

        from app.services.ai.service import AiService

        # 创建 mock session 和 repo
        mock_session = AsyncMock()
        mock_llm = MagicMock()
        mock_llm.enabled = False  # 强制走关键词匹配路径

        service = AiService(mock_session, llm=mock_llm)

        # Mock vocabulary
        service._repo.vocabulary = AsyncMock(return_value={"sales_gmv_daily", "order_count"})

        result = await service.nl2sql("查询 sales_gmv_daily", None)

        # 验证 SQL 使用参数化占位符而非 f-string
        sql = result["sql"]
        params = result.get("params", {})

        # SQL 不应包含直接拼接的字符串值
        assert "'sales_gmv_daily'" not in sql
        # SQL 应使用 :param 占位符
        assert ":metric_code_0" in sql
        # params 应包含参数值
        assert "metric_code_0" in params
        assert params["metric_code_0"] == "sales_gmv_daily"

    @pytest.mark.asyncio
    async def test_keyword_sql_no_sql_injection_via_input(self) -> None:
        """恶意输入不应通过关键词匹配注入 SQL。"""
        from unittest.mock import AsyncMock, MagicMock

        from app.services.ai.service import AiService

        mock_session = AsyncMock()
        mock_llm = MagicMock()
        mock_llm.enabled = False

        service = AiService(mock_session, llm=mock_llm)

        # 即使词汇表中有"恶意"名称，SQL 也应是参数化的
        service._repo.vocabulary = AsyncMock(return_value={"metric_a"})

        result = await service.nl2sql("查询 metric_a", None)
        sql = result["sql"]
        params = result.get("params", {})

        # 参数值不会直接出现在 SQL 中
        assert "metric_a" not in sql or ":metric_code" in sql
        # 参数在 params 字典中
        assert len(params) > 0


# ============================================================
# T018-3: 弱密钥启动拒绝
# ============================================================


class TestWeakKeyStartupRejection:
    """测试生产环境弱密钥拒绝启动。"""

    def test_short_jwt_secret_rejected_in_prod(self) -> None:
        """jwt_secret < 32 字符时，生产环境应拒绝启动。"""
        from app.core.config import ConfigurationError

        with pytest.raises(ConfigurationError, match="JWT_SECRET"):

            # 模拟生产环境短密钥
            os.environ["UNISENSE_ENV"] = "prod"
            os.environ["UNISENSE_JWT_SECRET"] = "short_key"
            os.environ["UNISENSE_DB_URL"] = "mysql://test"

            try:
                from app.core.config import Settings
                Settings()  # 应抛出 ConfigurationError
            finally:
                os.environ.pop("UNISENSE_ENV", None)
                os.environ.pop("UNISENSE_JWT_SECRET", None)
                os.environ.pop("UNISENSE_DB_URL", None)

    def test_prod_requires_fernet_key(self) -> None:
        """生产环境必须配置独立的 Fernet 密钥。"""
        from app.core.config import ConfigurationError

        with pytest.raises(ConfigurationError, match="FERNET_KEY"):
            os.environ["UNISENSE_ENV"] = "prod"
            os.environ["UNISENSE_JWT_SECRET"] = "a" * 32
            os.environ["UNISENSE_DB_URL"] = "mysql://test"
            os.environ["UNISENSE_OLAP_URL"] = "http://doris:8030"
            # 显式置空，覆盖 .env 中可能存在的 FERNET_KEY
            os.environ["UNISENSE_FERNET_KEY"] = ""

            try:
                from app.core.config import Settings
                Settings()  # 应抛出 ConfigurationError
            finally:
                os.environ.pop("UNISENSE_ENV", None)
                os.environ.pop("UNISENSE_JWT_SECRET", None)
                os.environ.pop("UNISENSE_DB_URL", None)
                os.environ.pop("UNISENSE_OLAP_URL", None)

    def test_prod_requires_olap_url(self) -> None:
        """生产环境必须配置 OLAP URL。"""
        from app.core.config import ConfigurationError

        with pytest.raises(ConfigurationError, match="OLAP_URL"):
            os.environ["UNISENSE_ENV"] = "prod"
            os.environ["UNISENSE_JWT_SECRET"] = "a" * 32
            os.environ["UNISENSE_DB_URL"] = "mysql://test"
            os.environ["UNISENSE_FERNET_KEY"] = "test-fernet-key-for-prod"
            # 不设置 UNISENSE_OLAP_URL

            try:
                from app.core.config import Settings
                Settings()  # 应抛出 ConfigurationError
            finally:
                os.environ.pop("UNISENSE_ENV", None)
                os.environ.pop("UNISENSE_JWT_SECRET", None)
                os.environ.pop("UNISENSE_DB_URL", None)
                os.environ.pop("UNISENSE_FERNET_KEY", None)


# ============================================================
# T018-4: Fernet 降级拒绝
# ============================================================


class TestFernetDegradationRejection:
    """测试 Fernet 密钥不可从 JWT_SECRET 派生降级。"""

    def test_secrets_no_jwt_fallback_in_prod(self) -> None:
        """生产环境 _build_key 不应从 JWT_SECRET 派生。"""
        from app.core.config import ConfigurationError

        os.environ["UNISENSE_ENV"] = "prod"
        # 不设置 UNISENSE_FERNET_KEY
        os.environ.pop("UNISENSE_FERNET_KEY", None)

        try:
            from app.core.secrets import _build_key
            with pytest.raises(ConfigurationError, match="FERNET_KEY"):
                _build_key()
        finally:
            os.environ.pop("UNISENSE_ENV", None)

    def test_secrets_dev_uses_default_key(self) -> None:
        """开发环境可以使用默认开发密钥。"""
        os.environ["UNISENSE_ENV"] = "local"
        os.environ.pop("UNISENSE_FERNET_KEY", None)

        try:
            from app.core.secrets import _build_key
            key = _build_key()
            assert key is not None
            assert len(key) > 0
        finally:
            os.environ.pop("UNISENSE_ENV", None)

    def test_secrets_explicit_fernet_key_used(self) -> None:
        """显式配置的 Fernet 密钥应被使用。"""
        os.environ["UNISENSE_FERNET_KEY"] = "my-custom-fernet-key"
        try:
            import base64
            import hashlib

            from app.core.secrets import _build_key
            key = _build_key()
            expected = base64.urlsafe_b64encode(
                hashlib.sha256(b"my-custom-fernet-key").digest()
            )
            assert key == expected
        finally:
            os.environ.pop("UNISENSE_FERNET_KEY", None)
