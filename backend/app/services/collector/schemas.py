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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import EntityTypeEnum, SourceTypeEnum

SourceType = SourceTypeEnum
EntityType = EntityTypeEnum


def _validate_cron(value: str) -> str:
    """cron 表达式格式校验（croniter；非法返回 422，防调度静默失效）。

    P2-12: 此前写入口仅限长度，非法 cron 只在 worker 每分钟扫描时告警、
    调度静默永不触发——设置端应尽早反馈。
    """
    from croniter import croniter

    if not croniter.is_valid(value):
        raise ValueError(f"非法 cron 表达式: {value!r}")
    return value


# Hive Metastore 默认元数据库名（HMS backend 库默认即 ``hive``，用户可覆盖）
_HMS_DEFAULT_DATABASE = "hive"


def _ensure_hms_database(cfg: dict[str, Any], source_type_value: str) -> None:
    """hive_metastore 的 ``database`` 是纯连接凭据（HMS 元数据库名），缺省按 hive 填充。

    产品缺陷修复：``database`` 曾被当作"可选"，漏填时采集器直连 HMS backend 库
    报裸 ``pymysql 1046 'No database selected'``，用户只有等报错才知道缺库名。
    现统一默认填 ``hive``（Hive 元数据默认库名）：即便用户漏填也能直连 HMS 元库，
    前端表单同步标注默认值并允许覆盖；非该类型不改变任何字段。
    """
    if source_type_value == "hive_metastore" and not str(cfg.get("database") or "").strip():
        cfg["database"] = _HMS_DEFAULT_DATABASE


