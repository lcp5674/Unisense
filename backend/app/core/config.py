"""应用配置模块。

使用 pydantic-settings 从环境变量（前缀 ``UNISENSE_``）读取配置。
对齐 DEV_GUIDE §12.2 / §12.4。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(Exception):
    """配置校验失败时抛出（拒绝启动）。"""


class Settings(BaseSettings):
    """Unisense 应用配置。

    从环境变量（前缀 ``UNISENSE_``）和 ``.env`` 文件读取配置。
    必填项缺失时 fail-fast（拒绝启动）。
    """

    # ---- 环境标识 ----
    env: str = "local"

    # ---- MySQL ----
    db_url: str

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"

    # ---- Neo4j ----
    neo4j_url: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    # ---- Elasticsearch ----
    es_url: str = "http://localhost:9200"

    # ---- OLAP（StarRocks / Doris，可选依赖）----
    olap_url: str = ""

    # ---- Doris（OLAP 引擎直连配置）----
    doris_host: str = "localhost"
    doris_port: int = 8030
    doris_database: str = "unisense"

    # ---- MinIO（S3 兼容对象存储）----
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "unisense-archive"

    # ---- 埋点 ----
    tracking_enabled: bool = False

    # ---- 通知渠道 ----
    notify_webhook_url: str = ""
    notify_dingtalk_webhook: str = ""
    notify_smtp_host: str = ""
    notify_smtp_port: int = 587
    notify_smtp_user: str = ""
    notify_smtp_password: str = ""

    # ---- JWT ----
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # ---- CORS ----
    cors_origins: str = "http://localhost:3000"

    # ---- 日志 ----
    log_level: str = "INFO"
    log_format: str = "json"

    # ---- OpenTelemetry ----
    otlp_endpoint: str = ""

    # ---- LLM ----
    llm_default_model: str = "deepseek-chat"
    llm_api_key: str = ""
    llm_base_url: str = ""

    # ---- KMS ----
    kms_key_id: str = ""

    # ---- Fernet 密钥 ----
    fernet_key: str = ""

    model_config = SettingsConfigDict(
        env_prefix="UNISENSE_",
        env_file=".env",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_production_config(self) -> Settings:
        """生产环境校验：jwt_secret≥32字符、Fernet密钥必须独立、olap_url必须非空。"""
        if self.env == "prod":
            if len(self.jwt_secret) < 32:
                raise ConfigurationError(
                    "生产环境 UNISENSE_JWT_SECRET 必须≥32字符，当前长度="
                    f"{len(self.jwt_secret)}。请设置强密钥后重启。"
                )
            if not self.fernet_key:
                raise ConfigurationError(
                    "生产环境 UNISENSE_FERNET_KEY 必须独立配置，"
                    "禁止从 JWT_SECRET 派生降级。请设置独立的 Fernet 密钥后重启。"
                )
            if not self.olap_url:
                raise ConfigurationError(
                    "生产环境 UNISENSE_OLAP_URL 必须非空，"
                    "consume 查询需要 OLAP 执行引擎。请配置 Doris/StarRocks 地址后重启。"
                )
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        """将逗号分隔的 CORS 源字符串拆分为列表。

        Returns:
            允许的 Origin 列表。
        """
        if not self.cors_origins:
            return []
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """获取 Settings 单例。

    Returns:
        Settings 实例。
    """
    return Settings()


settings: Settings = get_settings()
