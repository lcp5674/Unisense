"""采集领域请求/响应 Schema（对齐 TD §12.1 / DEV_GUIDE §8b.1）。

注意：
- ``DataSourceResponse`` 不暴露 ``connection_config`` 明文，仅以
  ``connection_config_present`` 标记是否存在，满足凭据脱敏（TD §13）。
- ``schema_json`` 与 Pydantic ``BaseModel.schema_json()`` 冲突，字段名用
  ``schema_def``，并以 alias="schema_json" 与模型列对齐（populate_by_name）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SourceType = Literal["mysql", "postgres", "hive", "doris", "starrocks", "kafka"]
EntityType = Literal["TABLE", "VIEW"]


class DataSourceCreateRequest(BaseModel):
    """数据源注册请求。"""

    source_id: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    source_type: SourceType
    connection_config: dict[str, Any]
    domain: str = Field(max_length=64)
    cluster_id: str | None = Field(default=None, max_length=64)


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
    created_by: int | None = None
    created_at: Any = None
    updated_at: Any = None


class DBCatalogCreateRequest(BaseModel):
    """元数据实体注册请求。"""

    model_config = ConfigDict(populate_by_name=True)

    source_id: str = Field(max_length=64)
    entity_name: str = Field(max_length=255)
    entity_type: EntityType = "TABLE"
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
