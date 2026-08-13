"""采集领域请求/响应 Schema（对齐 TD §12.1 / DEV_GUIDE §8b.1）。

注意：
- ``DataSourceResponse`` 不暴露 ``connection_config`` 明文，仅以
  ``connection_config_present`` 标记是否存在，满足凭据脱敏（TD §13）。
- ``schema_json`` 与 Pydantic ``BaseModel.schema_json()`` 冲突，字段名用
  ``schema_def``，并以 alias="schema_json" 与模型列对齐（populate_by_name）。
- ``SourceType`` / ``EntityType`` 引用共享枚举（FR-003/FR-007）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import EntityTypeEnum, SourceTypeEnum

SourceType = SourceTypeEnum
EntityType = EntityTypeEnum


class DataSourceCreateRequest(BaseModel):
    """数据源注册请求。

    生产约定：``source_id`` 可选——不传时由系统按
    ``{source_type}_{database|domain}`` 自动生成（见 CollectorService.create_source）。
    """

    source_id: str | None = Field(default=None, min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    source_type: SourceType
    connection_config: dict[str, Any]
    domain: str = Field(max_length=64)
    cluster_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _validate_connection_config(self) -> DataSourceCreateRequest:
        """FR-020/P2-7: 按类型校验连接配置必填项。

        - kafka：需要 ``bootstrap_servers`` 或 ``host``（语义错位修复）；
        - 其余类型：必须包含 ``host``。
        """
        cfg = self.connection_config
        if not isinstance(cfg, dict):
            raise ValueError("connection_config 必须是对象")
        source_type_value = (
            self.source_type.value if hasattr(self.source_type, "value") else str(self.source_type)
        )
        if source_type_value == "kafka":
            if "bootstrap_servers" not in cfg and "host" not in cfg:
                raise ValueError("kafka 的 connection_config 必须包含 bootstrap_servers 或 host")
        elif "host" not in cfg:
            raise ValueError("connection_config 必须包含 host 字段")
        return self


class DataSourceTypeInfo(BaseModel):
    """数据源类型元信息（供前端动态渲染类型选择器）。"""

    source_type: str
    label: str
    default_port: int
    supports_database: bool
    supports_schema: bool
    description: str


class TestConnectionRequest(BaseModel):
    """连接测试请求（创建数据源前预检，不落库）。"""

    source_type: SourceType
    connection_config: dict[str, Any]

    @model_validator(mode="after")
    def _validate_connection_config(self) -> TestConnectionRequest:
        cfg = self.connection_config
        if not isinstance(cfg, dict):
            raise ValueError("connection_config 必须是对象")
        source_type_value = (
            self.source_type.value if hasattr(self.source_type, "value") else str(self.source_type)
        )
        if source_type_value == "kafka":
            if "bootstrap_servers" not in cfg and "host" not in cfg:
                raise ValueError("kafka 的 connection_config 必须包含 bootstrap_servers 或 host")
        elif "host" not in cfg:
            raise ValueError("connection_config 必须包含 host 字段")
        return self


class TestConnectionResult(BaseModel):
    """连接测试结果。"""

    ok: bool
    source_type: str
    latency_ms: int | None = None
    error: str | None = None
    detail: dict[str, Any] | None = None


class DataSourceResponse(BaseModel):
    """数据源响应（脱敏：不含 connection_config 明文）。"""

    source_id: str
    name: str
    source_type: SourceType
    domain: str
    cluster_id: str | None = None
    coverage: float
    health_status: str
    connection_config_present: bool
    schedule_cron: str | None = None
    collection_mode: str = "FULL"
    created_by: int | None = None
    created_at: Any = None
    updated_at: Any = None


class DataSourceListResponse(BaseModel):
    """数据源列表分页响应（P1-1：此前仅返回 20 条、total 被丢弃导致静默截断）。"""

    items: list[DataSourceResponse]
    total: int
    page: int
    page_size: int


class DBCatalogCreateRequest(BaseModel):
    """元数据实体注册请求。"""

    model_config = ConfigDict(populate_by_name=True)

    source_id: str = Field(max_length=64)
    entity_name: str = Field(max_length=255)
    entity_type: EntityType = EntityTypeEnum.TABLE
    schema_def: dict[str, Any] = Field(alias="schema_json")
    etl_sql: str | None = None
    owner_id: int | None = None


class DBCatalogResponse(BaseModel):
    """元数据实体响应。"""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    source_id: str
    entity_name: str
    entity_type: str
    schema_def: dict[str, Any] = Field(alias="schema_json")
    etl_sql: str | None = None
    sensitivity_level: str
    owner_id: int | None = None
    upstream_signature: str
    content_signature: str | None = None
    schema_incomplete: bool = False


class DBCatalogListParams(BaseModel):
    """元数据列表过滤参数。"""

    source_id: str | None = None
    entity_type: str | None = None
    sensitivity_level: str | None = None
    keyword: str | None = Field(default=None, max_length=128)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)


class DBCatalogListResponse(BaseModel):
    """元数据列表响应。"""

    items: list[DBCatalogResponse]
    total: int
    page: int
    page_size: int


class BulkDeprecateItem(BaseModel):
    """批量废弃单项（由 source_id + entity_name 唯一定位）。"""

    source_id: str = Field(max_length=64)
    entity_name: str = Field(max_length=255)


class BulkDeprecateRequest(BaseModel):
    """批量废弃请求（部分失败返回 207，逐项标注）。"""

    items: list[BulkDeprecateItem] = Field(min_length=1, max_length=500)


class BulkDeprecateResult(BaseModel):
    """批量废弃结果。"""

    succeeded: list[BulkDeprecateItem]
    failed: list[dict[str, Any]]


class CollectRequest(BaseModel):
    """触发自动采集请求。"""

    collector_type: str = Field(default="information_schema", max_length=32)
    mode: str = Field(default="FULL", max_length=16, pattern=r"^(FULL|INCREMENTAL)$")


class ScheduleRequest(BaseModel):
    """定时调度配置请求（US3）。"""

    cron: str = Field(max_length=100, description="定时调度 cron 表达式")
    mode: str = Field(default="FULL", max_length=16, pattern=r"^(FULL|INCREMENTAL)$")
