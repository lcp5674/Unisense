"""应用配置模块。

使用 pydantic-settings 从环境变量（前缀 ``UNISENSE_``）读取配置。
对齐 DEV_GUIDE §12.2 / §12.4。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # ---- 通知渠道 ----
    notify_webhook_url: str = ""

    model_config = SettingsConfigDict(
        env_prefix="UNISENSE_",
        env_file=".env",
        extra="ignore",
    )

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
