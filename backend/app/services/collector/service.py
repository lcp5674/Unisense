"""采集领域服务（对齐 TD §12.1 / DEV_GUIDE §8b.1）。

职责：数据源注册/查询/删除、元数据注册（含敏感分级与幂等）、批量废弃（207）、
自动采集编排。所有写操作经 SecretManager 加密凭据、经审计落库、经事件发布（熔断）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.config import Settings
from app.core.exceptions import BusinessError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.secrets import SecretManager
from app.db.redis import get_redis
from app.models.collector_models import SchemaDriftLog
from app.models.data_source import DataSource
from app.services.collector.classifier import SensitivityClassifier
from app.services.collector.events import CatalogEventPublisher
from app.services.collector.queue import CollectionQueue, create_collection_queue
from app.services.collector.repository import CollectorRepository
from app.services.collector.schemas import (
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
from app.services.llm.client import LlmError

logger = get_logger("unisense.collector.service")

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
        self._settings = Settings()

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
            schedule_cron=src.schedule_cron,
            collection_mode=src.collection_mode or "FULL",
            enabled=bool(getattr(src, "enabled", True)),
            created_by=src.created_by,
            created_at=src.created_at,
            updated_at=src.updated_at,
        )

    async def create_source(
        self, req: DataSourceCreateRequest, actor_id: int
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
            domain=req.domain,
            cluster_id=req.cluster_id,
            coverage=0.0,
            health_status="unknown",
            quota={},
            created_by=actor_id,
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
            collector = registry.build_from_cfg(source_type, cfg)
            try:
                probe = await collector.probe()
            finally:
                await collector.dispose()
        except Exception as exc:  # 类型未注册 / 构建失败 / 探活异常
            logger.warning("test_connection_failed: type=%s err=%s", source_type, exc)
            return TestConnectionResult(ok=False, source_type=source_type, error=str(exc))
        return TestConnectionResult(
            ok=probe.ok,
            source_type=source_type,
            latency_ms=probe.latency_ms,
            error=probe.error,
            detail=probe.detail,
        )

    async def list_databases(self, source_type: str, cfg: dict[str, Any]) -> list[str]:
        """枚举实例下可采集的非系统数据库（创建数据源时选择目标库）。

        明文配置构建采集器（与 test_connection 一致），不支持枚举的
        连接器（如 Kafka）返回空列表，前端回退为手填；任何异常同样返回空。

        Args:
            source_type: 采集器类型。
            cfg: 明文连接配置（不落库）。

        Returns:
            非系统数据库名列表。
        """
        from app.services.collector.connectors import registry

        try:
            collector = registry.build_from_cfg(source_type, cfg)
            try:
                return await collector.list_databases()
            finally:
                await collector.dispose()
        except Exception as exc:
            logger.warning("list_databases_failed: type=%s err=%s", source_type, exc)
            return []

    async def check_connection(self, source_id: str) -> TestConnectionResult:
        """存量数据源实时探活：解密配置 → 轻量连接 → 更新健康状态与探活时间。"""
        from app.services.collector.connectors import registry

        src = await self._repo.get_source(source_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        try:
            collector = registry.build(src.source_type, src.connection_config)
            try:
                probe = await collector.probe()
            finally:
                await collector.dispose()
        except Exception as exc:
            # P1-3: 探活失败记录错误信息（供健康端点返回 last_error）
            await self._repo.update_health_status(source_id, "unhealthy", error=str(exc))
            logger.warning("check_connection_failed: source=%s err=%s", source_id, exc)
            return TestConnectionResult(ok=False, source_type=src.source_type, error=str(exc))
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
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[DataSourceResponse], int]:
        sources, total = await self._repo.list_sources(
            domain=domain,
            source_type=source_type,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
        return [self._to_source_response(s) for s in sources], total

    async def get_source(self, source_id: str) -> DataSourceResponse:
        src = await self._repo.get_source(source_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        # 详情/编辑回显需要明文连接配置（列表保持脱敏）
        return self._to_source_response(src, include_config=True)

    async def update_source(
        self, source_id: str, req: DataSourceUpdateRequest, actor_id: int
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
        src = await self._repo.get_source(source_id)
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
            src.connection_config = self._secrets.encrypt(req.connection_config)
        if req.name is not None:
            src.name = req.name
        if req.domain is not None:
            src.domain = req.domain
        if req.cluster_id is not None:
            src.cluster_id = req.cluster_id
        if req.enabled is not None:
            src.enabled = req.enabled
        # updated_at 由 BaseModel.onupdate 自动维护；连接配置变更后健康状态重置
        # （旧探活结果对新凭据不再可信），并清空历史错误。
        if req.connection_config is not None:
            src.health_status = "unknown"
            src.last_error = None
        await self._db.flush()
        return self._to_source_response(src)

    async def delete_source(self, source_id: str) -> None:
        if not await self._repo.soft_delete_source(source_id):
            raise NotFoundError(f"数据源不存在: {source_id}")

    async def get_source_orm(self, source_id: str) -> DataSource:
        """取原始 DataSource（供采集编排还原连接配置，不对外暴露明文）。"""
        src = await self._repo.get_source(source_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        return src

    async def register_catalog(
        self, req: DBCatalogCreateRequest, actor_id: int
    ) -> DBCatalogResponse:
        # source_id 是下游唯一键，缺失时服务层防御性拒绝
        # （API 层会按路径回填，但 worker/任务路径直接调用服务时需自行保证）
        if req.source_id is None:
            raise ValidationError("source_id 缺失：必须由路径参数或请求体提供")
        # 规则引擎分类（确定性基线）
        rule_sensitivity = self._classifier.classify(req.entity_name, req.schema_def)
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

        cat, _created, drift_info = await self._repo.upsert_catalog(
            source_id=req.source_id,
            entity_name=req.entity_name,
            entity_type=req.entity_type,
            schema_json=req.schema_def,
            etl_sql=req.etl_sql,
            sensitivity_level=sensitivity,
            owner_id=req.owner_id,
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

        # US2: 检测到 Schema Drift 时发布事件 + 记录变更历史
        if drift_info is not None:
            await self._handle_drift(req.source_id, req.entity_name, drift_info)

        return DBCatalogResponse.model_validate(cat)

    async def _llm_classify_sensitivity(
        self, entity_name: str, schema_def: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """使用 LLM 辅助分类敏感级别，返回结构化结果。

        LLM 不可用时返回 None（不阻断主流程）。
        """
        client = None
        try:
            from app.services.llm.client import build_llm_client

            client = build_llm_client()
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

        复用 build_llm_client + 熔断器模式（与 _llm_classify_sensitivity 一致）。
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
            from app.services.llm.client import build_llm_client

            client = build_llm_client()
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

            result = await client.chat(
                messages,
                temperature=0.0,
                max_tokens=200,
                response_format={"type": "json_object"},
            )
            description = result.get("description", "")
            confidence = float(result.get("confidence", 0) or 0)
            if not description or confidence <= 0:
                return None
            return {"description": description, "confidence": confidence}
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

    async def _llm_infer_table_description(
        self,
        entity_name: str,
        columns: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """使用 LLM 推断表级业务描述（表名 + 字段清单上下文）。

        复用 build_llm_client + 熔断器模式（与字段推断一致）；
        LLM 不可用返回 None（不阻断主流程）。
        """
        client = None
        try:
            from app.services.llm.client import build_llm_client

            client = build_llm_client()
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

            result = await client.chat(
                messages,
                temperature=0.0,
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            description = result.get("description", "")
            confidence = float(result.get("confidence", 0) or 0)
            if not description or confidence <= 0:
                return None
            return {"description": description, "confidence": confidence}
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

    async def list_catalogs(self, params: DBCatalogListParams) -> DBCatalogListResponse:
        cats, total = await self._repo.list_catalogs(params)
        # 批量补源维度信息（名称 / 删除状态）：join 路径已带瞬态属性，普通路径批量查询
        source_ids = {c.source_id for c in cats}
        meta = await self._repo.get_sources_meta(list(source_ids)) if source_ids else {}
        items: list[DBCatalogResponse] = []
        for c in cats:
            resp = DBCatalogResponse.model_validate(c)
            src_deleted = getattr(c, "_src_deleted", None)
            if src_deleted is not None:
                resp.source_deleted = bool(src_deleted)
                resp.source_name = getattr(c, "_src_name", None) or c.source_id
            else:
                name, deleted = meta.get(c.source_id, (None, True))
                resp.source_deleted = deleted
                resp.source_name = name or c.source_id
            items.append(resp)
        return DBCatalogListResponse(
            items=items,
            total=total,
            page=params.page,
            page_size=params.page_size,
        )

    async def list_catalog_databases(self, source_id: str | None = None) -> list[str]:
        """目录去重库名列表（供前端库名筛选下拉，可随 source_id 联动）。"""
        return await self._repo.list_catalog_databases(source_id)

    async def get_catalog_detail(self, catalog_id: int) -> DBCatalogResponse:
        """按主键取目录实体详情（血缘图谱表节点下钻用）。

        Raises:
            NotFoundError: 目录实体不存在（含已删除）。
        """
        cat = await self._repo.get_catalog_by_id(catalog_id)
        if cat is None:
            raise NotFoundError(f"目录实体不存在: {catalog_id}")
        resp = DBCatalogResponse.model_validate(cat)
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
    ) -> dict[str, Any]:
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
                    and should_mix_in(
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

        # US5: 采集成功后更新健康状态
        await self._emit_progress(
            progress_cb,
            {"phase": "start", "message": f"开始采集 {source_id}（{effective_mode} 模式）"},
        )
        try:
            # P0-6: 注入增量上下文——增量模式且水位有效时连接器只采变更实体
            collector.set_incremental_context(effective_mode, watermark_ts)
            result: CollectResult = await collector.collect(src)
        except Exception as exc:
            # P0-4: 健康状态更新必须落库——即使采集失败也要记录 unhealthy，
            # 否则 API/worker 上抛后被 get_db_session 回滚，健康状态永不更新。
            await self._repo.update_health_status(source_id, "unhealthy", error=str(exc))
            await self._db.commit()
            raise

        await self._emit_progress(
            progress_cb,
            {
                "phase": "scanning",
                "scanned": len(result.specs),
                "message": f"源端扫描完成，发现 {len(result.specs)} 个实体",
            },
        )

        registered = 0
        pii_registered = 0
        batch_payloads: list[dict[str, Any]] = []
        drift_events: list[dict[str, Any]] = []
        content_fingerprints: dict[str, str] = {}
        catalog_failed_specs: list[dict[str, str]] = []
        entities: list[dict[str, Any]] = []
        for spec in result.specs:
            sensitivity = self._classifier.classify(spec.entity_name, spec.schema_json)
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

        # P1-5: 废弃表自动对账——仅在全量采集后执行（增量仅覆盖变更实体，不可对未采集实体误废）。
        # 对比 catalog 中仍存活的实体与本次源端扫描到的实体名，源端已 drop 的标记为 DEPRECATED。
        deprecated_count = 0
        if effective_mode == "FULL":
            collected_names = {spec.entity_name for spec in result.specs}
            active_names = await self._repo.list_active_entity_names(source_id)
            for name in active_names:
                if name not in collected_names:
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

        coverage = await self._repo.recompute_coverage(source_id)

        # US5: 采集成功 → 更新健康状态
        await self._repo.update_health_status(source_id, "healthy")

        # US3: 更新采集水位
        await self._repo.update_watermark_after_collection(
            source_id=source_id,
            mode=effective_mode,
            scanned_count=len(result.specs),
            failed_count=len(result.failed_specs) + len(catalog_failed_specs),
            content_fingerprints=content_fingerprints or None,
        )

        # P0-4: 合并 collector 层 failed_specs 与 catalog 层 failed_specs
        all_failed_specs = [
            {"entity_name": f.entity_name, "error": f.error} for f in result.failed_specs
        ] + catalog_failed_specs
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
            "entities": entities,
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

        sensitivity = self._classifier.classify(spec.entity_name, spec.schema_json)
        _cat, _created, drift_info = await self._repo.upsert_catalog(
            source_id=source_id,
            entity_name=spec.entity_name,
            entity_type=spec.entity_type,
            schema_json=spec.schema_json,
            etl_sql=spec.etl_sql,
            sensitivity_level=sensitivity,
            owner_id=None,
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

    async def schedule_collection(
        self, source_id: str, actor_id: int, queue: CollectionQueue | None = None
    ) -> str:
        """将全量采集任务投递到异步队列，立即返回 job_id（请求内不再同步执行）。

        当 ``queue`` 未提供时，根据配置自动选择：
        - ``settings.redis_url`` 非空 → ArqCollectionQueue（Redis 持久化）
        - ``settings.redis_url`` 为空 → InMemoryCollectionQueue（降级）

        Raises:
            NotFoundError: 数据源不存在。
        """
        src = await self._repo.get_source(source_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        if not getattr(src, "enabled", True):
            raise BusinessError(
                f"数据源已停用: {source_id}，请先在数据源管理启用后再采集",
                error_code="SOURCE_DISABLED",
            )
        from app.core.config import settings as _settings

        q = queue or create_collection_queue(redis_url=_settings.redis_url)
        return await q.enqueue(source_id, actor_id)

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
        queue: CollectionQueue | None = None,
    ) -> list[dict[str, Any]]:
        """列出采集任务（按入队逆序分页，供采集任务中心展示）。

        队列不支持 list 时返回空列表（不阻断）。
        """
        from app.core.config import settings as _settings

        q = queue or create_collection_queue(redis_url=_settings.redis_url)
        lister = getattr(q, "list", None)
        if lister is None:
            return []
        result: list[dict[str, Any]] = await lister(limit=limit, offset=offset, source_id=source_id)
        return result

    async def update_schedule(self, source_id: str, cron: str, mode: str) -> None:
        """US3: 更新数据源的定时调度配置（schedule_cron + collection_mode）。"""
        src = await self._repo.get_source(source_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        src.schedule_cron = cron
        src.collection_mode = mode
        await self._db.flush()

    async def get_watermark(self, source_id: str) -> dict[str, Any]:
        """US3: 获取数据源采集水位（FR-014）。

        数据源不存在时抛 ``NotFoundError``；存在但从未采集时返回空水位
        （``last_collected_at=None``、计数为 0），而非 404——与 ``get_health``
        语义一致，使前端可正常展示「从未采集」。
        """
        src = await self._repo.get_source(source_id)
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

    async def get_health(self, source_id: str) -> dict[str, Any]:
        """US5: 获取数据源健康状态（FR-016）。

        P1-3 修复：返回真实 ``last_error`` / ``last_health_check``，
        ``uptime_check`` 为存储态健康判断（离线健康，非实时探活）。
        """
        src = await self._repo.get_source(source_id)
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
        }

    async def list_drift_logs(
        self,
        source_id: str,
        entity_name: str | None = None,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """P1-4: 暴露 Schema Drift 变更日志（按检测时间倒序，分页）。

        数据源不存在时抛 ``NotFoundError``；存在但无 drift 记录时返回空列表。
        """
        src = await self._repo.get_source(source_id)
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
                "before_schema": log.before_schema,
                "after_schema": log.after_schema,
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
