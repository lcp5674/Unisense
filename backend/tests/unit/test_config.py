"""core/config.py 单测（生产配置校验分支 + CORS 解析）。

覆盖 validate_production_config 的 4 个拒绝分支与 cors_origins_list 解析。
使用 `_env_file=None` 规避根目录 .env 干扰，以环境变量精确构造 Settings。
"""

from __future__ import annotations

import pytest

from app.core.config import ConfigurationError, Settings


def _mk(**overrides: object) -> Settings:
    """构造 Settings（跳过 .env 文件；prod 默认合法值）。

    注意：pydantic-settings 的 kwargs 用**字段名**（env/db_url/jwt_secret…），
    而非环境变量名（UNISENSE_*）。
    """
    base: dict[str, object] = {
        "env": "prod",
        "db_url": "mysql+pymysql://u:p@localhost:3306/db",
        "jwt_secret": "x" * 40,
        "fernet_key": "fernet-key-for-prod",
        "olap_url": "http://doris:8030",
        "cors_origins": "https://app.example.com",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class TestValidateProductionConfig:
    def test_valid_prod_config_passes(self) -> None:
        s = _mk()
        assert s.env == "prod"
        assert s.cors_origins_list == ["https://app.example.com"]

    def test_short_jwt_secret_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match="JWT_SECRET"):
            _mk(jwt_secret="short")

    def test_missing_fernet_key_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match="FERNET_KEY"):
            _mk(fernet_key="")

    def test_missing_olap_url_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match="OLAP_URL"):
            _mk(olap_url="")

    def test_cors_wildcard_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match="通配符"):
            _mk(cors_origins="*")


class TestCorsOriginsList:
    def test_empty_string_returns_empty_list(self) -> None:
        s = _mk(cors_origins="")
        assert s.cors_origins_list == []

    def test_comma_separated_trimmed(self) -> None:
        s = _mk(cors_origins=" https://a.com , https://b.com ,")
        assert s.cors_origins_list == ["https://a.com", "https://b.com"]
