"""采集领域服务（对齐 TD §12.1 / DEV_GUIDE §8b.1）。

职责：数据源注册/查询/删除、元数据注册（含敏感分级与幂等）、批量废弃（207）、
自动采集编排。所有写操作经 SecretManager 加密凭据、经审计落库、经事件发布（熔断）。
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import sqlglot
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.base_service import BaseService
from app.core.config import Settings
from app.core.exceptions import (
    BusinessError,
    ConflictError,
    ExternalDependencyError,
    NotFoundError,
    UnisenseError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.secrets import SecretManager
from app.db.redis import get_redis
from app.models.collector_models import SchemaDriftLog
from app.models.data_source import ColumnDescription, DataSource
from app.services.collector.classifier import SensitivityClassifier
from app.services.collector.events import CatalogEventPublisher
from app.services.collector.queue import CollectionQueue, create_collection_queue
from app.services.collector.repository import CollectorRepository
from app.services.collector.schemas import (
    BatchSourceItem,
    BatchSourceResult,
    BulkDeprecateRequest,
    BulkDeprecateResult,
    DataSourceCreateRequest,
    DataSourceResponse,
    DataSourceTypeInfo,
    DataSourceUpdateRequest,
    DBCatalogCreateRequest,
    DBCatalogListParams,
    DBCatalogListResponse,
    DBCatalogResponse,
    TestConnectionResult,
)
from app.services.collector.spi import BaseCollector, CatalogSpec, CollectResult
from app.services.llm.client import (
    DeterministicFallbackLlmClient,
    LlmClient,
    LlmError,
    LlmRouterClient,
)

logger = get_logger("unisense.collector.service")

#: 连接配置中的敏感凭据键名提示（编辑回显「二次确认」时保留未提交的旧值）。
#: 覆盖各连接器：mysql/postgres/clickhouse 的 ``password``，kafka 的
#: ``sasl_password``/``registry_password``/``auth_password`` 等。
_SECRET_KEY_HINTS = (
    "password",
    "passwd",
    "pwd",
    "sasl_password",
    "registry_password",
    "auth_password",
    "access_key",
    "api_key",
    "secret",
    "token",
    "credential",
)


def _is_secret_key(key: str) -> bool:
    """判断配置键是否为敏感凭据键（命中任一提示词子串）。"""
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def _sanitize_conn_error(message: str) -> str:
    """连接错误脱敏：移除 DSN/URL 内嵌凭据（user:password@、password= 等）。

    正常驱动错误串不含密码，但 DSN 或异常文本可能把账号密码带进
    ``last_error`` / 探活响应 ``message``——统一脱敏防凭据回显（TD §13）。
    """
    if not message:
        return message
    # scheme://user:pass@host → scheme://***:***@host
    text = re.sub(r"(://)([^/@:\s]+):([^/@\s]+)@", r"\1***:***@", message)
    # password=xxx / passwd=xxx / api_key=xxx（值含非空白）
    text = re.sub(
        r"(?i)(password|passwd|pwd|sasl_password|api_key|secret)\s*=\s*[^\s&;\"']+",
        r"\1=***",
        text,
    )
    return text


def _hit_to_dict(hit: Any) -> dict[str, Any]:
    """PiiFieldHit → classification.pii_columns 存储结构。

    含列名/类别/规则/置信度/匹配途径，以及命中字段的脱敏样本值（``sample``，
    已打码）——样本是 ``name+sample`` 双重验证的证据来源，供治理端复核误报时
    查看「这个字段实际存了什么」而无需回源查询。
    """
    return {
        "column": hit.column,
        "category": hit.category,
        "rule": hit.rule,
        "confidence": round(float(hit.confidence), 4),
        "matched_by": hit.matched_by,
        "sample": getattr(hit, "sample", "") or "",
    }


# PRD §4.13 健康状态机参数：滑动窗口上限与降级成功率阈值
_HEALTH_WINDOW = 20
_HEALTH_DEGRADED_RATE = 0.95

# FR-023: 描述推断的 JSON Schema 强约束（strict 模式保证字段名/类型一致），
# 网关不支持 json_schema 时降级为 json_object（见 _infer_description_structured）。
_DESCRIPTION_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "description_result",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["description", "confidence"],
            "properties": {
                "description": {"type": "string"},
                "confidence": {"type": "number"},
            },
        },
    },
}
_JSON_OBJECT_FORMAT: dict[str, Any] = {"type": "json_object"}
# 批量推断（一次调用返回多个字段）的 JSON Schema 强约束：数组元素也锁定
# column_name/description/confidence 结构，保证逐字段对齐。
_BATCH_DESCRIPTION_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "batch_description_result",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["descriptions"],
            "properties": {
                "descriptions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["column_name", "description", "confidence"],
                        "properties": {
                            "column_name": {"type": "string"},
                            "description": {"type": "string"},
                            "confidence": {"type": "number"},
                        },
                    },
                }
            },
        },
    },
}
# 格式重试提示：解析为 None 时追加，迫使模型收敛到合规 JSON（最多重试 1 次）。
_STRICT_JSON_HINT = "请严格只输出符合 JSON Schema 的 JSON，不要任何额外文字。"

# FR-023: LLM 分类错误 metric 计数器（进程内，非 Redis/Statsd）
_llm_classify_error_counts: dict[str, int] = {}


def _record_llm_error_metric(error_type: str) -> None:
    """记录 llm_classify_error_total metric。

    Args:
        error_type: 错误类型（timeout/format_error/runtime_error）。
    """
    global _llm_classify_error_counts
    _llm_classify_error_counts[error_type] = _llm_classify_error_counts.get(error_type, 0) + 1


def get_llm_classify_error_total() -> dict[str, int]:
    """获取 LLM 分类错误计数（供测试与可观测性使用）。"""
    return dict(_llm_classify_error_counts)


def _redis_available() -> bool:
    """检查 Redis 连接池是否已初始化。"""
    try:
        get_redis()
        return True
    except RuntimeError:
        return False


class CollectorService(BaseService):
    """采集领域服务。"""

    def __init__(
        self,
        db: AsyncSession,
        *,
        secrets: type[SecretManager] | SecretManager | None = None,
        events: CatalogEventPublisher | None = None,
        classifier: SensitivityClassifier | None = None,
    ) -> None:
        super().__init__(db)
        self._db = db
        self._repo = CollectorRepository(db)
        # secrets 作为工具类使用（静态方法），缺省用 SecretManager
        self._secrets = secrets or SecretManager
        self._events = events or CatalogEventPublisher(get_redis() if _redis_available() else None)
        self._classifier = classifier or SensitivityClassifier()
        # DB 规则加载仅执行一次（惰性；无 pii_rule 配置时回退内置默认）
        self._db_rules_loaded = False

    async def _maybe_load_db_rules(self) -> None:
        """从 system_dict 加载可配置敏感规则（仅首次执行；失败回退内置默认）。

        PII 合规增强 C-1：管理员在系统字典「PII 规则」配置的规则覆盖内置默认。
        best-effort：DB 无配置/读取异常均保持内置规则，不阻断采集。
        """
        if self._db_rules_loaded:
            return
        self._db_rules_loaded = True
        try:
            from app.services.collector.rules import load_pii_rules, load_pii_vocab

            pii_rules, conf_rules = await load_pii_rules(self._db)
            vocab = await load_pii_vocab(self._db)
            if pii_rules:
                self._classifier = SensitivityClassifier(
                    rules=pii_rules, confidential_rules=conf_rules, vocab=vocab
                )
                logger.info("collector_use_db_pii_rules count=%d", len(pii_rules))
        except Exception as exc:  # noqa: BLE001 - 规则加载失败不阻断采集
            logger.warning("collector_db_rules_load_failed: %s", exc)
        self._settings = Settings()

    # ---- 健康状态机（PRD §4.13：ACTIVE → DEGRADED → UNAVAILABLE）----

    @staticmethod
    def _evaluate_health_after_collect(
        prev: dict[str, Any] | None, attempted: int, failed: int
    ) -> tuple[str, dict[str, Any], datetime | None]:
        """采集后健康状态机：失败率 ≥5% → DEGRADED（黄态），否则 healthy。

        health_metrics 用滑动窗口计数（上限 ``_HEALTH_WINDOW`` 次采样，
        超限整体减半），近期失败会被放大、历史噪声被稀释。

        Returns:
            (status, metrics, degraded_since)。恢复 healthy 时 degraded_since
            为 None（repository 会清空历史值）。
        """
        ok_count = int((prev or {}).get("ok_count", 0))
        fail_count = int((prev or {}).get("fail_count", 0))
        sample_count = int((prev or {}).get("sample_count", 0))
        ok_count += max(0, attempted - failed)
        fail_count += max(0, failed)
        sample_count += max(0, attempted)
        if sample_count > _HEALTH_WINDOW:
            ok_count //= 2
            fail_count //= 2
            sample_count //= 2
        total = ok_count + fail_count
        success_rate = ok_count / total if total else 1.0
        status = "degraded" if (total and success_rate < _HEALTH_DEGRADED_RATE) else "healthy"
        metrics = {
            "ok_count": ok_count,
            "fail_count": fail_count,
            "sample_count": sample_count,
            "success_rate": round(success_rate, 4),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        degraded_since = datetime.now(UTC) if status == "degraded" else None
        return status, metrics, degraded_since

    @staticmethod
    def _to_source_response(src: DataSource, include_config: bool = False) -> DataSourceResponse:
        """ORM → 响应。

        Args:
            include_config: True 时解密并携带 ``connection_config`` 明文
                （仅详情/编辑回显用）；False（列表）时保持 ``None`` 脱敏。
        """
        config: dict[str, Any] | None = None
        if include_config and src.connection_config:
            try:
                config = SecretManager.decrypt(src.connection_config)
            except Exception:
                # 密钥漂移等导致解密失败：不阻断详情，按未配置处理
                config = None
        return DataSourceResponse(
            source_id=src.source_id,
            name=src.name,
            source_type=src.source_type,
            domain=src.domain,
            cluster_id=src.cluster_id,
            coverage=float(src.coverage or 0.0),
            health_status=src.health_status or "unknown",
            connection_config_present=bool(src.connection_config),
            connection_config=config,
            databases=getattr(src, "databases", None),
            schedule_cron=src.schedule_cron,
            schedule_enabled=bool(getattr(src, "schedule_enabled", True)),
            collection_mode=src.collection_mode or "FULL",
            enabled=bool(getattr(src, "enabled", True)),
            created_by=src.created_by,
            created_at=src.created_at,
            updated_at=src.updated_at,
            owner_id=getattr(src, "owner_id", None),
            description=getattr(src, "description", None),
            include_patterns=getattr(src, "include_patterns", None),
            exclude_patterns=getattr(src, "exclude_patterns", None),
            health_metrics=getattr(src, "health_metrics", None),
            degraded_since=getattr(src, "degraded_since", None),
            quota=src.quota or {},
        )

    async def create_source(
        self, req: DataSourceCreateRequest, actor_id: int, org_id: int | None = None
    ) -> DataSourceResponse:
        # 验证 source_type 在 CollectorRegistry 中已注册
        from app.services.collector.connectors import registry

        available_types = registry.list_types()
        source_type_value = (
            req.source_type.value if hasattr(req.source_type, "value") else str(req.source_type)
        )
        if source_type_value not in available_types:
            raise BusinessError(
                f"不支持的采集器类型: {source_type_value}，已注册类型: {available_types}",
                error_code="UNSUPPORTED_COLLECTOR",
            )

        # 生产约定：source_id 未传时按 类型_库|域 自动生成，冲突自增后缀
        source_id = req.source_id
        if not source_id:
            source_id = await self._generate_unique_source_id(
                source_type_value, req.connection_config, req.domain
            )
        elif await self._repo.get_source(source_id) is not None:
            raise ConflictError(f"数据源已存在: {source_id}")

        encrypted = self._secrets.encrypt(req.connection_config)
        src = DataSource(
            source_id=source_id,
            name=req.name,
            source_type=source_type_value,
            connection_config=encrypted,
            databases=req.databases or None,
            collection_mode=req.collection_mode or "FULL",
            domain=req.domain,
            cluster_id=req.cluster_id,
            coverage=0.0,
            health_status="unknown",
            quota={},
            created_by=actor_id,
            org_id=org_id,  # 多租户隔离：创建时归属当前用户组织（P1 加固）
        )
        try:
            await self._repo.create_source(src)
        except IntegrityError as exc:
            # P0-3/P2-2: 检查-插入竞态下软删遗留 ID 被占用 → 归一为 409（非 500）
            await self._db.rollback()
            raise ConflictError(f"数据源已存在: {source_id}") from exc
        return self._to_source_response(src)

    @staticmethod
    def _generate_source_id(source_type: str, cfg: dict[str, Any], domain: str) -> str:
        """按 ``{类型}_{库|schema|域}`` 生成规范化 source_id。

        优先取 connection_config.database；postgres 场景可退化为 schema；
        均缺失时回退到业务 domain。统一小写、非字母数字折叠为下划线、截断至 64。
        """
        import re

        base = cfg.get("database") or cfg.get("schema") or domain or "default"
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", str(base)).strip("_").lower()
        normalized = normalized or "default"
        return f"{source_type}_{normalized}"[:64]

    async def _generate_unique_source_id(
        self, source_type: str, cfg: dict[str, Any], domain: str
    ) -> str:
        """生成唯一 source_id：冲突时追加 ``_2/_3/...`` 后缀（上限 100 次）。"""
        base_id = self._generate_source_id(source_type, cfg, domain)
        candidate = base_id
        n = 2
        while await self._repo.get_source(candidate) is not None:
            suffix = f"_{n}"
            candidate = f"{base_id[: 64 - len(suffix)]}{suffix}"
            n += 1
            if n > 100:
                raise BusinessError(
                    f"无法为 {source_type} 生成唯一 source_id，请手动指定",
                    error_code="SOURCE_ID_EXHAUSTED",
                )
        return candidate

    async def list_source_types(self) -> list[DataSourceTypeInfo]:
        """返回全部已注册采集器类型的元信息（供前端动态渲染）。"""
        from app.services.collector.connectors import registry

        return registry.list_type_info()

    async def test_connection(self, source_type: str, cfg: dict[str, Any]) -> TestConnectionResult:
        """连接预检（创建前）：明文配置构建采集器并轻量探活，不落库。

        任何异常（含类型未注册、连接失败）都归一为 ``ok=False`` 结果，不抛出。
        """
        from app.services.collector.connectors import registry

        try:
            collector = registry.build_from_cfg(
                source_type, cfg, allow_private=self._settings.collector_allow_private
            )
            try:
                probe = await collector.probe()
            finally:
                await collector.dispose()
        except Exception as exc:  # 类型未注册 / 构建失败 / 探活异常
            logger.warning("test_connection_failed: type=%s err=%s", source_type, exc)
            return TestConnectionResult(
                ok=False,
                source_type=source_type,
                error=_sanitize_conn_error(str(exc)),
            )
        return TestConnectionResult(
            ok=probe.ok,
            source_type=source_type,
            latency_ms=probe.latency_ms,
            error=probe.error,
            detail=probe.detail,
        )

    async def list_databases(self, source_type: str, cfg: dict[str, Any]) -> list[str]:
        """枚举实例下可采集的非系统数据库（创建数据源时选择目标库）。

        明文配置构建采集器（与 test_connection 一致）。连接器不支持枚举
        （如 Kafka，spi 默认返回空列表）→ 返回空，前端回退为手填；
        **真实连接失败（2026-08-28 起）抛出明确错误**——此前静默返回空，
        前端无法区分「无库」与「连接失败」（用户误以为实例无库可采集）。

        Args:
            source_type: 采集器类型。
            cfg: 明文连接配置（不落库）。

        Returns:
            非系统数据库名列表。

        Raises:
            UnisenseError: 连接器实例化/探测失败（明确错误码，前端可展示）。
        """
        from app.core.exceptions import UnisenseError as _UnisenseError
        from app.services.collector.connectors import registry

        try:
            collector = registry.build_from_cfg(
                source_type, cfg, allow_private=self._settings.collector_allow_private
            )
            try:
                return await collector.list_databases()
            finally:
                await collector.dispose()
        except _UnisenseError:
            raise
        except Exception as exc:
            logger.warning("list_databases_failed: type=%s err=%s", source_type, exc)
            raise _UnisenseError(
                f"枚举数据库失败（{source_type} 连接异常），请检查连接配置后重试",
                error_code="LIST_DATABASES_FAILED",
                ctx={"source_type": source_type, "detail": _sanitize_conn_error(str(exc))},
            ) from exc

    async def list_tables(
        self,
        source_type: str,
        cfg: dict[str, Any],
        databases: list[str] | None = None,
    ) -> dict[str, list[str]]:
        """枚举指定库下的表（按库分组，创建数据源时级联选表）。

        与 ``list_databases`` 同构（明文配置不落库）；连接器不支持枚举表
        （如 Kafka）或任何异常均返回空字典，前端隐藏表级选择区。

        Args:
            source_type: 采集器类型。
            cfg: 明文连接配置（不落库）。
            databases: 要枚举表的库列表；空则由连接器回退全部非系统库。

        Returns:
            按库分组的表名映射 ``{库: [表名...]}``。
        """
        from app.services.collector.connectors import registry

        try:
            collector = registry.build_from_cfg(
                source_type, cfg, allow_private=self._settings.collector_allow_private
            )
            try:
                return await collector.list_tables(databases)
            finally:
                await collector.dispose()
        except Exception as exc:
            logger.warning("list_tables_failed: type=%s dbs=%s err=%s", source_type, databases, exc)
            return {}

    async def check_connection(self, source_id: str, org_id: int | None = None) -> TestConnectionResult:
        """存量数据源实时探活：解密配置 → 轻量连接 → 更新健康状态与探活时间。"""
        from app.services.collector.connectors import registry

        src = await self._repo.get_source(source_id, org_id=org_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        try:
            # 已存数据源探活：放行私有网段（内网库），仍拒回环/链路本地/保留
            collector = registry.build(src.source_type, src.connection_config, allow_private=True)
            try:
                probe = await collector.probe()
            finally:
                await collector.dispose()
        except Exception as exc:
            # P1-3: 探活失败记录错误信息（供健康端点返回 last_error）；脱敏防凭据回显
            err_text = _sanitize_conn_error(str(exc))
            await self._repo.update_health_status(source_id, "unhealthy", error=err_text)
            logger.warning("check_connection_failed: source=%s err=%s", source_id, exc)
            return TestConnectionResult(
                ok=False, source_type=src.source_type, error=err_text
            )
        new_status = "healthy" if probe.ok else "unhealthy"
        # P1-3: 探活失败（probe.ok=False）时回填 probe.error 到健康状态
        await self._repo.update_health_status(
            source_id, new_status, error=None if probe.ok else probe.error
        )
        return TestConnectionResult(
            ok=probe.ok,
            source_type=src.source_type,
            latency_ms=probe.latency_ms,
            error=probe.error,
            detail=probe.detail,
        )

    async def list_sources(
        self,
        *,
        domain: str | None = None,
        source_type: str | None = None,
        keyword: str | None = None,
        health_status: str | None = None,
        owner_id: int | None = None,
        source_status: str | None = None,
        org_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[DataSourceResponse], int]:
        sources, total = await self._repo.list_sources(
            domain=domain,
            source_type=source_type,
            keyword=keyword,
            health_status=health_status,
            owner_id=owner_id,
            source_status=source_status,
            org_id=org_id,
            page=page,
            page_size=page_size,
        )
        items = [self._to_source_response(s) for s in sources]
        # 三期：批量回填列表信号（表数/PII 数/最近采集/漂移数），一次聚合避免 N+1
        signals = await self._repo.list_sources_signals([s.source_id for s in sources])
        for item in items:
            sig = signals.get(item.source_id, {})
            item.table_count = sig.get("table_count")
            item.pii_count = sig.get("pii_count")
            last = sig.get("last_collected_at")
            item.last_collected_at = last.isoformat() if last else None
            item.drift_count = sig.get("drift_count")
            item.scanned_count = sig.get("scanned_count")
            item.failed_count = sig.get("failed_count")
        return items, total

    async def get_source(
        self, source_id: str, include_config: bool = False, org_id: int | None = None
    ) -> DataSourceResponse:
        src = await self._repo.get_source(source_id, org_id=org_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        # 明文连接配置仅平台管理员可读（由 API 层按角色决定 include_config）；
        # 其余角色保持脱敏（connection_config=None，仅 connection_config_present 标记）。
        return self._to_source_response(src, include_config=include_config)

    @staticmethod
    def _merge_preserved_secrets(
        existing_encrypted: str | None, incoming: dict[str, Any]
    ) -> dict[str, Any]:
        """编辑态「二次确认」：合并新配置时保留未提交的敏感凭据。

        非平台管理员无法读取明文密码（脱敏），编辑弹窗中密码字段为空表示
        「保持原密码」——将新配置与解密后的旧配置合并，敏感键在传入值为空/
        缺失时沿用旧值，避免编辑其他字段时把凭据清空或覆盖。

        Args:
            existing_encrypted: 现有加密配置（Fernet 密文，可能为 None）。
            incoming: 前端提交的新配置。

        Returns:
            合并后的明文配置（由调用方整体加密落库）。
        """
        if not existing_encrypted:
            return incoming
        try:
            existing = SecretManager.decrypt(existing_encrypted)
        except Exception:
            # 密钥漂移等导致无法解密旧配置：按新配置落库（不阻断编辑）
            return incoming
        merged = dict(incoming)
        for key, value in existing.items():
            if _is_secret_key(key) and not merged.get(key):
                merged[key] = value
        return merged

    async def update_source(
        self, source_id: str, req: DataSourceUpdateRequest, actor_id: int, org_id: int | None = None
    ) -> DataSourceResponse:
        """更新数据源（PATCH 语义：仅更新传入字段）。

        - ``source_id`` 不可变更（由路径参数唯一确定）。
        - ``source_type`` 变更时校验已在 CollectorRegistry 注册。
        - ``connection_config`` 变更时重新加密落库；其余字段直接覆盖。

        Args:
            source_id: 数据源标识（路径参数）。
            req: 更新请求（字段可选）。
            actor_id: 操作人 ID（审计用）。

        Raises:
            NotFoundError: 数据源不存在。
            BusinessError: source_type 未注册。
        """
        src = await self._repo.get_source(source_id, org_id=org_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")

        if req.source_type is not None:
            from app.services.collector.connectors import registry

            source_type_value = (
                req.source_type.value if hasattr(req.source_type, "value") else str(req.source_type)
            )
            if source_type_value not in registry.list_types():
                raise BusinessError(
                    f"不支持的采集器类型: {source_type_value}，已注册类型: {registry.list_types()}",
                    error_code="UNSUPPORTED_COLLECTOR",
                )
            src.source_type = source_type_value

        if req.connection_config is not None:
            cfg = dict(req.connection_config)
            # Med 9 防御：空 host 视为「未提供」——非平台管理员编辑时 host 脱敏
            # 不回显，若前端提交 host:"" 会覆盖真实 host 使源不可用。host 为连接
            # 必需字段（创建时必填），空值合法场景不存在，故从旧配置保留真实 host。
            if not str(cfg.get("host") or "").strip():
                try:
                    old_cfg = (
                        SecretManager.decrypt(src.connection_config)
                        if src.connection_config
                        else {}
                    )
                except Exception:  # noqa: BLE001 - 旧配置解密失败按空处理
                    old_cfg = {}
                cfg["host"] = old_cfg.get("host") or ""
            merged = self._merge_preserved_secrets(src.connection_config, cfg)
            src.connection_config = self._secrets.encrypt(merged)
        if req.name is not None:
            src.name = req.name
        if req.domain is not None:
            src.domain = req.domain
        if req.cluster_id is not None:
            src.cluster_id = req.cluster_id
        if req.enabled is not None:
            src.enabled = req.enabled
        # 多目标库（PATCH 语义：[] 表示清空回全部库/单库配置）
        if req.databases is not None:
            src.databases = req.databases or None
        # 默认采集模式（PATCH 语义：None 不修改）
        if req.collection_mode is not None:
            src.collection_mode = req.collection_mode
        # 治理字段：owner_id / description / include / exclude patterns（PATCH 语义）
        if (
            req.owner_id is not None
            or req.description is not None
            or req.include_patterns is not None
            or req.exclude_patterns is not None
        ):
            await self._repo.set_source_governance(
                source_id,
                owner_id=req.owner_id,
                description=req.description,
                include_patterns=req.include_patterns,
                exclude_patterns=req.exclude_patterns,
            )
        if req.quota is not None:
            src.quota = req.quota
        # updated_at 由 BaseModel.onupdate 自动维护；连接配置变更后健康状态重置
        # （旧探活结果对新凭据不再可信），并清空历史错误。
        if req.connection_config is not None:
            src.health_status = "unknown"
            src.last_error = None
        await self._db.flush()
        return self._to_source_response(src)

    async def delete_source(self, source_id: str, org_id: int | None = None) -> None:
        if not await self._repo.soft_delete_source(source_id, org_id=org_id):
            raise NotFoundError(f"数据源不存在: {source_id}")

    async def batch_toggle_sources(
        self, source_ids: list[str], enabled: bool, actor_id: int, org_id: int | None = None
    ) -> BatchSourceResult:
        """批量启用/停用数据源（逐条独立处理，单条失败不影响其余）。

        对齐 bulk_deprecate 的 207 语义：不存在的源记为 NOT_FOUND 失败项，
        其余逐条更新 enabled。成功项携带 name 供前端提示。

        Args:
            source_ids: 待操作数据源 ID 列表（API 层已按 max_length=200 校验）。
            enabled: True 启用 / False 停用。
            actor_id: 操作人 ID（审计用，预留）。

        Returns:
            BatchSourceResult(succeeded, failed)。
        """
        succeeded: list[BatchSourceItem] = []
        failed: list[BatchSourceItem] = []
        for sid in source_ids:
            try:
                src = await self._repo.set_source_enabled(sid, enabled, org_id=org_id)
                if src is None:
                    failed.append(
                        BatchSourceItem(
                            source_id=sid,
                            ok=False,
                            error_code="NOT_FOUND",
                            message="数据源不存在",
                        )
                    )
                    continue
                succeeded.append(BatchSourceItem(source_id=sid, name=src.name, ok=True))
            except Exception as exc:  # noqa: BLE001 - 批量单条失败不阻断其余（207 语义）
                failed.append(
                    BatchSourceItem(
                        source_id=sid,
                        ok=False,
                        error_code="INTERNAL",
                        message=str(exc),
                    )
                )
        return BatchSourceResult(succeeded=succeeded, failed=failed)

    async def batch_delete_sources(
        self, source_ids: list[str], actor_id: int, org_id: int | None = None
    ) -> BatchSourceResult:
        """批量删除数据源（软删，逐条独立处理，单条失败不影响其余）。

        Args:
            source_ids: 待删除数据源 ID 列表（API 层已按 max_length=200 校验）。
            actor_id: 操作人 ID（审计用，预留）。

        Returns:
            BatchSourceResult(succeeded, failed)。
        """
        succeeded: list[BatchSourceItem] = []
        failed: list[BatchSourceItem] = []
        for sid in source_ids:
            try:
                src = await self._repo.get_source(sid, org_id=org_id)
                if src is None:
                    failed.append(
                        BatchSourceItem(
                            source_id=sid,
                            ok=False,
                            error_code="NOT_FOUND",
                            message="数据源不存在",
                        )
                    )
                    continue
                if not await self._repo.soft_delete_source(sid):
                    failed.append(
                        BatchSourceItem(
                            source_id=sid,
                            ok=False,
                            error_code="DELETE_FAILED",
                            message="删除失败",
                        )
                    )
                    continue
                succeeded.append(BatchSourceItem(source_id=sid, name=src.name, ok=True))
            except Exception as exc:  # noqa: BLE001 - 批量单条失败不阻断其余（207 语义）
                failed.append(
                    BatchSourceItem(
                        source_id=sid,
                        ok=False,
                        error_code="INTERNAL",
                        message=str(exc),
                    )
                )
        return BatchSourceResult(succeeded=succeeded, failed=failed)

    async def batch_test_sources(
        self, source_ids: list[str], actor_id: int
    ) -> BatchSourceResult:
        """批量探活（207 语义）：用已存连接配置逐条 probe，逐条独立异常隔离。

        探活成功更新 healthy（清空错误），失败更新 unhealthy 并附错误。
        """
        succeeded: list[BatchSourceItem] = []
        failed: list[BatchSourceItem] = []
        from app.services.collector.connectors import registry

        for sid in source_ids:
            try:
                src = await self._repo.get_source(sid)
                if src is None:
                    failed.append(
                        BatchSourceItem(
                            source_id=sid,
                            ok=False,
                            error_code="NOT_FOUND",
                            message="数据源不存在",
                        )
                    )
                    continue
                if not src.connection_config:
                    failed.append(
                        BatchSourceItem(
                            source_id=sid,
                            name=src.name,
                            ok=False,
                            error_code="NO_CONFIG",
                            message="无连接配置",
                        )
                    )
                    continue
                cfg = self._secrets.decrypt(src.connection_config)
                # 已存数据源批量探活：放行私有网段（内网库），仍拒回环/链路本地/保留
                collector = registry.build_from_cfg(src.source_type, cfg, allow_private=True)
                try:
                    probe = await collector.probe()
                finally:
                    await collector.dispose()
                if probe.ok:
                    await self._repo.update_health_status(sid, "healthy")
                    succeeded.append(
                        BatchSourceItem(source_id=sid, name=src.name, ok=True)
                    )
                else:
                    # 脱敏防凭据回显（DSN/URL 内嵌 user:password 等）
                    err_text = _sanitize_conn_error(str(probe.error))
                    await self._repo.update_health_status(
                        sid, "unhealthy", error=err_text
                    )
                    # 三梯队通知：数据源连接失败定向通知源 Owner（best-effort）
                    await self._notify_source_owner_failure(
                        "catalog.connection_failed",
                        "数据源连接失败",
                        sid,
                        reason=err_text[:500],
                        src=src,
                    )
                    failed.append(
                        BatchSourceItem(
                            source_id=sid,
                            name=src.name,
                            ok=False,
                            error_code="PROBE_FAILED",
                            message=err_text,
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - 批量单条失败不阻断其余（207 语义）
                failed.append(
                    BatchSourceItem(
                        source_id=sid,
                        ok=False,
                        error_code="PROBE_ERROR",
                        message=str(exc),
                    )
                )
        return BatchSourceResult(succeeded=succeeded, failed=failed)

    async def batch_schedule_sources(
        self, source_ids: list[str], schedule_cron: str, actor_id: int
    ) -> BatchSourceResult:
        """批量设置调度 cron（207 语义，逐条独立处理）。"""
        succeeded: list[BatchSourceItem] = []
        failed: list[BatchSourceItem] = []
        for sid in source_ids:
            try:
                src = await self._repo.get_source(sid)
                if src is None:
                    failed.append(
                        BatchSourceItem(
                            source_id=sid,
                            ok=False,
                            error_code="NOT_FOUND",
                            message="数据源不存在",
                        )
                    )
                    continue
                src.schedule_cron = schedule_cron
                # 批量设置调度视为显式启用调度（与单源 /schedule 语义一致）
                src.schedule_enabled = True
                await self._db.flush()
                succeeded.append(
                    BatchSourceItem(source_id=sid, name=src.name, ok=True)
                )
            except Exception as exc:  # noqa: BLE001 - 批量单条失败不阻断其余（207 语义）
                failed.append(
                    BatchSourceItem(
                        source_id=sid,
                        ok=False,
                        error_code="INTERNAL",
                        message=str(exc),
                    )
                )
        return BatchSourceResult(succeeded=succeeded, failed=failed)

    async def get_source_orm(self, source_id: str, org_id: int | None = None) -> DataSource:
        """取原始 DataSource（供采集编排还原连接配置，不对外暴露明文）。

        S1 多租户隔离：org_id 非 None 时按组织过滤，跨组织返回 NOT_FOUND。
        """
        src = await self._repo.get_source(source_id, org_id=org_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        return src

    # ------------------------------------------------------------ 只读 SQL 查询

    @staticmethod
    def _validate_read_sql(sql: str) -> str:
        """校验 SQL 为单条只读 SELECT，返回原语句（未加 LIMIT，由调用方追加）。

        Raises:
            ValidationError: 多语句 / 非 SELECT / SELECT INTO / 无法解析。
        """
        try:
            asts = sqlglot.parse(sql)
        except Exception as exc:  # noqa: BLE001 - sqlglot 对部分畸形 SQL 抛 ParseError
            raise ValidationError(f"SQL 无法解析（{exc}），仅支持单条只读 SELECT 查询") from exc
        if not asts or any(ast is None for ast in asts):
            raise ValidationError("SQL 无法解析，仅支持单条只读 SELECT 查询")
        if len(asts) != 1:
            raise ValidationError("仅支持单条 SQL 语句（不允许分号分隔的多语句）")
        ast = asts[0]
        if not isinstance(ast, sqlglot.exp.Select):
            raise ValidationError("仅支持只读 SELECT 查询（不允许 DDL/DML 等写操作）")
        if ast.args.get("into") is not None:
            raise ValidationError("不支持 SELECT INTO（禁止写文件/变量）")
        return sql

    async def query_sql(
        self, source_id: str, sql: str, limit: int = 100
    ) -> dict[str, Any]:
        """对已注册数据源执行只读 SELECT（平台内部运维/分析，调用方负责审计）。

        - 用 sqlglot 校验为单条只读 SELECT（拒绝多语句 / DDL / DML / SELECT INTO）。
        - 语句无顶层 LIMIT 时追加 ``LIMIT n`` 兜底；有则保留，Python 侧再按 n 截断，
          双保险保证返回行数不超过 limit。
        - 复用连接器 ``registry.build + collector.query``（与维度枚举预览同链路）。

        Raises:
            NotFoundError: 数据源不存在。
            ValidationError: SQL 非只读 SELECT / 表名列名不合法。
            ExternalDependencyError: 源库连接/查询失败。
        """
        from app.services.collector.connectors import registry

        self._validate_read_sql(sql)
        src = await self.get_source_orm(source_id)
        collector = registry.build(src.source_type, src.connection_config)
        try:
            # 顶层无 LIMIT 才追加（避免子查询 LIMIT 被误判为顶层）
            parsed = sqlglot.parse_one(sql)
            exec_sql = (
                f"{sql.rstrip().rstrip(';')} LIMIT {int(limit)}"
                if parsed.args.get("limit") is None
                else sql
            )
            start = time.perf_counter()
            rows = await collector.query(exec_sql)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
        except ExternalDependencyError:
            raise  # 外部依赖错误（连接/查询超时）已语义化，交由 API 层映射
        except Exception as exc:
            raise UnisenseError(f"执行 SQL 查询失败: {exc}") from exc
        finally:
            await collector.dispose()

        rows = rows[:limit]
        columns = list(rows[0].keys()) if rows else []
        return {
            "columns": columns,
            "rows": rows,
            "total": len(rows),
            "truncated": len(rows) >= limit,
            "elapsed_ms": elapsed_ms,
        }

    async def register_catalog(
        self, req: DBCatalogCreateRequest, actor_id: int
    ) -> DBCatalogResponse:
        # 可配置敏感规则（system_dict pii_rule）惰性加载
        await self._maybe_load_db_rules()
        # source_id 是下游唯一键，缺失时服务层防御性拒绝
        # （API 层会按路径回填，但 worker/任务路径直接调用服务时需自行保证）
        if req.source_id is None:
            raise ValidationError("source_id 缺失：必须由路径参数或请求体提供")
        # 规则引擎分类（确定性基线）：先做字段级命中明细，再判级（避免重复检测）
        pii_hits = self._classifier.detect_pii_fields(req.entity_name, req.schema_def)
        rule_sensitivity = self._classifier.classify(
            req.entity_name, req.schema_def, hits=pii_hits
        )
        sensitivity = rule_sensitivity

        # US6: 空 schema 告警
        if not req.schema_def.get("columns"):
            logger.warning(
                "register_catalog_schema_incomplete: source=%s entity=%s",
                req.source_id,
                req.entity_name,
            )

        # P0-2: 使用 LLM 结构化输出分流——
        #   高置信度（>=0.7）采用 LLM 判定的 content；
        #   低置信度标记 NEEDS_REVIEW（大写，与 DB ENUM 一致）供人工复核。
        llm_sensitivity = await self._llm_classify_sensitivity(req.entity_name, req.schema_def)
        if llm_sensitivity is not None:
            confidence = float(llm_sensitivity.get("confidence", 1.0) or 0.0)
            content = str(llm_sensitivity.get("content", "")).strip().upper()
            if confidence >= 0.7 and content in ("PII", "CONFIDENTIAL", "INTERNAL", "PUBLIC"):
                if content != sensitivity:
                    logger.info(
                        "catalog_llm_override",
                        entity_name=req.entity_name,
                        confidence=confidence,
                        rule_sensitivity=rule_sensitivity,
                        llm_sensitivity=content,
                    )
                sensitivity = content
            elif confidence < 0.7:
                sensitivity = "NEEDS_REVIEW"
                logger.info(
                    "catalog_llm_low_confidence",
                    entity_name=req.entity_name,
                    confidence=confidence,
                    original_sensitivity=rule_sensitivity,
                )
                # 三梯队通知：定向通知合规官「PII 复核待办」（best-effort，独立 session）
                await self._notify_pii_review_pending(req.source_id, req.entity_name)

        cat, _created, drift_info = await self._repo.upsert_catalog(
            source_id=req.source_id,
            entity_name=req.entity_name,
            entity_type=req.entity_type,
            schema_json=req.schema_def,
            etl_sql=req.etl_sql,
            sensitivity_level=sensitivity,
            owner_id=req.owner_id,
        )
        # PII 合规增强：随分级把字段级命中明细落 classification（含类别/规则/置信度）
        if pii_hits:
            await self._repo.upsert_classification(
                cat.id, sensitivity, [_hit_to_dict(h) for h in pii_hits]
            )
        await self._repo.recompute_coverage(req.source_id)
        await self._events.publish(
            "catalog_registered",
            {
                "source_id": req.source_id,
                "entity_name": req.entity_name,
                "sensitivity": sensitivity,
            },
        )
        # 通知闭环：双发 EventBus（TD §5.5 订阅式扇出），Redis 裸通道保留不动
        await self._eventbus.publish(
            "catalog_registered",
            {
                "source_id": req.source_id,
                "entity_name": req.entity_name,
                "sensitivity": sensitivity,
            },
        )

        # US2: 检测到 Schema Drift 时发布事件 + 记录变更历史
        if drift_info is not None:
            await self._handle_drift(req.source_id, req.entity_name, drift_info)

        return DBCatalogResponse.model_validate(cat)

    @staticmethod
    def _parse_llm_description_result(raw: str) -> dict[str, Any] | None:
        """解析 LLM 字段/表描述返回，委托统一解析器（fence 剥离/字段别名/类型强转）。"""
        from app.services.llm.parse import parse_description_result

        description, confidence = parse_description_result(raw)
        if description is None or confidence is None:
            return None
        return {"description": description, "confidence": confidence}

    @staticmethod
    async def _infer_description_structured(
        client: Any, messages: list[dict[str, Any]], max_tokens: int
    ) -> dict[str, Any] | None:
        """调用 LLM 推断描述：json_schema 强约束优先，失败降级 json_object，解析失败重试 1 次。

        两层保证：请求层（json_schema strict 迫使模型输出正确结构）+ 解析层（统一解析器
        容错 fence/别名/越界）。任一层解析为 None 时追加约束提示重发，最多 2 次尝试。
        """
        from app.services.llm.parse import parse_description_result

        for attempt in (0, 1):
            aug = messages
            if attempt:
                aug = [*messages, {"role": "user", "content": _STRICT_JSON_HINT}]
            for fmt in (_DESCRIPTION_RESPONSE_FORMAT, _JSON_OBJECT_FORMAT):
                try:
                    result = await client.chat(
                        aug, temperature=0.0, max_tokens=max_tokens, response_format=fmt
                    )
                except LlmError:
                    continue
                description, confidence = parse_description_result(result.get("content", ""))
                if description is not None and confidence is not None:
                    return {"description": description, "confidence": confidence}
        logger.warning("llm_infer_desc_all_formats_failed: 强约束与降级均无法解析")
        return None

    async def _build_llm_client(
        self,
    ) -> LlmClient | LlmRouterClient | DeterministicFallbackLlmClient:
        """构建 LLM 客户端：优先 DB 配置（env 兜底参与路由），DB 读取失败回退 env 静态客户端。

        与 ai/metrics 消费方一致，避免描述/敏感度推断走已失效的 env 静态客户端
        （如 kilo.ai 模型下线 → 404 → LLM_INFER_UNAVAILABLE）。
        """
        try:
            from app.services.llm.config_service import LlmConfigService

            return await LlmConfigService(self._db).build_client()
        except Exception:  # noqa: BLE001 - DB 配置读取异常降级 env 静态客户端，不阻断推断
            logger.warning("llm_db_config_load_failed, fallback to env client", exc_info=True)
            from app.services.llm.client import build_llm_client

            return build_llm_client()

    async def _llm_classify_sensitivity(
        self, entity_name: str, schema_def: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """使用 LLM 辅助分类敏感级别，返回结构化结果。

        LLM 不可用时返回 None（不阻断主流程）。
        """
        client = None
        try:
            client = await self._build_llm_client()
            if not client.enabled:
                return None

            # 构建分类 prompt
            schema_str = str(schema_def)[:2000] if schema_def else "无 schema 信息"
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是数据敏感分类专家。根据表名和 schema 判断敏感级别。\n"
                        "返回 JSON 格式：{\n"
                        '  "content": "PII|CONFIDENTIAL|INTERNAL|PUBLIC",\n'
                        '  "confidence": 0.0-1.0,\n'
                        '  "reasoning": "判断依据",\n'
                        '  "candidates": [{"level": "...", "score": 0.0}]\n'
                        "}\n"
                        "confidence < 0.7 表示不确定，需要人工审核。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"表名: {entity_name}\nSchema: {schema_str}",
                },
            ]

            result = await client.chat(
                messages,
                temperature=0.0,
                max_tokens=500,
                response_format={"type": "json_object"},
            )
            # P0-2 回归防护：降级客户端返回 content="" + confidence=0，
            # 视为 LLM 不可用（不参与分流），否则所有实体都会被误标 NEEDS_REVIEW。
            if not result.get("content") or float(result.get("confidence", 0) or 0) <= 0:
                return None
            return result
        except (TimeoutError, ConnectionError, OSError) as exc:
            # FR-023: 具体异常类型替代 BLE001 + metric 计数
            logger.warning("llm_classify_timeout_error: %s", exc)
            _record_llm_error_metric("timeout")
            return None
        except (ValueError, KeyError, TypeError) as exc:
            # LLM 返回格式异常
            logger.warning("llm_classify_format_error: %s", exc)
            _record_llm_error_metric("format_error")
            return None
        except RuntimeError as exc:
            # LLM 客户端初始化失败等
            logger.warning("llm_classify_runtime_error: %s", exc)
            _record_llm_error_metric("runtime_error")
            return None
        except LlmError as exc:
            # LLM 网关/模型错误（如模型不存在 404）——分类是辅助能力，降级不阻断登记
            logger.warning("llm_classify_llm_error: %s", exc)
            _record_llm_error_metric("llm_error")
            return None
        finally:
            # 异常/早退路径也必须释放 httpx.AsyncClient，防连接泄漏
            if client is not None:
                await client.close()

    async def _llm_infer_column_description(
        self,
        entity_name: str,
        column_name: str,
        column_type: str | None = None,
    ) -> dict[str, Any] | None:
        """使用 LLM 推断字段描述，返回结构化结果。

        复用 _build_llm_client（DB 配置优先 + 熔断器模式，与 _llm_classify_sensitivity 一致）。
        LLM 不可用时返回 None（不阻断主流程）。

        Args:
            entity_name: 表名（库.表格式）。
            column_name: 字段名。
            column_type: 字段类型（可选，供推断上下文）。

        Returns:
            推断结果 dict 含 description/confidence，或 None 表示推断失败。
        """
        client = None
        try:
            client = await self._build_llm_client()
            if not client.enabled:
                return None

            type_info = f"，类型为 {column_type}" if column_type else ""
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是数据治理领域的字段描述专家。根据表名和字段名推断字段的中文描述。\n"
                        "返回 JSON 格式：{\n"
                        '  "description": "字段的中文描述",\n'
                        '  "confidence": 0.0-1.0\n'
                        "}\n"
                        "要求：\n"
                        "1. 描述简洁精准，10-50字\n"
                        "2. 基于字段名和表名的语义推断\n"
                        "3. confidence < 0.5 表示不确定"
                    ),
                },
                {
                    "role": "user",
                    "content": f"表名: {entity_name}\n字段名: {column_name}{type_info}",
                },
            ]

            result = await self._infer_description_structured(client, messages, max_tokens=200)
            return result
        except (TimeoutError, ConnectionError, OSError) as exc:
            logger.warning("llm_infer_desc_timeout_error: %s", exc)
            _record_llm_error_metric("timeout")
            return None
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("llm_infer_desc_format_error: %s", exc)
            _record_llm_error_metric("format_error")
            return None
        except RuntimeError as exc:
            logger.warning("llm_infer_desc_runtime_error: %s", exc)
            _record_llm_error_metric("runtime_error")
            return None
        except LlmError as exc:
            logger.warning("llm_infer_desc_llm_error: %s", exc)
            _record_llm_error_metric("llm_error")
            return None
        finally:
            if client is not None:
                await client.close()

    async def _llm_infer_batch_descriptions(
        self,
        entity_name: str,
        targets: list[tuple[str, str | None]],
    ) -> dict[str, tuple[str, float]]:
        """一次 LLM 调用推断多个字段描述（批量，FR-023 顺序性 + 格式保证）。

        将整表缺失字段清单一次发送，要求返回 ``{"descriptions": [...]}`` 数组；
        解析后按 ``column_name`` 回填（不依赖返回顺序）。json_schema 数组强约束优先，
        网关不支持时降级 json_object，解析失败重试 1 次（模式与
        ``_infer_description_structured`` 一致）。LLM 不可用返回空 dict（不阻断主流程）。

        Returns:
            ``{column_name: (description, confidence)}``；失败时为空 dict。
        """
        client = None
        try:
            client = await self._build_llm_client()
            if not client.enabled or not targets:
                return {}

            field_lines = "\n".join(
                f"- {name} ({ctype or 'unknown'})" for name, ctype in targets
            )
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是数据治理领域的字段描述专家。根据表名和字段清单，"
                        "逐字段推断每个字段的中文描述。\n"
                        "返回 JSON 格式：{\n"
                        '  "descriptions": [\n'
                        '    {"column_name": "字段名", "description": "中文描述", '
                        '"confidence": 0.0-1.0}\n'
                        "  ]\n"
                        "}\n"
                        "要求：\n"
                        "1. 必须为清单中每个字段各返回一个元素，column_name 与清单完全一致\n"
                        "2. 描述简洁精准，10-50字\n"
                        "3. confidence < 0.5 表示不确定"
                    ),
                },
                {
                    "role": "user",
                    "content": f"表名: {entity_name}\n字段清单:\n{field_lines}",
                },
            ]

            from app.services.llm.parse import parse_batch_description_result

            expected = [name for name, _ctype in targets]
            for attempt in (0, 1):
                aug = messages
                if attempt:
                    aug = [*messages, {"role": "user", "content": _STRICT_JSON_HINT}]
                for fmt in (_BATCH_DESCRIPTION_RESPONSE_FORMAT, _JSON_OBJECT_FORMAT):
                    try:
                        result = await client.chat(
                            aug, temperature=0.0, max_tokens=1500, response_format=fmt
                        )
                    except LlmError:
                        continue
                    parsed = parse_batch_description_result(result.get("content", ""), expected)
                    if parsed:
                        return parsed
            logger.warning("llm_infer_desc_batch_all_formats_failed: 强约束与降级均无法解析")
            return {}
        except (TimeoutError, ConnectionError, OSError) as exc:
            logger.warning("llm_infer_batch_timeout_error: %s", exc)
            _record_llm_error_metric("timeout")
            return {}
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("llm_infer_batch_format_error: %s", exc)
            _record_llm_error_metric("format_error")
            return {}
        except RuntimeError as exc:
            logger.warning("llm_infer_batch_runtime_error: %s", exc)
            _record_llm_error_metric("runtime_error")
            return {}
        except LlmError as exc:
            logger.warning("llm_infer_batch_llm_error: %s", exc)
            _record_llm_error_metric("llm_error")
            return {}
        finally:
            if client is not None:
                await client.close()

    async def _llm_infer_table_description(
        self,
        entity_name: str,
        columns: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """使用 LLM 推断表级业务描述（表名 + 字段清单上下文）。

        复用 _build_llm_client（DB 配置优先 + 熔断器模式，与字段推断一致）；
        LLM 不可用返回 None（不阻断主流程）。
        """
        client = None
        try:
            client = await self._build_llm_client()
            if not client.enabled:
                return None

            field_lines = [
                f"- {c.get('name') or c.get('column')} ({c.get('type') or c.get('data_type')})"
                for c in columns[:30]  # 限制字段数防超长
            ]
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是数据治理领域的表结构描述专家。根据表名和字段清单，"
                        "推断该表的中文业务描述。\n"
                        "返回 JSON 格式：{\n"
                        '  "description": "表的中文业务描述",\n'
                        '  "confidence": 0.0-1.0\n'
                        "}\n"
                        "要求：\n"
                        "1. 描述简洁准确，20-80字\n"
                        "2. 概括表的业务用途，基于表名和字段语义\n"
                        "3. confidence < 0.5 表示不确定"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"表名: {entity_name}\n字段清单:\n" + "\n".join(field_lines)
                    ),
                },
            ]

            result = await self._infer_description_structured(client, messages, max_tokens=300)
            return result
        except (TimeoutError, ConnectionError, OSError) as exc:
            logger.warning("llm_infer_table_desc_timeout_error: %s", exc)
            _record_llm_error_metric("timeout")
            return None
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("llm_infer_table_desc_format_error: %s", exc)
            _record_llm_error_metric("format_error")
            return None
        except RuntimeError as exc:
            logger.warning("llm_infer_table_desc_runtime_error: %s", exc)
            _record_llm_error_metric("runtime_error")
            return None
        except LlmError as exc:
            logger.warning("llm_infer_table_desc_llm_error: %s", exc)
            _record_llm_error_metric("llm_error")
            return None
        finally:
            if client is not None:
                await client.close()

    async def _handle_drift(
        self, source_id: str, entity_name: str, drift_info: dict[str, Any]
    ) -> None:
        """处理 Schema Drift：发布事件 + 记录变更历史（US2）。"""
        from datetime import UTC, datetime

        # 发布 catalog_schema_drifted 事件
        await self._events.publish(
            "catalog_schema_drifted",
            {
                "source_id": source_id,
                "entity_name": entity_name,
                "change_type": drift_info["change_type"],
                "diff_json": drift_info.get("diff_json", {}),
            },
        )
        # 通知闭环：双发 EventBus（TD §5.5 订阅式扇出），Redis 裸通道保留不动
        await self._eventbus.publish(
            "catalog_schema_drifted",
            {
                "source_id": source_id,
                "entity_name": entity_name,
                "change_type": drift_info["change_type"],
            },
        )
        # 血缘影响通知：schema 结构变更 → 沿下游血缘定向通知受影响指标 Owner
        # （P1-4 闭环：notify_lineage_impacted_owners 由采集侧 schema drift 触发，
        # 兑现 docstring 声称的「collector 的 schema drift 处理」触发点）。
        # best-effort：通知失败/血缘查询失败不阻断采集主流程。
        try:
            from app.services.semantic.service import MetricService

            await MetricService(self._db).notify_lineage_impacted_owners(f"table:{entity_name}")
        except Exception:  # noqa: BLE001 - 血缘影响通知 best-effort
            logger.warning(
                "lineage_impact_notify_failed",
                source_id=source_id,
                entity_name=entity_name,
                exc_info=True,
            )
        # 记录变更历史到 SchemaDriftLog
        drift_log = SchemaDriftLog(
            source_id=source_id,
            entity_name=entity_name,
            change_type=drift_info["change_type"],
            before_signature=drift_info.get("before_signature"),
            after_signature=drift_info["after_signature"],
            before_schema=drift_info.get("before_schema"),
            after_schema=drift_info.get("after_schema"),
            diff_json=drift_info.get("diff_json"),
            detected_at=datetime.now(UTC),
        )
        await self._repo.save_drift_log(drift_log)

    async def _notify_catalog_deprecated_owner(
        self, source_id: str, entity_name: str
    ) -> None:
        """目录废弃后定向通知目录 Owner（best-effort，不阻断批量废弃主流程）。

        与 conflict.py `_notify_loser_owner` 对称：IN_APP 定向通知，不依赖订阅偏好；
        owner_id 缺失（孤儿资产）跳过；通知失败仅告警。
        """
        try:
            from sqlalchemy import select

            from app.models.data_source import DBCatalog
            from app.services.notify.service import NotifyService

            row = (
                await self._db.execute(
                    select(DBCatalog).where(
                        DBCatalog.source_id == source_id,
                        DBCatalog.entity_name == entity_name,
                    )
                )
            ).scalar_one_or_none()
            owner_id = getattr(row, "owner_id", None) if row is not None else None
            if not owner_id:
                logger.info(
                    "catalog_deprecated_owner_missing: source=%s entity=%s",
                    source_id,
                    entity_name,
                )
                return
            await NotifyService(self._db).notify_user(
                user_id=int(owner_id),
                event_type="catalog.deprecated",
                title="目录已废弃",
                body=f"目录 {entity_name} 已被废弃",
                payload={"source_id": source_id, "entity_name": entity_name},
            )
            logger.info(
                "catalog_deprecated_owner_notified: source=%s entity=%s owner_id=%s",
                source_id,
                entity_name,
                owner_id,
            )
        except Exception:  # noqa: BLE001 - 通知降级，不阻断废弃主流程
            logger.warning(
                "catalog_deprecated_owner_notify_failed: source=%s entity=%s",
                source_id,
                entity_name,
                exc_info=True,
            )

    async def _notify_source_owner_degraded(
        self, source_id: str, reason: str, src: DataSource
    ) -> None:
        """增量降级为全量后定向通知数据源 Owner（best-effort，不阻断采集主流程）。

        数据源 owner_id 缺失时跳过；通知失败仅告警。
        """
        try:
            from app.services.notify.service import NotifyService

            owner_id = getattr(src, "owner_id", None)
            if not owner_id:
                logger.info(
                    "collect_degraded_owner_missing: source=%s reason=%s",
                    source_id,
                    reason,
                )
                return
            await NotifyService(self._db).notify_user(
                user_id=int(owner_id),
                event_type="collect.degraded",
                title="采集已降级为全量",
                body=f"数据源 {source_id} 增量采集不可用，已降级为全量采集（原因：{reason}）",
                payload={"source_id": source_id, "reason": reason},
            )
            logger.info(
                "collect_degraded_owner_notified: source=%s owner_id=%s reason=%s",
                source_id,
                owner_id,
                reason,
            )
        except Exception:  # noqa: BLE001 - 通知降级，不阻断采集主流程
            logger.warning(
                "collect_degraded_owner_notify_failed: source=%s reason=%s",
                source_id,
                reason,
                exc_info=True,
            )

    async def _notify_source_owner_failure(
        self,
        event_type: str,
        title: str,
        source_id: str,
        reason: str,
        src: DataSource | None = None,
    ) -> None:
        """采集任务失败/数据源连接失败定向通知源 Owner（TD §5.5）。

        ``src`` 可直接提供 owner_id（探活循环内）；不可用（如异常早于对象构建）
        时回退按 ``source_id`` 查库。独立 session 通知，不干扰采集事务；
        best-effort 失败仅告警，绝不阻断采集主流程。
        """
        try:
            from sqlalchemy import select

            from app.db.mysql import async_session_factory
            from app.models.data_source import DataSource as DSTable
            from app.services.notify.service import NotifyService

            owner_id = getattr(src, "owner_id", None) if src is not None else None
            if not owner_id:
                async with async_session_factory() as session:
                    row = (
                        await session.execute(
                            select(DSTable).where(DSTable.source_id == source_id)
                        )
                    ).scalars().first()
                    owner_id = getattr(row, "owner_id", None)
            if not owner_id:
                logger.info(
                    "source_owner_failure_owner_missing: source=%s event=%s",
                    source_id,
                    event_type,
                )
                return
            async with async_session_factory() as session:
                await NotifyService(session).notify_user(
                    user_id=int(owner_id),
                    event_type=event_type,
                    title=title,
                    body=f"数据源 {source_id} 异常（原因：{reason}）",
                    payload={"source_id": source_id, "reason": reason},
                )
            logger.info(
                "source_owner_failure_notified: source=%s event=%s owner=%s",
                source_id,
                event_type,
                owner_id,
            )
        except Exception:  # noqa: BLE001 - 通知降级，不阻断采集主流程
            logger.warning(
                "source_owner_failure_notify_failed: source=%s event=%s",
                source_id,
                event_type,
                exc_info=True,
            )

    async def _notify_pii_review_pending(
        self, source_id: str, entity_name: str
    ) -> None:
        """PII 低置信度标记 NEEDS_REVIEW 后定向通知合规官（TD §5.5）。

        查 active compliance_officer 定向送达（IN_APP，不依赖订阅偏好）。
        独立 session 通知，不干扰采集事务；best-effort 失败仅告警。
        """
        try:
            from sqlalchemy import or_, select

            from app.db.mysql import async_session_factory
            from app.models.user import User, UserRole
            from app.services.notify.service import NotifyService

            async with async_session_factory() as session:
                stmt = select(User.id).where(
                    User.status == "active",
                    or_(
                        User.role == "compliance_officer",
                        User.role_items.any(UserRole.role == "compliance_officer"),
                    ),
                )
                result = await session.execute(stmt)
                targets = [r[0] for r in result.all()]
            for uid in targets:
                async with async_session_factory() as session:
                    await NotifyService(session).notify_user(
                        user_id=int(uid),
                        event_type="pii.review_pending",
                        title="PII 复核待办",
                        body=(
                            f"数据实体 {entity_name}（源 {source_id}）敏感级别被"
                            "低置信度判定，需人工复核"
                        ),
                        payload={"source_id": source_id, "entity_name": entity_name},
                    )
            logger.info(
                "pii_review_pending_notified: source=%s entity=%s targets=%d",
                source_id,
                entity_name,
                len(targets),
            )
        except Exception:  # noqa: BLE001 - 通知降级，不阻断采集主流程
            logger.warning(
                "pii_review_pending_notify_failed: source=%s entity=%s",
                source_id,
                entity_name,
                exc_info=True,
            )

    @staticmethod
    def _merge_descriptions_to_schema(
        resp: DBCatalogResponse, descriptions: Sequence[ColumnDescription]
    ) -> None:
        """将 column_descriptions 合并到 resp.schema_def.columns[] 的 description 字段。

        字段描述存 column_descriptions 表（不回写 schema_json.comment），采集目录
        字段详情（SchemaTable）靠该合并展示 LLM 推断/人工编辑的描述，与
        assetmap ``_merge_descriptions`` 语义一致（manual > llm > schema comment）。
        """
        desc_map = {d.column_name: d for d in descriptions}
        schema = resp.schema_def if isinstance(resp.schema_def, dict) else {}
        columns = schema.get("columns") or schema.get("fields") or []
        if not isinstance(columns, list):
            return
        for col in columns:
            if not isinstance(col, dict):
                continue
            name = col.get("name") or col.get("column")
            if not name:
                continue
            d = desc_map.get(str(name))
            if d is not None:
                col["description"] = d.description
                col["description_source"] = d.source

    async def list_catalogs(self, params: DBCatalogListParams) -> DBCatalogListResponse:
        cats, total = await self._repo.list_catalogs(params)
        # 批量补源维度信息（名称 / 删除状态）：join 路径已带瞬态属性，普通路径批量查询
        source_ids = {c.source_id for c in cats}
        meta = await self._repo.get_sources_meta(list(source_ids)) if source_ids else {}
        # 生产化补充：业务域（经数据源继承）+ 责任人展示名
        domains = (
            await self._repo.get_sources_domain(list(source_ids)) if source_ids else {}
        )
        owner_ids = {c.owner_id for c in cats if c.owner_id is not None}
        owner_names = (
            await self._repo.get_owner_names(list(owner_ids)) if owner_ids else {}
        )
        # 批量合并字段描述（column_descriptions），供字段详情抽屉展示
        desc_map = await self._repo.get_descriptions_for_catalogs([c.id for c in cats])
        items: list[DBCatalogResponse] = []
        for c in cats:
            resp = DBCatalogResponse.model_validate(c)
            self._merge_descriptions_to_schema(resp, desc_map.get(c.id, []))
            src_deleted = getattr(c, "_src_deleted", None)
            if src_deleted is not None:
                resp.source_deleted = bool(src_deleted)
                resp.source_name = getattr(c, "_src_name", None) or c.source_id
                resp.domain = getattr(c, "_src_domain", None) or domains.get(c.source_id)
            else:
                name, deleted = meta.get(c.source_id, (None, True))
                resp.source_deleted = deleted
                resp.source_name = name or c.source_id
                resp.domain = domains.get(c.source_id)
            resp.owner_name = owner_names.get(c.owner_id) if c.owner_id is not None else None
            items.append(resp)
        return DBCatalogListResponse(
            items=items,
            total=total,
            page=params.page,
            page_size=params.page_size,
        )

    async def list_catalog_databases(
        self, source_id: str | None = None, source_status: str | None = None
    ) -> list[str]:
        """目录去重库名列表（供前端库名筛选下拉，可随 source_id / source_status 联动）。"""
        return await self._repo.list_catalog_databases(source_id, source_status)

    async def get_catalog_detail(self, catalog_id: int) -> DBCatalogResponse:
        """按主键取目录实体详情（血缘图谱表节点下钻用）。

        Raises:
            NotFoundError: 目录实体不存在（含已删除）。
        """
        cat = await self._repo.get_catalog_by_id(catalog_id)
        if cat is None:
            raise NotFoundError(f"目录实体不存在: {catalog_id}")
        resp = DBCatalogResponse.model_validate(cat)
        # 合并字段描述（column_descriptions），字段详情抽屉展示 LLM 推断/人工编辑描述
        self._merge_descriptions_to_schema(
            resp, await self._repo.get_descriptions(catalog_id)
        )
        name, deleted = (await self._repo.get_sources_meta([cat.source_id])).get(
            cat.source_id, (None, True)
        )
        resp.source_deleted = deleted
        resp.source_name = name or cat.source_id
        return resp

    async def bulk_deprecate(self, req: BulkDeprecateRequest, actor_id: int) -> BulkDeprecateResult:
        succeeded, failed = await self._repo.bulk_deprecate(req.items)
        for it in succeeded:
            await self._events.publish(
                "catalog_deprecated",
                {"source_id": it.source_id, "entity_name": it.entity_name},
            )
            # 通知闭环：双发 EventBus（TD §5.5 订阅式扇出），Redis 裸通道保留不动
            await self._eventbus.publish(
                "catalog.deprecated",
                {"source_id": it.source_id, "entity_name": it.entity_name},
            )
            # 定向通知目录 Owner（best-effort；notify_user 内部会 commit，废弃已 flush，
            # 提前提交语义等价于 API 层 commit，不影响结果）
            await self._notify_catalog_deprecated_owner(it.source_id, it.entity_name)
        return BulkDeprecateResult(succeeded=succeeded, failed=failed)

    @staticmethod
    async def _emit_progress(
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None, event: dict[str, Any]
    ) -> None:
        """触发采集进度回调（回调失败仅告警，不阻断采集主流程）。"""
        if progress_cb is None:
            return
        try:
            await progress_cb(event)
        except Exception as exc:  # noqa: BLE001 - 进度推送是辅助能力
            logger.warning("collect_progress_cb_failed: %s", exc)

    async def collect_and_register(
        self,
        source_id: str,
        collector: BaseCollector,
        actor_id: int,
        *,
        mode: str = "FULL",
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> dict[str, Any]:
        # 可配置敏感规则（system_dict pii_rule）惰性加载
        await self._maybe_load_db_rules()
        src = await self._repo.get_source(source_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        if not getattr(src, "enabled", True):
            raise BusinessError(
                f"数据源已停用: {source_id}，请先在数据源管理启用后再采集",
                error_code="SOURCE_DISABLED",
            )

        # US3: 增量采集逻辑 —— 读取水位，不支持时降级为全量
        effective_mode = mode
        watermark_ts: datetime | None = None
        degrade_reason: str | None = None
        if mode == "INCREMENTAL":
            from app.services.collector.incremental import should_degrade_to_full, should_mix_in

            watermark = await self._repo.get_watermark(source_id)
            if watermark is not None:
                watermark_ts = watermark.last_collected_at
            if should_degrade_to_full(src.source_type, watermark_ts):
                logger.info(
                    "collect_incremental_degrade_to_full: source=%s type=%s watermark=%s",
                    source_id,
                    src.source_type,
                    watermark_ts,
                )
                effective_mode = "FULL"
                degrade_reason = (
                    "watermark_missing"
                    if watermark_ts is None
                    else f"source_type_not_supported:{src.source_type}"
                )
            else:
                # P0-3: MySQL InnoDB UPDATE_TIME 通常为 NULL，按占比阈值降级全量
                if (
                    src.source_type == "mysql"
                    and getattr(collector, "_connector", None) is not None
                    and hasattr(getattr(collector, "_connector", None), "query")
                    and await should_mix_in(
                        src.source_type,
                        getattr(collector, "_connector", None),
                        self._settings.collector_mysql_incremental_ratio_threshold,
                    )
                ):
                    logger.info(
                        "collect_mysql_update_time_sparse: source=%s, 降级为全量",
                        source_id,
                    )
                    effective_mode = "FULL"
                    degrade_reason = "mysql_update_time_sparse"
                if effective_mode == "INCREMENTAL":
                    logger.info(
                        "collect_incremental: source=%s watermark=%s",
                        source_id,
                        watermark_ts,
                    )

        # 可观测性：增量请求被降级为全量时发布事件（供通知/审计/运维追踪），
        # 避免「增量静默失效」——用户以为在走增量，实际每次全量扫描。
        if degrade_reason is not None:
            await self._events.publish(
                "collect_degraded",
                {
                    "source_id": source_id,
                    "reason": degrade_reason,
                    "source_type": src.source_type,
                    "watermark": watermark_ts.isoformat() if watermark_ts else None,
                },
            )
            # 通知闭环：双发 EventBus（TD §5.5 订阅式扇出），Redis 裸通道保留不动
            await self._eventbus.publish(
                "collect.degraded",
                {
                    "source_id": source_id,
                    "reason": degrade_reason,
                    "source_type": src.source_type,
                    "watermark": watermark_ts.isoformat() if watermark_ts else None,
                },
            )
            # 定向通知数据源 Owner（best-effort；此刻主事务尚无待提交变更）
            await self._notify_source_owner_degraded(source_id, degrade_reason, src)

        # US5: 采集成功后更新健康状态
        await self._emit_progress(
            progress_cb,
            {"phase": "start", "message": f"开始采集 {source_id}（{effective_mode} 模式）"},
        )
        try:
            # P0-6: 注入增量上下文——增量模式且水位有效时连接器只采变更实体
            collector.set_incremental_context(effective_mode, watermark_ts)
            # 治理：表级 include/exclude 过滤（数据源配置为基线；本次临时过滤覆盖时
            # 仅本次生效，不污染数据源配置——collect-now 弹窗的临时白/黑名单）
            effective_include = (
                src.include_patterns if include_patterns is None else include_patterns
            )
            effective_exclude = (
                src.exclude_patterns if exclude_patterns is None else exclude_patterns
            )
            if effective_include or effective_exclude:
                setter = getattr(collector, "set_table_filter", None)
                if setter is not None:
                    setter(effective_include, effective_exclude)
            # 多目标库：数据源配置了目标库列表时注入连接器（逐库扫描）
            if getattr(src, "databases", None):
                db_setter = getattr(collector, "set_databases", None)
                if db_setter is not None:
                    db_setter(src.databases)
            # PII 精度增强：样本采样配置（quota.sample_rows，0/缺省=不采样）。
            # 采样在连接器内部复用源库连接执行（见各连接器 sample_columns）。
            sampling_setter = getattr(collector, "set_sampling", None)
            if sampling_setter is not None:
                quota_cfg = src.quota or {}
                sample_rows = (
                    int(quota_cfg.get("sample_rows") or 0)
                    if isinstance(quota_cfg, dict)
                    else 0
                )
                sampling_setter(sample_rows)
            # 采样/扫描阶段进度：把进度回调注入连接器，使其在 collect() 内
            # 逐表采样时发 phase=sampling 进度（否则该阶段只发 phase=start，
            # 前端会一直停在 0%——Doris 数百表采样可长达数分钟）。
            progress_setter = getattr(collector, "set_progress_cb", None)
            if progress_setter is not None:
                progress_setter(progress_cb)
            result: CollectResult = await collector.collect(src)
        except Exception as exc:
            # P0-4: 健康状态更新必须落库——即使采集失败也要记录 unhealthy，
            # 否则 API/worker 上抛后被 get_db_session 回滚，健康状态永不更新。
            # 采集异常可能已让 session 进入 PendingRollback（flush/commit 失败），
            # 必须先 rollback 释放会话——否则 update_health_status 的 flush 会抛
            # PendingRollbackError，掩盖原始异常且健康状态无法落库。
            await self._db.rollback()
            await self._repo.update_health_status(source_id, "unhealthy", error=str(exc))
            await self._db.commit()
            # 三梯队通知：采集任务失败定向通知源 Owner（best-effort，独立 session）
            await self._notify_source_owner_failure(
                "collect.failed",
                "采集任务失败",
                source_id,
                reason=str(exc)[:500],
                src=locals().get("src"),
            )
            raise

        await self._emit_progress(
            progress_cb,
            {
                "phase": "scanning",
                "scanned": len(result.specs),
                "message": f"源端扫描完成，发现 {len(result.specs)} 个实体",
            },
        )

        # P1-4: 资源配额——max_scan_rows 按表数截断注册清单。
        # 源端实体数超过配额时仅注册前 N 个（配额=0/未配置表示不限制），
        # 防止超大表清单一次性拖垮注册/内存（服务可用性保护）。
        # 截断丢弃的表名必须记录：FULL 对账据此排除，否则被误判「源表已 DROP」
        # 触发批量废弃目录 + 下游指标置 DSD（数据事故，HIGH-1 回归防护）。
        quota_truncated = 0
        quota_truncated_names: list[str] = []
        quota = src.quota or {}
        max_scan_rows = (
            int(quota.get("max_scan_rows") or 0) if isinstance(quota, dict) else 0
        )
        if max_scan_rows > 0 and len(result.specs) > max_scan_rows:
            quota_truncated = len(result.specs) - max_scan_rows
            quota_truncated_names = [s.entity_name for s in result.specs[max_scan_rows:]]
            logger.warning(
                "collect_quota_truncated: source=%s scanned=%s limit=%s truncated=%s",
                source_id,
                len(result.specs),
                max_scan_rows,
                quota_truncated,
            )
            result.specs = result.specs[:max_scan_rows]

        registered = 0
        pii_registered = 0
        batch_payloads: list[dict[str, Any]] = []
        drift_events: list[dict[str, Any]] = []
        content_fingerprints: dict[str, str] = {}
        catalog_failed_specs: list[dict[str, str]] = []
        entities: list[dict[str, Any]] = []
        for spec in result.specs:
            pii_hits = self._classifier.detect_pii_fields(spec.entity_name, spec.schema_json)
            sensitivity = self._classifier.classify(
                spec.entity_name, spec.schema_json, hits=pii_hits
            )
            if sensitivity == "PII":
                pii_registered += 1

            # US6: 空 schema 告警 + schema_incomplete 标记
            schema_incomplete = not spec.schema_json.get("columns")
            if schema_incomplete:
                logger.warning(
                    "collect_schema_incomplete: source=%s entity=%s",
                    source_id,
                    spec.entity_name,
                )

            # P0-4: 每个 spec 单独 try/except，单表失败不影响整批
            try:
                _cat, _created, drift_info = await self._repo.upsert_catalog(
                    source_id=source_id,
                    entity_name=spec.entity_name,
                    entity_type=spec.entity_type,
                    schema_json=spec.schema_json,
                    etl_sql=spec.etl_sql,
                    sensitivity_level=sensitivity,
                    owner_id=None,
                    description=getattr(spec, "description", None),
                )
            except Exception as exc:
                logger.warning(
                    "collect_catalog_upsert_failed: source=%s entity=%s error=%s",
                    source_id,
                    spec.entity_name,
                    exc,
                )
                catalog_failed_specs.append({"entity_name": spec.entity_name, "error": str(exc)})
                continue
            # PII 合规增强：随分级把字段级命中明细落 classification
            if pii_hits:
                try:
                    await self._repo.upsert_classification(
                        _cat.id, sensitivity, [_hit_to_dict(h) for h in pii_hits]
                    )
                except Exception as exc:  # noqa: BLE001 - 明细落库失败不阻断采集
                    logger.warning(
                        "collect_classification_failed: source=%s entity=%s error=%s",
                        source_id,
                        spec.entity_name,
                        exc,
                    )
            # P2-4: 回填实体级内容指纹（供增量判断与审计追溯）
            signature = _cat.content_signature
            if signature:
                content_fingerprints[spec.entity_name] = signature
            registered += 1
            batch_payloads.append(
                {
                    "source_id": source_id,
                    "entity_name": spec.entity_name,
                    "sensitivity": sensitivity,
                }
            )
            # US2: 检测到 Schema Drift 时处理
            if drift_info is not None:
                await self._handle_drift(source_id, spec.entity_name, drift_info)
                drift_events.append(
                    {
                        "entity_name": spec.entity_name,
                        "change_type": drift_info["change_type"],
                    }
                )
            # 明细：本次采集到的实体（供前端"采集结果"展示）
            entities.append(
                {
                    "entity_name": spec.entity_name,
                    "sensitivity_level": sensitivity,
                    "drifted": drift_info is not None,
                    "change_type": drift_info["change_type"] if drift_info is not None else None,
                }
            )
            await self._emit_progress(
                progress_cb,
                {
                    "phase": "registering",
                    "index": len(entities),
                    "total": len(result.specs),
                    "entity_name": spec.entity_name,
                    "sensitivity": sensitivity,
                    "message": f"注册 {len(entities)}/{len(result.specs)}：{spec.entity_name}",
                },
            )
        # FR-024: 发布1次batch事件而非逐条publish
        if batch_payloads:
            await self._events.publish_batch("catalog_registered", batch_payloads)
            # 通知闭环：双发 EventBus（TD §5.5 订阅式扇出），逐条发布以便通知中心按
            # 单事件订阅/扇出（Redis 裸通道保留 batch 不动）
            for item in batch_payloads:
                await self._eventbus.publish("catalog_registered", item)

        # P1-5: 废弃表自动对账 + DSD 闭环——仅在全量采集后执行（增量仅覆盖变更实体，
        # 不可对未采集实体误废）。对比 catalog 中仍存活的实体与本次源端扫描到的实体名，
        # 源端已 drop 的：① 沿血缘把下游 PUBLISHED 指标置 DATA_SOURCE_DROPPED（P1-4 接线，
        # 兑现 PRD R3-04④「采集检测到源表 DROP 后调用」）；② 目录实体标 DEPRECATED。
        # 顺序关键：mark_source_dropped 只查活跃表（deleted_at IS NULL），须在
        # deprecate_catalog 置 deleted_at 之前调用，否则查不到已 drop 表。
        deprecated_count = 0
        dsd_count = 0
        if effective_mode == "FULL":
            # 对账排除集：include/exclude 过滤跳过的表 + 配额截断丢弃的表——
            # 它们并非源端已 DROP，只是本次未采集。若不排除，会被误判为
            # 「源表已消失」触发批量废弃目录 + 下游指标置 DSD（HIGH-1）。
            skipped_names = set(result.filtered_names or []) | set(quota_truncated_names)
            collected_names = {spec.entity_name for spec in result.specs}
            active_names = await self._repo.list_active_entity_names(source_id)
            dropped_names = [
                name
                for name in active_names
                if name not in collected_names and name not in skipped_names
            ]
            if dropped_names:
                # 源表 DROP → 血缘下游指标置 DSD。采集侧是可信系统组件：DSD 翻转是
                # 事实检测（表确实从源端消失），非用户授权决策，故以管理角色触发；
                # 按 entity_names 精确到本次 drop 的表，避免误伤同源未 drop 表的下游。
                try:
                    from app.services.semantic.service import MetricService

                    dsd_count = await MetricService(self._db).mark_source_dropped(
                        [source_id],
                        actor_id=actor_id,
                        role="platform_admin",
                        entity_names=dropped_names,
                    )
                except Exception as exc:  # noqa: BLE001 - best-effort 不阻断采集主流程
                    logger.warning(
                        "collect_dsd_mark_failed: source=%s dropped=%d error=%s",
                        source_id,
                        len(dropped_names),
                        exc,
                    )
            for name in dropped_names:
                try:
                    if await self._repo.deprecate_catalog(source_id, name):
                        deprecated_count += 1
                except Exception as exc:
                    logger.warning(
                        "collect_deprecate_failed: source=%s entity=%s error=%s",
                        source_id,
                        name,
                        exc,
                    )

        # 覆盖率基线更新（HIGH-2）：仅 FULL 用「本次源端完整扫描数（含过滤/截断）」
        # 刷新基线；增量只采变更实体，若用变更数覆盖基线会把 coverage 压缩失真为 1.0。
        if effective_mode == "FULL":
            total_entities = (
                len(result.specs)
                + len(result.filtered_names or [])
                + len(quota_truncated_names)
            )
        else:
            total_entities = None
        coverage = await self._repo.recompute_coverage(
            source_id, total_entities=total_entities
        )

        # P0-4: 合并 collector 层 failed_specs 与 catalog 层 failed_specs
        all_failed_specs = [
            {"entity_name": f.entity_name, "error": f.error} for f in result.failed_specs
        ] + catalog_failed_specs

        # US5: 采集后健康状态机——失败率 >5% 进入 DEGRADED（黄态），否则 healthy
        attempted = len(result.specs) + len(all_failed_specs)
        health_status, metrics, degraded_since = self._evaluate_health_after_collect(
            getattr(src, "health_metrics", None), attempted, len(all_failed_specs)
        )
        await self._repo.update_health_status(
            source_id,
            health_status,
            health_metrics=metrics,
            degraded_since=degraded_since,
        )

        # US3: 更新采集水位
        await self._repo.update_watermark_after_collection(
            source_id=source_id,
            mode=effective_mode,
            scanned_count=len(result.specs),
            failed_count=len(result.failed_specs) + len(catalog_failed_specs),
            content_fingerprints=content_fingerprints or None,
        )

        return {
            "source_id": source_id,
            "scanned": len(result.specs),
            "registered": registered,
            "pii_registered": pii_registered,
            "failed_count": len(all_failed_specs),
            "failed_specs": all_failed_specs,
            "coverage": coverage,
            "mode": effective_mode,
            "drift_count": len(drift_events),
            "drift_events": drift_events,
            "deprecated_count": deprecated_count,
            "dsd_count": dsd_count,
            "entities": entities,
            # 表级过滤跳过（方案 B：采集结果/记录展示被白黑名单过滤掉的表）
            "filtered_count": getattr(result, "filtered_count", 0),
            "filtered_names": getattr(result, "filtered_names", []),
            "quota_truncated": quota_truncated,
        }

    async def refresh_entity(
        self,
        source_id: str,
        entity_name: str,
        actor_id: int,
        collector: BaseCollector,
    ) -> dict[str, Any]:
        """单实体元数据刷新（生产运维：只刷新一张表，不触发全源扫描）。

        优先走连接器的 ``collect_entity`` 精确刷新；连接器不支持单实体采集
        （如 Hive，启动开销大）时回退为全量采集后仅取目标实体。
        目标实体在源端已不存在时抛 ``NotFoundError``。

        Returns:
            dict 含 entity_name / sensitivity_level / drifted / columns 数。
        """
        src = await self._repo.get_source(source_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        if not getattr(src, "enabled", True):
            raise BusinessError(
                f"数据源已停用: {source_id}，请先在数据源管理启用后再刷新",
                error_code="SOURCE_DISABLED",
            )

        # PII 精度增强：样本采样配置注入（与全量采集路径一致，
        # quota.sample_rows，0/缺省=不采样）
        sampling_setter = getattr(collector, "set_sampling", None)
        if sampling_setter is not None:
            quota_cfg = src.quota or {}
            sample_rows = (
                int(quota_cfg.get("sample_rows") or 0)
                if isinstance(quota_cfg, dict)
                else 0
            )
            sampling_setter(sample_rows)

        # 判断连接器是否真实覆盖了 collect_entity（区分「不支持」与「表不存在」）；
        # getattr 兜底防御：不继承 BaseCollector 的自定义采集器视为不支持单实体。
        collect_entity_fn = getattr(type(collector), "collect_entity", None)
        supports_single = (
            collect_entity_fn is not None and collect_entity_fn is not BaseCollector.collect_entity
        )
        spec: CatalogSpec | None = None
        if supports_single:
            spec = await collector.collect_entity(src, entity_name)
        else:
            result = await collector.collect(src)
            for s in result.specs:
                if s.entity_name == entity_name:
                    spec = s
                    break
        if spec is None:
            raise NotFoundError(f"源端不存在实体: {entity_name}")

        await self._maybe_load_db_rules()
        sensitivity = self._classifier.classify(spec.entity_name, spec.schema_json)
        _cat, _created, drift_info = await self._repo.upsert_catalog(
            source_id=source_id,
            entity_name=spec.entity_name,
            entity_type=spec.entity_type,
            schema_json=spec.schema_json,
            etl_sql=spec.etl_sql,
            sensitivity_level=sensitivity,
            owner_id=None,
            description=getattr(spec, "description", None),
        )
        # PII 合规增强：单表采集同样落字段级命中明细
        pii_hits = self._classifier.detect_pii_fields(spec.entity_name, spec.schema_json)
        if pii_hits:
            await self._repo.upsert_classification(
                _cat.id, sensitivity, [_hit_to_dict(h) for h in pii_hits]
            )
        if drift_info is not None:
            await self._handle_drift(source_id, spec.entity_name, drift_info)
        # 刷新成功视为健康信号
        await self._repo.update_health_status(source_id, "healthy")
        await self._db.flush()
        return {
            "source_id": source_id,
            "entity_name": spec.entity_name,
            "sensitivity_level": sensitivity,
            "drifted": drift_info is not None,
            "columns": len(spec.schema_json.get("columns", [])),
        }

    async def sample_entity(
        self,
        source_id: str,
        entity_name: str,
        actor_id: int,
        collector: BaseCollector,
        sample_rows: int | None = None,
    ) -> dict[str, Any]:
        """单表样本采样（不重跑全量采集，只补采样本值）。

        与 ``refresh_entity`` 的区别：刷新会重扫源端元数据（结构/注释/ETL），
        本方法仅对**已在目录中的字段**执行 ``SELECT ... LIMIT n`` 取代表值，
        打码写入 ``schema_json.columns[].sample``，并据此重算字段级 PII 命中
        （采样后 ``name+sample`` 双重验证可提升置信度、纠正仅靠名称的误判）。

        Args:
            source_id: 数据源 ID。
            entity_name: 目录实体名（库.表）。
            actor_id: 操作者（审计留痕）。
            collector: 已构建的连接器。
            sample_rows: 本次采样行数（覆盖 ``quota.sample_rows``；None=取配额）。

        Returns:
            dict 含 entity_name / columns（字段数）/ sampled（取到样本的列数）
            / sample_rows / sensitivity_level / pii_hits（命中数）/ upgraded。

        Raises:
            NotFoundError: 数据源或实体不存在。
            BusinessError: 连接器不支持采样、未开启采样配额、或采样连接未配置。
        """
        src = await self._repo.get_source(source_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        if not getattr(src, "enabled", True):
            raise BusinessError(
                f"数据源已停用: {source_id}，请先在数据源管理启用后再采样",
                error_code="SOURCE_DISABLED",
            )
        quota_cfg = src.quota or {}
        quota_rows = int(quota_cfg.get("sample_rows") or 0) if isinstance(quota_cfg, dict) else 0
        rows = int(sample_rows) if sample_rows is not None else quota_rows
        if rows <= 0:
            raise BusinessError(
                "未开启采样：请先在数据源「配额」中设置采样行数（sample_rows > 0），"
                "或在本次请求中指定 sample_rows",
                error_code="SAMPLING_DISABLED",
            )
        # 区分「不支持采样」与「支持但没取到值」：未覆盖基类默认实现的连接器
        # （如 Kafka 无表结构）静默返回原 schema，会让用户误以为采样成功。
        sample_fn = getattr(type(collector), "sample_columns", None)
        if sample_fn is None or sample_fn is BaseCollector.sample_columns:
            raise BusinessError(
                f"数据源类型 {getattr(src, 'source_type', '')} 不支持样本采样",
                error_code="SAMPLING_UNSUPPORTED",
            )

        cat = await self._repo.get_catalog(source_id, entity_name)
        if cat is None:
            raise NotFoundError(
                f"目录中不存在实体: {entity_name}（请先执行一次采集以建立目录记录）"
            )
        schema_json = dict(cat.schema_json or {})
        columns = schema_json.get("columns")
        if not isinstance(columns, list) or not columns:
            raise BusinessError(
                f"实体 {entity_name} 尚无字段信息，请先刷新元数据再采样",
                error_code="SCHEMA_EMPTY",
            )

        # 采样前命中集合（用于展示本次采样带来的 PII 识别变化）。
        # 必须在 sample_columns 之前计算：连接器的采样是**就地**写入
        # columns[].sample（schema_json 的浅拷贝共享同一列表对象），
        # 若采样后再算会拿到已被污染的 schema，前后对比恒为空。
        before_hits = self._classifier.detect_pii_fields(entity_name, schema_json)
        before_cols = {h.column for h in before_hits}

        collector.set_sampling(rows)
        await self._maybe_load_db_rules()
        schema_json = await collector.sample_columns(entity_name, schema_json)
        sampled = sum(1 for c in schema_json.get("columns", []) if c.get("sample"))

        # 源端编码乱码检测：采样后取连接器登记的乱码字段，标记进 schema_json
        # （前端详情可展示）并返回 mojibake_fields（前端采样提示告警）。乱码是
        # 源端 GBK→UTF-8 替换残留、信息已在源头丢失，仅标记不修改样本值。
        mojibake = getattr(collector, "_take_mojibake", lambda: {})()
        mojibake_fields: list[str] = []
        if mojibake:
            schema_json["mojibake"] = mojibake
            mojibake_fields = sorted(
                set(mojibake.get("sample_fields", []))
                | set(mojibake.get("comment_fields", []))
            )
            logger.warning(
                "样本采样检测到源端编码乱码 source=%s entity=%s fields=%s "
                "（GBK→UTF-8 替换，信息已在源头丢失，请在 Hive 侧修复后重采）",
                source_id,
                entity_name,
                mojibake_fields,
            )

        # 写回 schema_json（仅更新样本，不触发 drift 判定——结构未变）
        cat.schema_json = schema_json
        flag_modified(cat, "schema_json")
        sensitivity = self._classifier.classify(entity_name, schema_json)
        cat.sensitivity_level = sensitivity
        hits = self._classifier.detect_pii_fields(entity_name, schema_json)
        # 仅在「本次有命中」或「此前有命中需清空」时写明细，避免为无命中表
        # 创建空 classification 记录（与 refresh_entity 行为一致）。
        if hits or before_hits:
            await self._repo.upsert_classification(
                cat.id, sensitivity, [_hit_to_dict(h) for h in hits]
            )
        await self._db.flush()
        after_cols = {h.column for h in hits}
        return {
            "source_id": source_id,
            "entity_name": entity_name,
            "columns": len(schema_json.get("columns", [])),
            "sampled": sampled,
            "sample_rows": rows,
            "sensitivity_level": sensitivity,
            "pii_hits": len(hits),
            # 采样后新增/减少的 PII 命中列（双重验证的收益可视化）
            "new_pii_columns": sorted(after_cols - before_cols),
            "cleared_pii_columns": sorted(before_cols - after_cols),
            # 源端编码乱码字段（GBK→UTF-8 替换残留，需源端修复后重采）
            "mojibake_fields": mojibake_fields,
        }

    async def schedule_collection(
        self,
        source_id: str,
        actor_id: int,
        queue: CollectionQueue | None = None,
        *,
        org_id: int | None = None,
        mode: str = "FULL",
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> str:
        """将采集任务投递到异步队列，立即返回 job_id（请求内不再同步执行）。

        ``mode`` 透传到 worker（FULL/INCREMENTAL）——collect-now 前端选择的
        INCREMENTAL 必须实际执行，不能静默降级为全量（跨链路一致性，M4）。
        ``include_patterns`` / ``exclude_patterns`` 为本次临时表级过滤（仅本次生效），
        随任务投递到 worker；None 时 worker 回退到数据源配置的白黑名单。

        当 ``queue`` 未提供时，根据配置自动选择：
        - ``settings.redis_url`` 非空 → ArqCollectionQueue（Redis 持久化）
        - ``settings.redis_url`` 为空 → InMemoryCollectionQueue（降级）

        Raises:
            NotFoundError: 数据源不存在。
        """
        src = await self._repo.get_source(source_id, org_id=org_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        if not getattr(src, "enabled", True):
            raise BusinessError(
                f"数据源已停用: {source_id}，请先在数据源管理启用后再采集",
                error_code="SOURCE_DISABLED",
            )
        from app.core.config import settings as _settings

        q = queue or create_collection_queue(redis_url=_settings.redis_url)
        return await q.enqueue(
            source_id,
            actor_id,
            mode=mode,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )

    async def get_job_status(
        self, job_id: str, queue: CollectionQueue | None = None
    ) -> dict[str, Any] | None:
        """查询采集任务状态（队列自带状态存储时直接读取）。"""
        from app.core.config import settings as _settings

        q = queue or create_collection_queue(redis_url=_settings.redis_url)
        getter = getattr(q, "get", None)
        if getter is None:
            return None
        result: dict[str, Any] | None = await getter(job_id)
        return result

    async def list_jobs(
        self,
        limit: int = 50,
        offset: int = 0,
        source_id: str | None = None,
        status: str | None = None,
        queue: CollectionQueue | None = None,
    ) -> list[dict[str, Any]]:
        """列出采集任务（按入队逆序分页，供采集任务中心展示）。

        可按 ``status`` 过滤（总览仪表「采集任务」资产卡片下钻）。
        队列不支持 list 时返回空列表（不阻断）。
        """
        from app.core.config import settings as _settings

        q = queue or create_collection_queue(redis_url=_settings.redis_url)
        lister = getattr(q, "list", None)
        if lister is None:
            return []
        result: list[dict[str, Any]] = await lister(
            limit=limit, offset=offset, source_id=source_id, status=status
        )
        return result

    async def list_jobs_paged(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        source_id: str | None = None,
        status: str | None = None,
        queue: CollectionQueue | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """采集任务服务端分页：返回 (items, total)。

        total 来自队列 ``count``（与 list 相同过滤），修复前端本地切片导致的
        「超过 50 条任务永远不可见」问题（job 为 ephemeral 数据但任务中心需完整翻页）。
        """
        from app.core.config import settings as _settings

        q = queue or create_collection_queue(redis_url=_settings.redis_url)
        lister = getattr(q, "list", None)
        counter = getattr(q, "count", None)
        if lister is None:
            return [], 0
        items: list[dict[str, Any]] = await lister(
            limit=limit, offset=offset, source_id=source_id, status=status
        )
        total = int(await counter(source_id=source_id, status=status)) if counter else len(items)
        return items, total

    async def count_jobs_by_status(self) -> dict[str, int]:
        """按状态统计采集任务数（供总览仪表「采集任务」资产卡片）。

        采集任务为运行时数据（JobStore：内存 / Redis，终态带 7 天 TTL），
        非持久化表；此处复用 ``list_jobs`` 全量拉取后按 status 聚合。
        队列不支持 list 时返回空分布（不阻断仪表盘）。

        Returns:
            {status: count}，如 {QUEUED: 2, RUNNING: 1, COMPLETED: 5, FAILED: 0}。
        """
        jobs = await self.list_jobs(limit=100000, offset=0)
        counts: dict[str, int] = {}
        for job in jobs:
            status = job.get("status") or "UNKNOWN"
            counts[status] = counts.get(status, 0) + 1
        return counts

    async def update_schedule(
        self,
        source_id: str,
        cron: str,
        mode: str | None = None,
        schedule_enabled: bool | None = None,
    ) -> None:
        """US3: 更新数据源的定时调度配置（schedule_cron [+ collection_mode] [+ 调度启停]）。

        ``mode`` 为 None 时保持数据源现有 ``collection_mode`` 不变——采集模式由
        数据源自身的默认采集模式决定（编辑表单设置），调度只负责 cron 与启停。
        ``schedule_enabled`` 为 None 时保持当前状态（兼容仅改 cron 的旧调用）。
        """
        src = await self._repo.get_source(source_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        src.schedule_cron = cron
        if mode is not None:
            src.collection_mode = mode
        if schedule_enabled is not None:
            src.schedule_enabled = schedule_enabled
        await self._db.flush()

    async def get_watermark(self, source_id: str, org_id: int | None = None) -> dict[str, Any]:
        """US3: 获取数据源采集水位（FR-014）。

        数据源不存在时抛 ``NotFoundError``；存在但从未采集时返回空水位
        （``last_collected_at=None``、计数为 0），而非 404——与 ``get_health``
        语义一致，使前端可正常展示「从未采集」。
        """
        src = await self._repo.get_source(source_id, org_id=org_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        watermark = await self._repo.get_watermark(source_id)
        if watermark is None:
            return {
                "source_id": source_id,
                "last_collected_at": None,
                "mode": src.collection_mode or "FULL",
                "scanned_count": 0,
                "failed_count": 0,
            }
        return {
            "source_id": watermark.source_id,
            "last_collected_at": (
                watermark.last_collected_at.isoformat() if watermark.last_collected_at else None
            ),
            "mode": watermark.mode,
            "scanned_count": watermark.scanned_count,
            "failed_count": watermark.failed_count,
        }

    async def get_health(self, source_id: str, org_id: int | None = None) -> dict[str, Any]:
        """US5: 获取数据源健康状态（FR-016）。

        P1-3 修复：返回真实 ``last_error`` / ``last_health_check``，
        ``uptime_check`` 为存储态健康判断（离线健康，非实时探活）。
        三期：DEGRADED（黄态）时附带 health_metrics / degraded_since。
        """
        src = await self._repo.get_source(source_id, org_id=org_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        watermark = await self._repo.get_watermark(source_id)
        return {
            "source_id": source_id,
            "health_status": src.health_status or "unknown",
            "last_collected_at": (
                watermark.last_collected_at.isoformat()
                if watermark and watermark.last_collected_at
                else None
            ),
            "last_error": src.last_error,
            "last_health_check": (
                src.last_health_check.isoformat() if src.last_health_check else None
            ),
            "uptime_check": src.health_status == "healthy",
            "health_metrics": src.health_metrics,
            "degraded_since": (
                src.degraded_since.isoformat() if src.degraded_since else None
            ),
        }

    async def get_source_overview(self, source_id: str, org_id: int | None = None) -> dict[str, Any]:
        """资产规模概览（详情页头部）：实体类型/PII 分布/字段数/漂移/水位。"""
        overview = await self._repo.get_source_overview(source_id)
        if not overview:
            raise NotFoundError(f"数据源不存在: {source_id}")
        return overview

    async def get_sampling_coverage(self, source_id: str | None = None) -> dict[str, Any]:
        """采样覆盖率（PII 精度增强可观测性）：已采样表数/列数与占比。

        Args:
            source_id: 数据源 ID；None 表示全部数据源（全库口径）。

        Raises:
            NotFoundError: 指定数据源不存在（全库口径不校验）。
        """
        if source_id is not None:
            src = await self._repo.get_source(source_id)
            if src is None:
                raise NotFoundError(f"数据源不存在: {source_id}")
        return await self._repo.get_sampling_coverage(source_id)

    async def list_drift_logs(
        self,
        source_id: str,
        entity_name: str | None = None,
        *,
        org_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """P1-4: 暴露 Schema Drift 变更日志（按检测时间倒序，分页）。

        数据源不存在时抛 ``NotFoundError``；存在但无 drift 记录时返回空列表。
        """
        src = await self._repo.get_source(source_id, org_id=org_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        rows, total = await self._repo.list_drift_logs(
            source_id, entity_name, page=page, page_size=page_size
        )
        items = [
            {
                "source_id": log.source_id,
                "entity_name": log.entity_name,
                "change_type": log.change_type,
                "before_signature": log.before_signature,
                "after_signature": log.after_signature,
                # P2-17: 列表不返回全量 schema（仅 diff_json 摘要）——大字段由
                # repository load_only 排除，此处置 None 避免触发懒加载
                "before_schema": None,
                "after_schema": None,
                "diff_json": log.diff_json,
                "detected_at": log.detected_at.isoformat() if log.detected_at else None,
            }
            for log in rows
        ]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    # ---- 采集运行历史（CollectionRun 持久化，工业级可追溯）----

    @staticmethod
    def _to_run_response(
        run: Any,
        *,
        src_names: dict[str, tuple[str, bool]] | None = None,
        owner_names: dict[int, str] | None = None,
        include_detail: bool = False,
    ) -> dict[str, Any]:
        """CollectionRun ORM → 响应 dict。

        Args:
            run: CollectionRun ORM 对象。
            src_names: {source_id: (名称, 是否删除)}（批量预取，避免 N+1）。
            owner_names: {user_id: 展示名}。
            include_detail: True 时携带 detail_json（详情接口）。
        """
        src_names = src_names or {}
        owner_names = owner_names or {}
        finished_at = run.finished_at
        started_at = run.started_at
        return {
            "id": run.id,
            "source_id": run.source_id,
            "source_name": (src_names.get(run.source_id) or (None, False))[0] or run.source_id,
            "job_id": run.job_id,
            "trigger": run.trigger,
            "mode": run.mode,
            "effective_mode": run.effective_mode,
            "status": run.status,
            "actor_id": run.actor_id,
            "actor_name": owner_names.get(run.actor_id) if run.actor_id else None,
            "started_at": started_at.isoformat() if started_at else None,
            "finished_at": finished_at.isoformat() if finished_at else None,
            "duration_seconds": (
                round((finished_at - started_at).total_seconds(), 1)
                if started_at and finished_at
                else None
            ),
            "scanned": run.scanned,
            "registered": run.registered,
            "pii_registered": run.pii_registered,
            "failed_count": run.failed_count,
            "drift_count": run.drift_count,
            "deprecated_count": run.deprecated_count,
            "dsd_count": (run.detail_json or {}).get("dsd_count", 0),
            "coverage": run.coverage,
            "error": run.error,
            "detail": run.detail_json if include_detail else None,
        }

    async def start_collection_run(
        self,
        *,
        source_id: str,
        trigger: str = "manual",
        mode: str = "FULL",
        job_id: str | None = None,
        actor_id: int | None = None,
    ) -> int:
        """创建采集运行记录（RUNNING）并提交——独立记录，立即可见、进程崩溃不丢。"""
        run = await self._repo.create_collection_run(
            source_id=source_id,
            trigger=trigger,
            mode=mode,
            job_id=job_id,
            actor_id=actor_id,
        )
        await self._db.commit()
        return int(run.id)

    async def complete_collection_run(self, run_id: int, result: dict[str, Any]) -> None:
        """采集成功收尾（回填指标 + COMPLETED）并提交。"""
        await self._repo.complete_collection_run(run_id, result)
        await self._db.commit()

    async def fail_collection_run(self, run_id: int, error: str) -> None:
        """采集失败收尾（记录错误 + FAILED）并提交。

        P2-2：错误文本先脱敏（DSN 内嵌凭据/密码回显，TD §13）再截断落库——
        采集失败原始异常可能含 DB 连接串/账号密码等内部细节，且
        ``GET /collection-runs/{id}`` 允许任意登录用户读取，防凭据/内部信息泄露。
        """
        sanitized = _sanitize_conn_error(error or "")[:512]
        await self._repo.fail_collection_run(run_id, sanitized)
        await self._db.commit()

    async def cancel_collection_run(self, run_id: int) -> None:
        """用户主动取消收尾（状态 → CANCELLED，2026-08-28 起与 JobStore 终态对齐）。"""
        await self._repo.cancel_collection_run(run_id)
        await self._db.commit()

    async def list_collection_runs(
        self,
        *,
        source_id: str | None = None,
        status: str | None = None,
        trigger: str | None = None,
        page: int = 1,
        page_size: int = 20,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
    ) -> dict[str, Any]:
        """采集运行历史分页列表（按开始时间倒序，批量回填源名/责任人）。"""
        runs, total = await self._repo.list_collection_runs(
            source_id=source_id,
            status=status,
            trigger=trigger,
            page=page,
            page_size=page_size,
            started_after=started_after,
            started_before=started_before,
        )
        source_ids = {r.source_id for r in runs}
        src_names = await self._repo.get_sources_meta(list(source_ids)) if source_ids else {}
        actor_ids = {r.actor_id for r in runs if r.actor_id is not None}
        owner_names = (
            await self._repo.get_owner_names(list(actor_ids)) if actor_ids else {}
        )
        items = [
            self._to_run_response(r, src_names=src_names, owner_names=owner_names)
            for r in runs
        ]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def get_collection_run_summary(
        self,
        *,
        source_id: str | None = None,
        status: str | None = None,
        trigger: str | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
    ) -> dict[str, int]:
        """采集运行历史聚合统计（服务端 SQL 聚合，供前端统计摘要）。"""
        return await self._repo.summarize_collection_runs(
            source_id=source_id,
            status=status,
            trigger=trigger,
            started_after=started_after,
            started_before=started_before,
        )

    async def get_collection_run_detail(self, run_id: int) -> dict[str, Any]:
        """采集运行详情（含失败/漂移明细）。"""
        run = await self._repo.get_collection_run(run_id)
        if run is None:
            raise NotFoundError(f"采集运行记录不存在: {run_id}")
        src_names = await self._repo.get_sources_meta([run.source_id])
        owner_names = (
            await self._repo.get_owner_names([run.actor_id])
            if run.actor_id is not None
            else {}
        )
        return self._to_run_response(
            run,
            src_names=src_names,
            owner_names=owner_names,
            include_detail=True,
        )

    async def flush_run_logs(
        self, run_id: int, entries: list[dict[str, Any]]
    ) -> None:
        """把 Redis 实时缓冲的采集日志批量落库（任务终态回写，长期可追溯）。

        采集期间日志先写 Redis（实时可读），收尾一次性 bulk 落 ``collection_run_log``
        表——避免采集高频逐条 INSERT 拖慢主流程。日志回写失败不阻断采集收尾。

        Args:
            run_id: 采集运行记录 ID。
            entries: 日志条目列表 [{ts, level, phase, entity_name, message}]。
        """
        try:
            await self._repo.append_run_logs(run_id, entries)
            await self._db.commit()
        except Exception as exc:  # noqa: BLE001 - 日志回写是辅助能力
            logger.warning(
                "collection_run_log_flush_failed: run=%s err=%s", run_id, exc
            )

    async def get_collection_run_logs(
        self, run_id: int, offset: int = 0, limit: int = 200
    ) -> dict[str, Any]:
        """采集运行日志分页查询（采集记录详情页「实时日志」）。

        读取策略：
        - 已回写 DB（终态）→ 读 ``collection_run_log`` 表（长期可追溯）；
        - 未回写（RUNNING 中或崩溃未收尾）→ 读 Redis 实时缓冲
          （``collect:run_log:{run_id}``，RUNNING 期间前端轮询可见）；
        - Redis 不可用且 DB 无日志 → 空列表。

        Returns:
            {items: [{ts, level, phase, entity_name, message}], total, source, status}。
        """
        run = await self._repo.get_collection_run(run_id)
        if run is None:
            raise NotFoundError(f"采集运行记录不存在: {run_id}")
        # 优先 DB（终态已回写）——长期可追溯
        if await self._repo.has_run_logs(run_id):
            rows, total = await self._repo.list_run_logs(run_id, offset, limit)
            items = [
                {
                    "ts": r.ts.isoformat() if r.ts else None,
                    "level": r.level,
                    "phase": r.phase,
                    "entity_name": r.entity_name,
                    "message": r.message,
                }
                for r in rows
            ]
            return {"items": items, "total": total, "source": "db", "status": run.status}
        # 未回写：读 Redis 实时缓冲（RUNNING 中 / 崩溃未收尾）
        redis = get_redis() if _redis_available() else None
        if redis is not None:
            from app.services.collector.queue import read_run_logs

            items, total = await read_run_logs(redis, run_id, offset, limit)
            return {"items": items, "total": total, "source": "redis", "status": run.status}
        return {"items": [], "total": 0, "source": "none", "status": run.status}
