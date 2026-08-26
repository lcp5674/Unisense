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

    def test_doris_localhost_rejected_in_prod(self) -> None:
        """P0-1：生产 OLAP 直连地址不能是 localhost（容器自身），否则查询必失败——
        olap_url 指向 localhost 会被派生为 doris_host=localhost，须拒绝启动。"""
        with pytest.raises(ConfigurationError, match="Doris 连接地址"):
            _mk(olap_url="http://localhost:8030")

    def test_cors_wildcard_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match="通配符"):
            _mk(cors_origins="*")

    def test_default_jwt_secret_rejected(self) -> None:
        """S-1：compose 默认 JWT 密钥（dev-jwt-secret...）长度≥32 可通过长度校验，
        但属已知默认弱凭据，生产必须拒绝。"""
        with pytest.raises(ConfigurationError, match="JWT_SECRET"):
            _mk(jwt_secret="dev-jwt-secret-change-in-production-32bytes")

    def test_default_minio_secret_rejected(self) -> None:
        """S-1：MinIO 默认凭据 minioadmin 须拒绝。"""
        with pytest.raises(ConfigurationError, match="MINIO_SECRET_KEY"):
            _mk(minio_secret_key="minioadmin")

    def test_default_es_password_rejected(self) -> None:
        """S-1：ES 默认密码 es_changeme 须拒绝。"""
        with pytest.raises(ConfigurationError, match="ES_PASSWORD"):
            _mk(es_password="es_changeme")

    def test_default_mysql_password_rejected(self) -> None:
        """S-1：db_url 内嵌默认密码 test 须拒绝。"""
        with pytest.raises(ConfigurationError, match="弱密码"):
            _mk(db_url="mysql+pymysql://unisense:test@mysql:3306/unisense")

    def test_empty_weak_field_allowed(self) -> None:
        """S-1：空值=未配置属合法（非弱凭据），不应误拒。"""
        s = _mk(es_password="", minio_secret_key="")
        assert s.env == "prod"


class TestDorisDerivedFromOlapUrl:
    """P0-1：OLAPExecutor 实际连接用 doris_host/port，生产校验却强制 olap_url 非空——
    两者脱节导致生产设了 olap_url 仍连 localhost。olap_url 配置后自动派生 doris 参数。"""

    def test_dev_olap_url_derives_doris(self) -> None:
        s = Settings(
            _env_file=None,
            env="dev",
            db_url="mysql+pymysql://u:p@localhost:3306/db",
            jwt_secret="x" * 40,
            olap_url="http://doris-fe:8030/unisense",
        )
        assert s.doris_host == "doris-fe"
        assert s.doris_port == 8030
        assert s.doris_database == "unisense"

    def test_explicit_doris_host_wins(self) -> None:
        """显式配置 doris_host 时不被 olap_url 覆盖。"""
        s = Settings(
            _env_file=None,
            env="dev",
            db_url="mysql+pymysql://u:p@localhost:3306/db",
            jwt_secret="x" * 40,
            olap_url="http://fe-a:8030",
            doris_host="fe-b",
        )
        assert s.doris_host == "fe-b"

    def test_olap_url_port_derived(self) -> None:
        s = Settings(
            _env_file=None,
            env="dev",
            db_url="mysql+pymysql://u:p@localhost:3306/db",
            jwt_secret="x" * 40,
            olap_url="http://doris:9030",
        )
        assert s.doris_port == 9030

    def test_no_olap_url_keeps_default(self) -> None:
        s = Settings(
            _env_file=None,
            env="dev",
            db_url="mysql+pymysql://u:p@localhost:3306/db",
            jwt_secret="x" * 40,
        )
        assert s.doris_host == "localhost"


class TestCorsOriginsList:
    def test_empty_string_returns_empty_list(self) -> None:
        s = _mk(cors_origins="")
        assert s.cors_origins_list == []

    def test_comma_separated_trimmed(self) -> None:
        s = _mk(cors_origins=" https://a.com , https://b.com ,")
        assert s.cors_origins_list == ["https://a.com", "https://b.com"]
