"""查询引擎（OLAP/MySQL 降级）DB 配置 Schemas（方案 A：前端可配置化）。

仿 ``services/llm/schemas.py`` 范式：保存载荷中密码/连接串留空表示「保持原值」
（编辑不覆盖已有密钥）；响应一律脱敏（仅返回 has_* 布尔标记与生效来源），
明文密钥仅经专有测试端点按需解密使用、绝不回传前端。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class QueryEngineConfigPayload(BaseModel):
    """查询引擎配置保存载荷（整行 upsert）。

    - olap_url / doris_host 二选一提供 OLAP 连接（olap_url 提供且未显式
      doris_host 时，由后端派生 host/port/database 落库）；
    - doris_password / mysql_fallback_url 留空表示保持原值（编辑时不覆盖）；
    - enabled=False 表示停用 DB 配置（回落环境变量）。
    """

    olap_url: str = Field("", max_length=512, description="OLAP 基础 URL（可选，含则派生连接参数）")
    doris_host: str = Field("", max_length=128, description="Doris FE 主机（显式直连优先）")
    doris_port: int = Field(8030, ge=1, le=65535, description="Doris FE HTTP 端口")
    doris_database: str = Field("", max_length=128, description="Doris 默认库（可空）")
    doris_user: str = Field("", max_length=64, description="Doris basic auth 用户名（可空）")
    doris_password: str = Field("", description="Doris 密码（留空表示保持原值）")
    mysql_fallback_url: str = Field(
        "", max_length=512, description="MySQL 降级引擎完整 URL（留空表示保持原值）"
    )
    enabled: bool = Field(True, description="是否启用 DB 配置（停用回落环境变量）")

    @field_validator("olap_url", "doris_host", "doris_database", "doris_user", mode="after")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


class QueryEngineConfigResponse(BaseModel):
    """DB 配置行响应（脱敏：不含任何明文密钥/URL）。"""

    id: int | None = None
    olap_url: str = ""
    doris_host: str = ""
    doris_port: int = 8030
    doris_database: str = ""
    doris_user: str = ""
    has_doris_password: bool = False
    has_mysql_fallback: bool = False
    enabled: bool = True
    updated_by: int | None = None
    updated_at: str | None = None


class QueryEngineEffectiveResponse(BaseModel):
    """当前生效配置视图（脱敏，供展示）。

    source: db=DB 行生效 / env=环境变量 / none=均未配置。
    olap_configured: OLAP 引擎是否已配置（host 非空）。
    mysql_fallback_configured: MySQL 降级引擎是否已配置（URL 非空）。
    note: 面向用户的配置状态说明（未配置时的指引）。
    """

    source: str = "none"
    olap_url: str = ""
    doris_host: str = ""
    doris_port: int = 8030
    doris_database: str = ""
    doris_user: str = ""
    has_doris_password: bool = False
    has_mysql_fallback: bool = False
    olap_configured: bool = False
    mysql_fallback_configured: bool = False
    updated_by: int | None = None
    updated_at: str | None = None
    note: str = ""


class QueryEngineViewResponse(BaseModel):
    """系统配置页「查询引擎」完整视图（DB 行 + 生效 + 可编辑标记）。"""

    row: QueryEngineConfigResponse | None = None
    effective: QueryEngineEffectiveResponse = Field(default_factory=QueryEngineEffectiveResponse)
    can_edit: bool = False


class QueryEngineTestRequest(BaseModel):
    """连通性测试请求。

    - engine: olap | mysql（要测试的引擎段）；
    - payload 为空：使用当前生效配置（已保存/环境）测试；
    - payload 非空：用载荷临时测试（不落库，密钥留空回落已保存/生效值）。
    """

    engine: str = Field("olap", description="测试引擎段：olap | mysql")
    payload: QueryEngineConfigPayload | None = Field(None, description="临时测试载荷（可空）")


class QueryEngineTestResult(BaseModel):
    """查询引擎连通性测试结果。

    olap 测试：TCP 探活 host:port（必测）+ 配置了 basic auth 用户名时附加
    Doris ``/api/bootstrap`` HTTP 鉴权校验；mysql 测试：真实建立连接执行
    ``SELECT 1``（最高保真）。
    """

    ok: bool
    engine: str = "olap"
    latency_ms: int = 0
    error: str = ""
    detail: dict[str, Any] | None = None
