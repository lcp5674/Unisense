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
            self.source_type.value
            if self.source_type is not None and hasattr(self.source_type, "value")
            else str(self.source_type)
        )
        if source_type_value == "kafka":
            if "bootstrap_servers" not in cfg and "host" not in cfg:
                raise ValueError("kafka 的 connection_config 必须包含 bootstrap_servers 或 host")
        elif "host" not in cfg:
            raise ValueError("connection_config 必须包含 host 字段")
        return self


class DataSourceUpdateRequest(BaseModel):
    """数据源更新请求（PATCH 语义：全部字段可选，仅更新传入项）。

    安全约束：
    - ``source_id`` 不可变更（由路径参数唯一确定），变更连接配置前须走
      ``test-connection`` 预检（前端引导），后端不强制重新探活。
    - ``connection_config`` 传入时按类型校验必填项（与创建一致）。
    """

    name: str | None = Field(default=None, min_length=1, max_length=128)
    source_type: SourceType | None = None
    connection_config: dict[str, Any] | None = None
    domain: str | None = Field(default=None, max_length=64)
    cluster_id: str | None = Field(default=None, max_length=64)
    enabled: bool | None = Field(default=None, description="停用/启用（None 表示不修改）")

    @model_validator(mode="after")
    def _validate_connection_config(self) -> DataSourceUpdateRequest:
        """FR-020/P2-7: 仅当传入 connection_config 时按类型校验必填项。"""
        cfg = self.connection_config
        if cfg is None:
            return self
        if not isinstance(cfg, dict):
            raise ValueError("connection_config 必须是对象")
        # 仅更新连接配置而未指定类型时按通用规则校验（host 必填）
        source_type_value = self.source_type.value if self.source_type is not None else ""
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
            self.source_type.value
            if self.source_type is not None and hasattr(self.source_type, "value")
            else str(self.source_type)
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
    """数据源响应。

    安全边界：``connection_config`` 明文**仅详情接口**（``GET /data-sources/{id}``）
    返回，供前端编辑回显；列表接口保持 ``None``（脱敏，TD §13）。
    是否携带明文由 service 层 ``_to_source_response(include_config=...)`` 控制。
    """

    source_id: str
    name: str
    source_type: SourceType
    domain: str
    cluster_id: str | None = None
    coverage: float
    health_status: str
    connection_config_present: bool
    connection_config: dict[str, Any] | None = None
    schedule_cron: str | None = None
    collection_mode: str = "FULL"
    enabled: bool = True
    created_by: int | None = None
    created_at: Any = None
    updated_at: Any = None


class DataSourceListResponse(BaseModel):
    """数据源列表分页响应（P1-1：此前仅返回 20 条、total 被丢弃导致静默截断）。"""

    items: list[DataSourceResponse]
    total: int
    page: int
    page_size: int


class BatchSourceItem(BaseModel):
    """批量操作结果单项（207 语义，逐项标注成败原因）。"""

    source_id: str
    name: str | None = None
    ok: bool
    error_code: str | None = None
    message: str | None = None


class BatchSourceResult(BaseModel):
    """批量操作汇总结果（对齐 BulkDeprecateResult 的 207 模式）。

    ``succeeded`` 为成功项（含 name 便于前端提示）；``failed`` 为失败项
    （含 error_code + message）。调用方按「全成功=200、部分/全失败=207」判断。
    """

    succeeded: list[BatchSourceItem]
    failed: list[BatchSourceItem]


class BatchToggleRequest(BaseModel):
    """批量启用/停用请求。

    上限 200（对齐 BATCH_QUOTA_EXCEEDED 校验），空列表由 min_length 拒绝。
    """

    source_ids: list[str] = Field(min_length=1, max_length=200)
    enabled: bool


class BatchDeleteRequest(BaseModel):
    """批量删除请求（软删，逐条独立处理）。"""

    source_ids: list[str] = Field(min_length=1, max_length=200)


class DBCatalogCreateRequest(BaseModel):
    """元数据实体注册请求。"""

    model_config = ConfigDict(populate_by_name=True)

    source_id: str | None = Field(default=None, max_length=64)  # 可选——前端不填，由 URL 路径决定
    entity_name: str = Field(max_length=255)
    entity_type: EntityType = EntityTypeEnum.TABLE
    # validation_alias 仅用于「从 ORM/JSON 读取 schema_json 列」；输出固定用字段名
    # schema_def。若用 alias 会被 FastAPI 按 by_alias 序列化输出 schema_json，
    # 与前端 DBCatalog.schema_def 契约不一致（字段详情抽屉读不到列）。
    schema_def: dict[str, Any] = Field(validation_alias="schema_json")
    etl_sql: str | None = None
    owner_id: int | None = None


