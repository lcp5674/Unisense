"""Kafka 连接器（对齐 TD §12.1 / spec FR-001）。

采集 Topic 列表 + Schema Registry 元数据。
- Broker: 通过 httpx 调用 REST API（kafka-python 不作为必需依赖）
- Schema Registry: GET http://{registry_host}:{port}/subjects + /subjects/{subject}/versions/latest
- 支持 Basic Auth（connection_config 中可选 auth_user/auth_password）
- 无增量支持，始终全量
- @registry.register("kafka") 注册

注意：Kafka Broker REST Proxy 非 Kafka 原生接口，
实际 Topic 列表需通过 kafka-python 或 Kafka AdminClient 获取。
本实现使用 kafka-python（可选依赖）获取 Topic 元数据，
未安装时降级为空列表。
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx

from app.core.exceptions import ExternalDependencyError
from app.services.collector.classifier import SensitivityClassifier
from app.services.collector.connectors.collector_registry import registry
from app.services.collector.spi import (
    BaseCollector,
    CatalogSpec,
    CollectResult,
    FailedSpec,
    ProbeResult,
)

logger = logging.getLogger("unisense.collector.connectors.kafka")


class KafkaCollector(BaseCollector):
    """Kafka 采集器：Broker Topic 列表 + Schema Registry 元数据。"""

    def __init__(
        self,
        bootstrap_servers: str = "127.0.0.1:9092",
        registry_url: str | None = None,
        registry_user: str | None = None,
        registry_password: str | None = None,
        classifier: SensitivityClassifier | None = None,
        *,
        security_protocol: str | None = None,
        sasl_mechanism: str | None = None,
        sasl_username: str | None = None,
        sasl_password: str | None = None,
        ssl_cafile: str | None = None,
    ) -> None:
        super().__init__(classifier)
        self._bootstrap_servers = bootstrap_servers
        self._registry_url = registry_url
        self._registry_user = registry_user
        self._registry_password = registry_password
        # P2-5: 生产 Kafka 常启用 SASL/SSL——透传连接参数（若客户端支持）
        self._security_protocol = security_protocol
        self._sasl_mechanism = sasl_mechanism
        self._sasl_username = sasl_username
        self._sasl_password = sasl_password
        self._ssl_cafile = ssl_cafile

    def _admin_kwargs(self) -> dict[str, Any]:
        """构建 kafka-python KafkaAdminClient 连接参数（含可选 SASL/SSL）。"""
        kwargs: dict[str, Any] = {
            "bootstrap_servers": self._bootstrap_servers,
            "request_timeout_ms": 10000,
        }
        if self._security_protocol:
            kwargs["security_protocol"] = self._security_protocol
        if self._sasl_mechanism:
            kwargs["sasl_mechanism"] = self._sasl_mechanism
            kwargs["sasl_plain_username"] = self._sasl_username or ""
            kwargs["sasl_plain_password"] = self._sasl_password or ""
        if self._ssl_cafile:
            kwargs["ssl_cafile"] = self._ssl_cafile
        return kwargs

    def _registry_headers(self) -> dict[str, str]:
        """构建 Schema Registry Basic Auth 头。"""
        if self._registry_user and self._registry_password:
            token = base64.b64encode(
                f"{self._registry_user}:{self._registry_password}".encode()
            ).decode()
            return {"Authorization": f"Basic {token}"}
        return {}

    async def _get_topics(self) -> list[dict[str, Any]]:
        """获取 Kafka Topic 列表（含分区数/副本因子）。

        P0-5/P2-5 修复：
        - 依赖缺失（kafka-python 未安装）时明确抛 ExternalDependencyError，
          不再静默返回空列表（否则「测试连接」恒假成功）。
        - 异常路径在 finally 中 close 客户端，避免连接泄漏。
        """
        try:
            from kafka import KafkaAdminClient
        except ImportError:
            raise ExternalDependencyError(
                "kafka-python 未安装，无法连接 Kafka Broker；"
                "请安装 kafka-python（可选依赖 collectors 组）"
            ) from None

        client: KafkaAdminClient | None = None
        try:
            client = KafkaAdminClient(**self._admin_kwargs())
            topics = client.list_topics()
            topic_details = []
            for topic_name in topics:
                # 获取 Topic 元数据（describe_topics 为旧 API 但功能稳定；
                # 依赖客户端不泄漏——见 finally close）
                try:
                    partitions = client.describe_topics([topic_name])
                    partition_count = 0
                    replication_factor = 0
                    if partitions and len(partitions) > 0:
                        partition_count = len(partitions[0].partitions)
                        if partitions[0].partitions:
                            replication_factor = len(partitions[0].partitions[0].replicas)
                    topic_details.append(
                        {
                            "name": topic_name,
                            "partition_count": partition_count,
                            "replication_factor": replication_factor,
                        }
                    )
                except Exception as exc:
                    logger.warning("获取 Topic %s 元数据失败: %s", topic_name, exc)
                    topic_details.append(
                        {
                            "name": topic_name,
                            "partition_count": 0,
                            "replication_factor": 0,
                        }
                    )
            return topic_details
        except Exception as exc:
            raise ExternalDependencyError(
                f"连接 Kafka Broker 失败 ({self._bootstrap_servers}): {exc}"
            ) from exc
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception as exc:  # noqa: BLE001 - 关闭失败不影响结果
                    logger.warning("关闭 Kafka 客户端失败: %s", exc)

    async def _get_subject_schemas(self, client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
        """从 Schema Registry 获取所有 Subject 的最新 Schema。"""
        if not self._registry_url:
            return {}

        headers = self._registry_headers()
        try:
            resp = await client.get(f"{self._registry_url}/subjects", headers=headers)
            resp.raise_for_status()
            subjects: list[str] = resp.json()
        except Exception as exc:
            logger.warning("获取 Schema Registry subjects 失败: %s", exc)
            return {}

        result: dict[str, dict[str, Any]] = {}
        for subject in subjects:
            try:
                resp = await client.get(
                    f"{self._registry_url}/subjects/{subject}/versions/latest",
                    headers=headers,
                )
                resp.raise_for_status()
                schema_info = resp.json()
                result[subject] = {
                    "schema": schema_info.get("schema", ""),
                    "schema_type": schema_info.get("schemaType", "AVRO"),
                    "version": schema_info.get("version"),
                }
            except Exception as exc:
                logger.warning("获取 Subject %s Schema 失败: %s", subject, exc)

        return result

    async def collect(self, source: Any) -> CollectResult:
        source_id = getattr(source, "source_id", "?")

        # 获取 Topic 列表
        topics = await self._get_topics()

        # 获取 Schema Registry 信息
        subject_schemas: dict[str, dict[str, Any]] = {}
        if self._registry_url:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    subject_schemas = await self._get_subject_schemas(client)
            except Exception as exc:
                logger.warning("Schema Registry 连接失败: %s", exc)

        specs: list[CatalogSpec] = []
        failed_specs: list[FailedSpec] = []

        for topic in topics:
            topic_name = topic.get("name", "")
            if not topic_name:
                continue
            try:
                # 构建 schema_json: Topic 元数据 + 关联的 Schema
                schema_json: dict[str, Any] = {
                    "partition_count": topic.get("partition_count", 0),
                    "replication_factor": topic.get("replication_factor", 0),
                }
                # 关联 Subject Schema（Topic 名通常对应 subject 后缀）
                topic_key = f"{topic_name}-value"
                if topic_key in subject_schemas:
                    schema_json["schema_info"] = subject_schemas[topic_key]

                specs.append(
                    CatalogSpec(
                        entity_name=topic_name,
                        entity_type="TABLE",
                        schema_json=schema_json,
                    )
                )
            except Exception as exc:
                logger.warning("采集源 %s Topic %s 处理失败: %s", source_id, topic_name, exc)
                failed_specs.append(FailedSpec(entity_name=topic_name, error=str(exc)))

        # 如果无 Topic 但有 Subject，也采集
        for subject, schema_info in subject_schemas.items():
            existing_names = {s.entity_name for s in specs}
            if subject not in existing_names:
                specs.append(
                    CatalogSpec(
                        entity_name=subject,
                        entity_type="TABLE",
                        schema_json=schema_info,
                    )
                )

        return CollectResult(specs=specs, failed_specs=failed_specs, source_id=source_id)

    async def probe(self) -> ProbeResult:
        """轻量探活：尝试获取 Broker Topic 列表（含 Schema Registry 连通性）。"""
        start = time.monotonic()
        detail: dict[str, Any] = {}
        try:
            topics = await self._get_topics()
            detail["topics"] = len(topics)
            if self._registry_url:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        subjects = await self._get_subject_schemas(client)
                        detail["schema_subjects"] = len(subjects)
                except Exception as exc:  # Registry 不可达不判定整体失败
                    detail["schema_registry"] = f"不可达: {exc}"
            return ProbeResult(
                ok=True,
                latency_ms=int((time.monotonic() - start) * 1000),
                detail=detail or None,
            )
        except Exception as exc:
            return ProbeResult(
                ok=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                error=str(exc),
            )


@registry.register("kafka")
def create_kafka_collector(cfg: dict[str, Any]) -> KafkaCollector:
    """Kafka 采集器工厂函数。"""
    return KafkaCollector(
        bootstrap_servers=cfg.get("bootstrap_servers", cfg.get("host", "127.0.0.1:9092")),
        registry_url=cfg.get("registry_url"),
        registry_user=cfg.get("registry_user", cfg.get("auth_user")),
        registry_password=cfg.get("registry_password", cfg.get("auth_password")),
        security_protocol=cfg.get("security_protocol"),
        sasl_mechanism=cfg.get("sasl_mechanism"),
        sasl_username=cfg.get("sasl_username"),
        sasl_password=cfg.get("sasl_password"),
        ssl_cafile=cfg.get("ssl_cafile"),
    )
