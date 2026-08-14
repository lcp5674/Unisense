"""应用配置模块。

使用 pydantic-settings 从环境变量（前缀 ``UNISENSE_``）读取配置。
对齐 DEV_GUIDE §12.2 / §12.4。
"""

from __future__ import annotations

import time as _time
from functools import lru_cache
from typing import Any

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
    # 默认 19200：docker-compose 已将 ES 避让到宿主 19200（避开本机 9200 占用）。
    es_url: str = "http://localhost:19200"
    es_username: str = ""
    es_password: str = ""
    # ES 客户端请求超时（秒）：避免慢/挂的 ES 阻塞就绪探针与调用方。工业级容错下限。
    es_request_timeout: float = 3.0

    # ---- OLAP（StarRocks / Doris，可选依赖）----
    olap_url: str = ""

    # ---- MySQL 查询降级引擎（OLAP 不可用时的只读兜底，可选依赖）----
    # 指向可执行指标口径 SQL 的 MySQL 业务库（如 E2E 业务库）；空则不启用降级。
    mysql_fallback_url: str = ""

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
    jwt_expire_minutes: int = 15

    # ---- CORS ----
    cors_origins: str = "http://localhost:3000"

    # ---- Trusted Proxies ----
    trusted_proxies: str = ""

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

    # ---- QuickBI 嵌入（FR-12：BI 报表嵌入消费，可选依赖）----
    quickbi_sign_key: str = ""  # 票据签名密钥（未配置则 ticket 接口 503 降级）
    quickbi_embed_base_url: str = ""  # 嵌入网关地址（默认 https://quickbi.aliyun.com）

    # ---- Fernet 密钥 ----
    fernet_key: str = ""

    # ---- 语义模块 ----
    metric_sunset_days: int = 30  # 指标废弃过渡天数（TD §13）
    glossary_synonym_threshold: float = 0.8  # 术语同义词冲突判定阈值（T053）

    # ---- 采集模块 ----
    # MySQL 增量采集：UPDATE_TIME IS NOT NULL 表占比低于此值时降级全量（0.0-1.0）
    # 修复前：硬编码 0.1（10%），无法根据不同数据源调整。
    # 生产建议：稳定表多的库设 0.05（5%），频繁无 UPDATE_TIME 的库设 0.2（20%）。
    collector_mysql_incremental_ratio_threshold: float = 0.1

    # ---- 血缘采集通道（TD §12.2）----
    # 增量采集的失效观察期：某条边连续 N 次未被来源通道确认后进入失效队列
    # （期间不直接删除，防止"本次未采到"误删真实血缘）。达到阈值后由人工
    # 在「采集通道」视图确认删除或恢复。
    lineage_stale_observation_runs: int = 3

    model_config = SettingsConfigDict(
        env_prefix="UNISENSE_",
        env_file=".env",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_production_config(self) -> Settings:
        """生产环境校验：jwt_secret≥32字符、Fernet密钥必须独立、olap_url必须非空、CORS 禁通配符。"""
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
            # CORS 严格校验：allow_credentials=True 时禁止通配符
            if "*" in self.cors_origins_list:
                raise ConfigurationError(
                    "生产环境 CORS 不允许通配符与 credentials=True 组合，请配置具体 Origin"
                )
            # CORS 内网地址检查（警告，不拒绝）
            internal_patterns = ("127.0.0.1", "0.0.0.0", "localhost")
            for origin in self.cors_origins_list:
                if any(p in origin for p in internal_patterns):
                    import logging

                    logging.getLogger("unisense.config").warning(
                        "cors_internal_origin_in_prod origin=%s", origin
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

    @property
    def trusted_proxies_list(self) -> list[str]:
        """将逗号分隔的 trusted_proxies 字符串拆分为列表。"""
        if not self.trusted_proxies:
            return []
        return [p.strip() for p in self.trusted_proxies.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    """获取 Settings 单例。

    Returns:
        Settings 实例。
    """
    return Settings()


settings: Settings = get_settings()


class HotSettings:
    """热配置（Redis Hash + 30s 内存缓存，对齐 R&D-08）。"""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}
        self._cache_at: float = 0.0
        self._ttl: float = 30.0

    async def refresh(self, redis_client: Any) -> None:
        now = _time.monotonic()
        if now - self._cache_at < self._ttl:
            return
        try:
            data = await redis_client.hgetall("unisense:hot_config")
            self._cache = dict(data) if data else {}
            self._cache_at = now
        except Exception:
            pass

    def get(self, key: str, default: str = "") -> str:
        return self._cache.get(key, default)


_hot_settings: HotSettings | None = None


def get_hot_settings() -> HotSettings:
    global _hot_settings
    if _hot_settings is None:
        _hot_settings = HotSettings()
    return _hot_settings


async def init_hot_settings(redis_client: object | None) -> HotSettings:
    global _hot_settings
    _hot_settings = HotSettings()
    if redis_client is not None:
        await _hot_settings.refresh(redis_client)
    return _hot_settings