class DBCatalogResponse(BaseModel):
    """元数据实体响应。"""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    source_id: str
    entity_name: str
    entity_type: str
    # validation_alias（而非 alias）：ORM 输入读 schema_json 列，响应输出用
    # 字段名 schema_def——FastAPI by_alias 序列化下 alias 会输出 schema_json，
    # 与前端契约 schema_def 不一致（Catalogs 字段详情读不到字段）。
    schema_def: dict[str, Any] = Field(validation_alias="schema_json")
    etl_sql: str | None = None
    sensitivity_level: str
    owner_id: int | None = None
    upstream_signature: str
    content_signature: str | None = None
    schema_incomplete: bool = False
    # 表级业务描述（治理补全，TD §12.1；采集不覆盖）
    description: str | None = None
    description_source: str | None = None
    description_updated_by: int | None = None
    description_updated_at: Any = None
    # 数据源维度展示信息（默认 False/None——源被软删或不存在时 source_deleted=True）
    source_deleted: bool = False
    source_name: str | None = None


class DBCatalogListParams(BaseModel):
    """元数据列表过滤参数。"""

    source_id: str | None = None
    entity_type: str | None = None
    sensitivity_level: str | None = None
    database: str | None = Field(
        default=None, max_length=128, description="库名（entity_name 前缀过滤）"
    )
    keyword: str | None = Field(default=None, max_length=128)
    # 业务域过滤（经数据源继承）：db_catalog 无 domain 列，join DataSource 取 domain
    domain: str | None = Field(
        default=None, max_length=128, description="业务域（经数据源继承过滤）"
    )
    # 源状态过滤：active（仅活跃源）/ deleted（仅已删除源）/ all（全部，含已删除源）
    source_status: str | None = Field(default=None, pattern=r"^(active|deleted|all)$")
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


class DriftLogResponse(BaseModel):
    """Schema Drift 日志条目（P1-4 暴露给前端）。"""

    model_config = ConfigDict(from_attributes=True)

    source_id: str
    entity_name: str
    change_type: str
    before_signature: str | None = None
    after_signature: str
    before_schema: dict[str, Any] | None = None
    after_schema: dict[str, Any]
    diff_json: dict[str, Any] | None = None
    detected_at: str


class DriftLogListResponse(BaseModel):
    """Schema Drift 日志分页列表（P1-4）。"""

    items: list[DriftLogResponse]
    total: int
    page: int
    page_size: int


# ---- 字段描述推断 + 人工编辑 Schema ----


class ColumnDescriptionResponse(BaseModel):
    """字段描述响应。"""

    catalog_id: int
    column_name: str
    description: str
    source: str
    updated_by: int | None = None
    updated_at: Any = None


class InferDescriptionRequest(BaseModel):
    """推断单字段描述请求。"""

    entity_name: str = Field(max_length=256, description="表名（供 LLM 推断上下文）")
    column_type: str | None = Field(
        default=None, max_length=128, description="字段类型（供 LLM 推断上下文）"
    )


class InferDescriptionResponse(BaseModel):
    """推断单字段描述响应。"""

    column_name: str
    description: str
    source: str = "llm"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class InferBatchResponse(BaseModel):
    """批量推断描述响应。"""

    inferred: list[InferDescriptionResponse]
    skipped: list[str] = Field(
        default_factory=list, description="跳过的字段名（已有 manual/llm 描述）"
    )
    failed: list[str] = Field(default_factory=list, description="推断失败的字段名")


class UpdateDescriptionRequest(BaseModel):
    """人工编辑字段描述请求。"""

    description: str = Field(min_length=1, max_length=2000, description="新的描述文本")


class UpdateDescriptionResponse(BaseModel):
    """人工编辑字段描述响应。"""

    catalog_id: int
    column_name: str
    description: str
    source: str = "manual"
    updated_by: int | None = None
    updated_at: Any = None


# ---- 表级业务描述 + 描述缺失统计 Schema（TD §12.1）----


class TableDescriptionRequest(BaseModel):
    """人工编辑表级描述请求。"""

    description: str = Field(min_length=1, max_length=2000, description="表级业务描述")


class TableDescriptionResponse(BaseModel):
    """表级描述响应。"""

    catalog_id: int
    description: str
    source: str
    updated_by: int | None = None
    updated_at: Any = None


class InferTableDescriptionRequest(BaseModel):
    """LLM 推断表级描述请求（字段清单上下文，可空）。"""

    fields: list[dict[str, Any]] | None = Field(
        default=None, description="字段清单（空则服务端取 schema_json）"
    )


class InferTableDescriptionResponse(BaseModel):
    """LLM 推断表级描述响应。"""

    catalog_id: int
    description: str
    source: str = "llm"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class TableCoverageItem(BaseModel):
    """按表列描述覆盖明细。"""

    catalog_id: int
    entity_name: str
    source_id: str
    entity_type: str
    domain: str | None = None
    sensitivity_level: str
    table_desc: bool
    total_fields: int
    covered_fields: int
    missing_fields: int


class DescriptionCoverageResponse(BaseModel):
    """描述缺失统计响应。"""

    total_tables: int
    tables_with_desc: int
    tables_missing_desc: int
    total_fields: int
    fields_with_desc: int
    fields_missing_desc: int
    per_table: list[TableCoverageItem]
