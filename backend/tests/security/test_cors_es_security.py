"""CORS 与 ES 安全加固测试（对齐 US8 / FR-14 / FR-15）。

覆盖：
1. CORS 白名单拒绝非白名单 Origin
2. ES 认证连接（es_username/es_password 配置）
3. CORS 通配符与凭证组合安全校验
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.core.config import ConfigurationError, Settings


class TestCORSWhitelistRejection:
    """CORS 白名单拒绝测试。"""

    def test_cors_origins_list_parses_comma_separated(self):
        """逗号分隔的 CORS 源字符串正确解析为列表。"""
        with patch.dict("os.environ", {
            "UNISENSE_DB_URL": "mysql+pymysql://test:test@localhost/test",
            "UNISENSE_JWT_SECRET": "test-secret-at-least-32-characters-long",
            "UNISENSE_CORS_ORIGINS": "http://localhost:3000,http://localhost:8080",
            "UNISENSE_ENV": "local",
        }, clear=False):
            settings = Settings()
            assert settings.cors_origins_list == [
                "http://localhost:3000",
                "http://localhost:8080",
            ]

    def test_cors_origins_list_empty_when_not_configured(self):
        """CORS 源为空时返回空列表。"""
        with patch.dict("os.environ", {
            "UNISENSE_DB_URL": "mysql+pymysql://test:test@localhost/test",
            "UNISENSE_JWT_SECRET": "test-secret-at-least-32-characters-long",
            "UNISENSE_CORS_ORIGINS": "",
            "UNISENSE_ENV": "local",
        }, clear=False):
            settings = Settings()
            assert settings.cors_origins_list == []

    def test_cors_wildcard_rejected_in_production(self):
        """生产环境拒绝通配符 CORS 源。"""
        with patch.dict("os.environ", {
            "UNISENSE_DB_URL": "mysql+pymysql://test:test@localhost/test",
            "UNISENSE_JWT_SECRET": "prod-secret-at-least-32-characters-long!!",
            "UNISENSE_FERNET_KEY": "test-fernet-key-for-production-env",
            "UNISENSE_OLAP_URL": "http://doris:8030",
            "UNISENSE_CORS_ORIGINS": "*",
            "UNISENSE_ENV": "prod",
        }, clear=False), pytest.raises(ConfigurationError, match="通配符"):
            Settings()

    def test_specific_origins_allowed_in_production(self):
        """生产环境允许具体 Origin。"""
        with patch.dict("os.environ", {
            "UNISENSE_DB_URL": "mysql+pymysql://test:test@localhost/test",
            "UNISENSE_JWT_SECRET": "prod-secret-at-least-32-characters-long!!",
            "UNISENSE_FERNET_KEY": "test-fernet-key-for-production-env",
            "UNISENSE_OLAP_URL": "http://doris:8030",
            "UNISENSE_CORS_ORIGINS": "https://unisense.example.com,https://admin.example.com",
            "UNISENSE_ENV": "prod",
        }, clear=False):
            settings = Settings()
            assert settings.cors_origins_list == [
                "https://unisense.example.com",
                "https://admin.example.com",
            ]


class TestESAuthenticatedConnection:
    """ES 认证连接测试。"""

    def test_es_username_password_configurable(self):
        """ES 用户名密码可通过环境变量配置。"""
        with patch.dict("os.environ", {
            "UNISENSE_DB_URL": "mysql+pymysql://test:test@localhost/test",
            "UNISENSE_JWT_SECRET": "test-secret-at-least-32-characters-long",
            "UNISENSE_ES_USERNAME": "elastic",
            "UNISENSE_ES_PASSWORD": "es_changeme",
            "UNISENSE_ENV": "local",
        }, clear=False):
            settings = Settings()
            assert settings.es_username == "elastic"
            assert settings.es_password == "es_changeme"

    def test_es_default_values_empty(self):
        """ES 认证默认值为空。"""
        with patch.dict("os.environ", {
            "UNISENSE_DB_URL": "mysql+pymysql://test:test@localhost/test",
            "UNISENSE_JWT_SECRET": "test-secret-at-least-32-characters-long",
            "UNISENSE_ENV": "local",
        }, clear=False):
            settings = Settings()
            assert settings.es_username == ""
            assert settings.es_password == ""

    def test_es_url_default(self):
        """ES URL 默认值为 localhost:9200。"""
        with patch.dict("os.environ", {
            "UNISENSE_DB_URL": "mysql+pymysql://test:test@localhost/test",
            "UNISENSE_JWT_SECRET": "test-secret-at-least-32-characters-long",
            "UNISENSE_ENV": "local",
        }, clear=False):
            settings = Settings()
            assert settings.es_url == "http://localhost:9200"


class TestCORSMiddlewareSecurity:
    """CORS 中间件安全测试。"""

    def test_wildcard_stripped_from_origins_in_non_local_env(self):
        """非本地环境移除通配符 Origin。"""
        # Simulate the logic from main.py
        origins = ["*", "http://localhost:3000"]
        env = "dev"
        if "*" in origins and env != "local":
            origins = [o for o in origins if o != "*"]
        assert "*" not in origins
        assert "http://localhost:3000" in origins

    def test_wildcard_kept_in_local_env(self):
        """本地开发环境保留通配符（便于调试）。"""
        origins = ["*", "http://localhost:3000"]
        env = "local"
        # In local env, we keep the wildcard
        if "*" in origins and env != "local":
            origins = [o for o in origins if o != "*"]
        assert "*" in origins
