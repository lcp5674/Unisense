"""应用配置模块。

使用 pydantic-settings 从环境变量（前缀 ``UNISENSE_``）读取配置。
对齐 DEV_GUIDE §12.2 / §12.4。
"""

from __future__ import annotations

import time as _time
from functools import lru_cache
from typing import Any

from pydantic import AliasChoices, Field, model_validator
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
    # P11 修复：.env.example/.env 历史拼写为 UNISENSE_ES_USER（pydantic extra=ignore 曾静默丢弃）
    # → 用 AliasChoices 同时兼容规范拼写 UNISENSE_ES_USERNAME 与旧拼写 UNISENSE_ES_USER。
    es_username: str = Field(
        default="",
        validation_alias=AliasChoices("UNISENSE_ES_USERNAME", "UNISENSE_ES_USER"),
    )
    es_password: str = ""
    # ES 客户端请求超时（秒）：避免慢/挂的 ES 阻塞就绪探针与调用方。工业级容错下限。
    es_request_timeout: float = 3.0

    # ---- OLAP（StarRocks / Doris，可选依赖）----
    olap_url: str = ""

    # ---- MySQL 查询降级引擎（OLAP 不可用时的只读兜底，可选依赖）----
    # 指向可执行指标口径 SQL 的 MySQL 业务库（如 E2E 业务库）；空则不启用降级。
    mysql_fallback_url: str = ""

    # ---- Doris（OLAP 引擎直连配置）----
    # 方案 A：DB 配置（query_engine_config）优先；env 兜底。doris_user/password
    # 为 Doris HTTP basic auth（可空=无认证），仅 env 直接配置时使用。
    doris_host: str = "localhost"
    doris_port: int = 8030
    doris_database: str = "unisense"
    doris_user: str = ""
    doris_password: str = ""

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

    # ---- 通知/事件日志保留策略（每日凌晨清理）----
    # 已读/已办结通知超过该天数软删；未读与 FAILED 永不清理（用户未看/待重试）
    notify_retention_days: int = 90
    # 事件日志为审计留痕，保留期独立且更长
    event_log_retention_days: int = 180
    # 采集运行历史保留期（P2-13）：终态记录超过该天数物理清理（每日凌晨）
    collection_run_retention_days: int = 90

    # ---- JWT ----
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 15

    # ---- 部署托管账号（密码由部署/env 统一托管，禁止经应用改密入口修改）----
    # 逗号分隔的用户名列表，默认内置种子管理员 admin。命中的账号在应用内
    # 「自助改密 / 管理员重置密码」一律 403 拒绝；其密码只能通过部署侧
    # （UNISENSE_SEED_ADMIN_PASSWORD + scripts/align_admin_password.py 对齐）变更，
    # 避免多会话/人工在 UI 内漂移部署口令。
    managed_accounts: str = "admin"

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
    # 采集预检（测试连接/枚举库/枚举表）是否放行私有网段。内网部署（后端与
    # 源库同内网，如 Hive 192.168.x.x）时设 UNISENSE_COLLECTOR_ALLOW_PRIVATE=true，
    # 否则 SSRF 严格模式会拒绝 RFC1918 私网导致「枚举库为空/测试连接失败」。
    # 默认 false 保持公网部署 SSRF 防护不降级。
    collector_allow_private: bool = False

    # ---- 血缘采集通道（TD §12.2）----
    # 增量采集的失效观察期：某条边连续 N 次未被来源通道确认后进入失效队列
    # （期间不直接删除，防止"本次未采到"误删真实血缘）。达到阈值后由人工
    # 在「采集通道」视图确认删除或恢复。
    lineage_stale_observation_runs: int = 3

    # ---- 血缘库级扫描（企业级批量重建，TD §12.2）----
    # 定时扫描的 SQL 目录（空=禁用定时扫描任务）；方言：空=按文件内容启发式推断。
    # worker 每日按 lineage_scan_cron 触发 scan_tasks.lineage_scan_task。
    lineage_scan_dir: str = ""
    lineage_scan_dialect: str | None = None

    model_config = SettingsConfigDict(
        env_prefix="UNISENSE_",
        env_file=".env",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _derive_doris_from_olap_url(self) -> Settings:
        """olap_url 配置后同步派生 doris_host/port/database（若仍为默认值）。

        OLAPExecutor 实际连接用 ``doris_host/port/database``（config.py:58-60），
        而生产校验强制 ``olap_url`` 非空（config.py:162）——两者此前脱节：生产设了
        ``UNISENSE_OLAP_URL`` 通过启动校验，但容器实际连 ``localhost:8030``（自身）
        → Doris 查询必失败（P0-1，第六轮工业审查）。此处从 ``olap_url`` 解析
        host/port 覆盖默认值，并允许 ``olap_url`` 含路径段（``http://fe:8030/unisense``）
        派生 database。显式配置了 ``UNISENSE_DORIS_HOST/PORT`` 时不被覆盖。
        """
        if not self.olap_url:
            return self
        from urllib.parse import urlparse

        parsed = urlparse(self.olap_url)
        host = parsed.hostname
        if not host:
            return self
        if self.doris_host in ("", "localhost"):
            self.doris_host = host
        if self.doris_port == 8030:
            self.doris_port = parsed.port or 8030
        if self.doris_database == "unisense":
            db = parsed.path.strip("/")
            if db:
                self.doris_database = db
        return self

    @model_validator(mode="after")
    def validate_production_config(self) -> Settings:
        """生产环境校验：jwt_secret≥32字符、Fernet密钥必须独立、olap_url必须非空、CORS 禁通配符。

        S3（审查修复）：原先仅 env=="prod" 精确匹配生效，staging/production 等取值或
        漏配（默认 local）会跳过全部校验。改为「非 local/dev/test 一律按生产校验」。
        """
        if self.env not in ("local", "dev", "test"):
            if len(self.jwt_secret) < 32:
                raise ConfigurationError(
                    "生产环境 UNISENSE_JWT_SECRET 必须≥32字符，当前长度="
                    f"{len(self.jwt_secret)}。请设置强密钥后重启。"
                )
            # S-1（第八轮）：拒已知默认弱凭据——compose 默认值（dev-jwt-secret.../test/
            # es_changeme/minioadmin 等）可通过长度/非空校验，但一旦暴露即被撞库。
            # 生产必须显式注入强凭据（空值=未配置属合法，不在此列）。
            weak_values = {
                "dev-jwt-secret-change-in-production-32bytes",
                "test",
                "test1234",
                "changeme",
                "es_changeme",
                "minioadmin",
                "admin",
                "password",
                "123456",
                "12345678",
                "secret",
            }
            _cred_env_map: tuple[tuple[str, str], ...] = (
                ("UNISENSE_JWT_SECRET", self.jwt_secret),
                ("UNISENSE_ES_PASSWORD", self.es_password),
                ("UNISENSE_MINIO_ACCESS_KEY", self.minio_access_key),
                ("UNISENSE_MINIO_SECRET_KEY", self.minio_secret_key),
                ("UNISENSE_NEO4J_PASSWORD", self.neo4j_password),
            )
            for var, val in _cred_env_map:
                if val and val in weak_values:
                    raise ConfigurationError(
                        f"生产环境 {var} 使用了已知默认弱凭据，请注入强凭据后重启"
                    )
            # MySQL 默认密码 test 内嵌于 db_url（compose 默认 mysql+pymysql://unisense:test@...）
            if ":test@" in self.db_url or ":test:" in self.db_url:
                raise ConfigurationError(
                    "生产环境 UNISENSE_DB_URL 使用了已知默认弱密码 test，请注入强密码后重启"
                )
            if not self.fernet_key:
                raise ConfigurationError(
                    "生产环境 UNISENSE_FERNET_KEY 必须独立配置，"
                    "禁止从 JWT_SECRET 派生降级。请设置独立的 Fernet 密钥后重启。"
                )
            # 方案 A（前端可配置化）：OLAP 连接可经 DB 配置（query_engine_config），
            # env 未配置 olap_url 不代表引擎不可用——故不再强制 olap_url 非空拒启
            # （env/DB 双空时由系统配置页提示 + consume 降级 503/MySQL fallback）。
            # 仅当 env 显式配置了 OLAP（olap_url 非空，或 doris_host 被改为非默认值）
            # 时，才校验实际连接地址不能是 localhost（默认值即容器自身，查询必失败）。
            olap_env_configured = bool(self.olap_url) or self.doris_host not in (
                "", "localhost", "127.0.0.1", "0.0.0.0"
            )
            if olap_env_configured and self.doris_host in (
                "localhost", "127.0.0.1", "0.0.0.0"
            ):
                raise ConfigurationError(
                    "生产环境 Doris 连接地址不能是 localhost/127.0.0.1——OLAPExecutor 实际"
                    "连接用 doris_host（默认 localhost 为容器自身，查询必失败）。"
                    "请通过 UNISENSE_OLAP_URL（自动派生）或 UNISENSE_DORIS_HOST/PORT "
                    "配置真实 Doris/StarRocks FE 地址，或在系统配置页配置查询引擎后重启。"
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
        except Exception as exc:
            # R11（审查修复）：热配置刷新失败不再静默——可能长期提供过期配置
            import logging

            logging.getLogger(__name__).warning(
                "feature_flag_refresh_failed_using_stale", error=str(exc)
            )

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