def _validate_sample_connection(cfg: dict[str, Any], source_type_value: str) -> None:
    """hive_metastore 可选采样连接（HiveServer2）校验：提供时 host 必填。

    采样连接与元数据连接分离——HMS 元数据库只含表结构、不含数据，采样需直连
    Hive 计算引擎执行 SELECT。该 host 已纳入 SSRF 校验
    （``ssrf.py _extract_hosts`` 递归提取 ``sample_connection.host``）。
    """
    if source_type_value != "hive_metastore":
        return
    sample_conn = cfg.get("sample_connection")
    if sample_conn is None:
        return
    if not isinstance(sample_conn, dict):
        raise ValueError("sample_connection 必须是对象")
    if not str(sample_conn.get("host") or "").strip():
        raise ValueError("sample_connection 必须包含 host 字段")


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
    databases: list[str] | None = Field(
        default=None,
        description="目标数据库列表（None=按连接配置/全部非系统库）",
    )
    collection_mode: str = Field(
        default="FULL",
        max_length=16,
        pattern=r"^(FULL|INCREMENTAL)$",
        description="默认采集模式：FULL 全量 / INCREMENTAL 增量（定时调度与手动采集默认按此模式）",
    )

    @model_validator(mode="after")
    def _validate_connection_config(self) -> DataSourceCreateRequest:
        """FR-020/P2-7: 按类型校验连接配置必填项 + 类型专属默认值。

        - kafka：需要 ``bootstrap_servers`` 或 ``host``（语义错位修复）；
        - 其余类型：必须包含 ``host``；
        - hive_metastore：``database``（HMS 元数据库名）缺省按 ``hive`` 填充；
        - hive_metastore：可选 ``sample_connection``（HiveServer2 采样连接，提供时
          host 必填）。
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
        _ensure_hms_database(cfg, source_type_value)
        _validate_sample_connection(cfg, source_type_value)
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
    # 治理字段（PATCH 语义：None 表示不修改）
    owner_id: int | None = Field(default=None, description="数据源负责人用户 ID")
    description: str | None = Field(default=None, max_length=2000, description="用途描述")
    include_patterns: list[str] | None = Field(
        default=None, description="表级包含白名单（fnmatch 风格，None=不修改）"
    )
    exclude_patterns: list[str] | None = Field(
        default=None, description="表级排除黑名单（fnmatch 风格，None=不修改）"
    )
    quota: dict[str, Any] | None = Field(
        default=None,
        description=(
            "资源配额（max_concurrency/max_scan_rows/sample_rows，None=不修改；"
            "整体覆盖语义，提交时须合并原有配额项）"
        ),
    )
    databases: list[str] | None = Field(
        default=None,
        description="目标数据库列表；[] 表示清空（采集全部库/单库配置），None 表示不修改",
    )
    collection_mode: str | None = Field(
        default=None,
        max_length=16,
        pattern=r"^(FULL|INCREMENTAL)$",
        description="默认采集模式（FULL/INCREMENTAL），None 表示不修改",
    )

    @model_validator(mode="after")
    def _validate_connection_config(self) -> DataSourceUpdateRequest:
        """FR-020/P2-7: 仅当传入 connection_config 时按类型校验必填项 + 类型专属默认值。"""
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
        _ensure_hms_database(cfg, source_type_value)
        _validate_sample_connection(cfg, source_type_value)
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
        _ensure_hms_database(cfg, source_type_value)
        _validate_sample_connection(cfg, source_type_value)
        return self


class TestConnectionResult(BaseModel):
    """连接测试结果。"""

    ok: bool
    source_type: str
    latency_ms: int | None = None
    error: str | None = None
    detail: dict[str, Any] | None = None


class ListTablesRequest(BaseModel):
    """枚举指定库下的表请求（明文配置不落库，与 list_databases 同构）。

    ``databases`` 为空时由连接器回退枚举全部非系统库；连接器不支持枚举表
    （如 Kafka）时返回空字典，前端隐藏表级选择区。
    """

    source_type: SourceType
    connection_config: dict[str, Any]
    databases: list[str] = Field(
        default_factory=list, description="要枚举表的库列表；空=全部非系统库"
    )

    @model_validator(mode="after")
    def _validate_connection_config(self) -> ListTablesRequest:
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
        _validate_sample_connection(cfg, source_type_value)
        return self


class ListTablesResult(BaseModel):
    """按库分组的表名列表（级联选表数据源）。"""

    tables: dict[str, list[str]]
    source_type: str


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
    databases: list[str] | None = None
    schedule_cron: str | None = None
    schedule_enabled: bool = True
    collection_mode: str = "FULL"
    enabled: bool = True
    created_by: int | None = None
    created_at: Any = None
    updated_at: Any = None
    # 治理字段
    owner_id: int | None = None
    description: str | None = None
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None
    health_metrics: dict[str, Any] | None = None
    degraded_since: Any = None
    # 资源配额（max_concurrency/max_scan_rows/sample_rows，PRD §4.2/§4.11.9）
    quota: dict[str, Any] = Field(default_factory=dict, description="资源配额")
    # 列表信号（list_sources 批量回填；详情不依赖）
    table_count: int | None = None
    pii_count: int | None = None
    last_collected_at: Any = None
    drift_count: int | None = None
    scanned_count: int | None = None
    failed_count: int | None = None


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


class BatchTestConnectionRequest(BaseModel):
    """批量探活请求（用已存连接配置逐条探活，207 语义）。

    ``probe_connection`` 必须已实现（registry 探测）；不存在的源标记
    NOT_FOUND，探活失败标记 PROBE_FAILED 并附错误。
    """

    source_ids: list[str] = Field(min_length=1, max_length=200)


class BatchScheduleRequest(BaseModel):
    """批量设置调度 cron 请求（统一覆盖 schedule_cron）。"""

    source_ids: list[str] = Field(min_length=1, max_length=200)
    schedule_cron: str = Field(min_length=1, max_length=100, description="cron 表达式")

    _validate_cron = field_validator("schedule_cron")(_validate_cron)


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
    # 元数据实体最近更新时间（采集刷新/治理补全时更新；资产目录「最近更新」列用）
    updated_at: Any = None
    # 数据源维度展示信息（默认 False/None——源被软删或不存在时 source_deleted=True）
    source_deleted: bool = False
    source_name: str | None = None
    # 生产化补充：业务域（经 data_source 继承）/ 责任人名（展示可读，非 id）
    domain: str | None = None
    owner_name: str | None = None


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
    # 责任人（Owner）ID 过滤（总览仪表 Owner 责任分布下钻用）
    owner_id: int | None = Field(default=None, description="责任人（Owner）ID 过滤")
    # 待复核敏感资产过滤（sensitivity IN (PII,CONFIDENTIAL) 且未合规复核）——
    # 资产地图 PII 合规卡下钻未复核敏感资产明细用
    pending_review: bool | None = Field(
        default=None, description="仅未复核敏感资产（PII/CONFIDENTIAL 且未复核）"
    )
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
    # 本次临时表级过滤（仅本次采集生效，不污染数据源配置；None=按数据源既有规则）
    include_patterns: list[str] | None = Field(
        default=None, description="本次临时白名单（fnmatch 风格），None=按数据源配置"
    )
    exclude_patterns: list[str] | None = Field(
        default=None, description="本次临时黑名单（fnmatch 风格），None=按数据源配置"
    )


class ScheduleRequest(BaseModel):
    """定时调度配置请求（US3）。

    ``mode`` 为 None 时保持数据源当前的 ``collection_mode`` 不变——调度只负责
    cron 与启停，采集模式由数据源自身的默认采集模式决定（编辑表单设置）。
    """

    cron: str = Field(max_length=100, description="定时调度 cron 表达式")
    mode: str | None = Field(
        default=None,
        max_length=16,
        pattern=r"^(FULL|INCREMENTAL)$",
        description="采集模式覆盖；None=保持数据源现有 collection_mode",
    )
    schedule_enabled: bool | None = Field(
        default=None, description="是否启用定时调度；None=保持当前状态"
    )

    _validate_cron = field_validator("cron")(_validate_cron)


class DriftLogResponse(BaseModel):
    """Schema Drift 日志条目（P1-4 暴露给前端）。"""

    model_config = ConfigDict(from_attributes=True)

    source_id: str
    entity_name: str
    change_type: str
    before_signature: str | None = None
    after_signature: str
    before_schema: dict[str, Any] | None = None
    after_schema: dict[str, Any] | None = None
    diff_json: dict[str, Any] | None = None
    detected_at: str


class DriftLogListResponse(BaseModel):
    """Schema Drift 日志分页列表（P1-4）。"""

    items: list[DriftLogResponse]
    total: int
    page: int
    page_size: int


class CollectionRunResponse(BaseModel):
    """采集运行历史条目（采集记录页主视图）。

    detail（detail_json 明细）仅详情接口返回，列表保持 None 控制体积。
    """

    id: int
    source_id: str
    source_name: str | None = None
    job_id: str | None = None
    trigger: str
    mode: str
    effective_mode: str | None = None
    status: str
    actor_id: int | None = None
    actor_name: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    scanned: int
    registered: int
    pii_registered: int
    failed_count: int
    drift_count: int
    deprecated_count: int
    coverage: float | None = None
    error: str | None = None
    detail: dict[str, Any] | None = None


class CollectionRunListResponse(BaseModel):
    """采集运行历史分页列表。"""

    items: list[CollectionRunResponse]
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
    force: bool = Field(
        default=False, description="强制重新推断；默认已存在 LLM 描述时短路返回"
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
    force: bool = Field(
        default=False, description="强制重新推断；默认已存在 LLM 描述时短路返回"
    )


class InferTableDescriptionResponse(BaseModel):
    """LLM 推断表级描述响应。"""

    catalog_id: int
    description: str
    source: str = "llm"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class TableCoverageItem(BaseModel):
    """按表列描述覆盖明细。

    四个下钻口径（字段覆盖率/缺失字段/缺表描述/全部资产）共用该结构，
    前端按口径各取所需字段差异化展示。
    """

    catalog_id: int
    entity_name: str
    source_id: str
    source_name: str | None = None  # 数据源名称（join DataSource.name）
    entity_type: str
    domain: str | None = None
    sensitivity_level: str
    table_desc: bool
    description: str | None = None  # 表级描述内容（缺表描述明细用）
    description_source: str | None = None  # manual/llm/schema
    owner_name: str | None = None  # 责任人中文名（缺表描述/全部资产明细用）
    total_fields: int
    covered_fields: int
    missing_fields: int
    missing_field_names: list[str] = []  # 缺失字段名列表（缺失字段明细用）
    updated_at: str | None = None  # 表更新时间（全部资产明细用）


class DescriptionCoverageResponse(BaseModel):
    """描述缺失统计响应。

    汇总指标（total_tables 等）为 SQL 端聚合；``per_table`` 为分页明细
    （page_size=None 时全量，向后兼容旧契约），分页元信息可选携带。
    """

    total_tables: int
    tables_with_desc: int
    tables_missing_desc: int
    total_fields: int
    fields_with_desc: int
    fields_missing_desc: int
    per_table: list[TableCoverageItem]
    per_table_total: int | None = None
    page: int | None = None
    page_size: int | None = None


# ---- 跨表批量 LLM 推断历史（服务端持久化，跨设备/团队可见） ----


class BatchLlmInferTaskItem(BaseModel):
    """批量任务清单中的一张表（对齐治理面板勾选项：字段缺失与表描述缺失可并存）。"""

    catalog_id: int = Field(..., description="目录实体 ID")
    entity_name: str = Field(default="", description="实体名（展示用）")
    missing_fields: int = Field(default=0, ge=0, description="缺失字段数（>0 则执行字段批量推断）")
    needs_table_desc: bool = Field(default=False, description="是否需生成表描述")


class BatchLlmInferTaskCreate(BaseModel):
    """创建跨表批量 LLM 推断后台任务（方案 B：arq 执行，进度落库跨页可见）。"""

    tasks: list[BatchLlmInferTaskItem] = Field(
        min_length=1, max_length=200, description="待推断表清单"
    )
    concurrency: int = Field(default=3, ge=1, le=8, description="有界并发表数")


class BatchInferHistoryTable(BaseModel):
    """批量历史中的一张表（catalog_id + entity_name，供一键重新勾选）。"""

    catalog_id: int
    entity_name: str


class BatchInferHistoryCreate(BaseModel):
    """写入一条批量推断历史。"""

    tables: list[BatchInferHistoryTable] = Field(min_length=1, description="本次会话涉及的表")
    done: int = Field(default=0, ge=0, description="成功表数")
    failed: int = Field(default=0, ge=0, description="失败表数")
    cancelled: int = Field(default=0, ge=0, description="取消表数")
    added: int = Field(default=0, ge=0, description="新增字段描述数")
    elapsed: int = Field(default=0, ge=0, description="总耗时（秒）")
    failed_tables: list[BatchInferHistoryTable] = Field(
        default_factory=list, description="失败表（一键重跑用）"
    )


class SqlQueryRequest(BaseModel):
    """数据源只读 SQL 查询请求（平台内部运维/分析用）。

    sql 仅允许单条只读语句（黑名单制：SELECT / SHOW / DESC / EXPLAIN / USE / HELP /
    CHECKSUM / CHECK 等非 DDL/DML 语句放行，服务层用 sqlglot 校验拒绝 DDL/DML/多语句/
    状态变更/行锁/写出/危险函数），limit 兜底返回行数上限。
    """

    sql: str = Field(
        min_length=1,
        max_length=8000,
        description="只读语句（SELECT / SHOW / DESC / EXPLAIN / USE 等，仅允许单条）",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=1_000_000,
        description="返回行数上限；不传/传 null 表示不限制（不追加 SQL LIMIT，"
        "由服务端安全护栏兜底防 OOM）",
    )


class SqlQueryResponse(BaseModel):
    """数据源只读 SQL 查询结果。"""

    columns: list[str] = Field(default_factory=list, description="结果列名（按首行 key 顺序）")
    rows: list[dict[str, Any]] = Field(default_factory=list, description="结果行（字典列表）")
    total: int = Field(default=0, description="实际返回行数")
    truncated: bool = Field(default=False, description="是否被 limit 截断")
    elapsed_ms: int = Field(default=0, description="源库执行耗时（毫秒）")
    current_db: str | None = Field(
        default=None, description="会话级当前库（USE 切换后生效，供前端展示）"
    )
    note: str | None = Field(default=None, description="提示信息（如 USE 切换成功）")


class BatchInferHistoryEntry(BaseModel):
    """批量推断历史单条记录（含操作人快照，团队治理动作可追溯）。"""

    id: int
    actor_id: int | None = None
    actor_name: str | None = None
    tables: list[BatchInferHistoryTable]
    done: int
    failed: int
    cancelled: int
    added: int
    elapsed: int
    failed_tables: list[BatchInferHistoryTable]
    created_at: str  # UTC ISO 时间（前端按上海时区展示）
