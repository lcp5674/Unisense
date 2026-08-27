"""LLM 平台配置 Schemas（对齐 TD §12.7 / FR-14 扩展，多实例轮询路由）。

提供前端「系统配置」页配置多个 OpenAI 协议兼容 LLM 实例的载荷与响应结构。
API Key 前端提交时为明文（HTTPS 传输），后端经 SecretManager 加密后落库，
响应一律脱敏（仅返回 has_api_key 布尔标记）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class LlmConfigPayload(BaseModel):
    """LLM 实例保存载荷（创建 / 更新共用）。

    api_key 可选：为空表示保持原密钥不变（编辑时不覆盖已有密钥）。
    """

    name: str = Field("", max_length=64, description="实例名称（如 主用/备用）")
    provider: str = Field("custom", max_length=32, description="提供商标识")
    base_url: str = Field("", max_length=256, description="OpenAI 兼容接口基础 URL")
    model: str = Field("", max_length=128, description="模型名称")
    api_key: str = Field("", description="API Key（留空表示保持原密钥）")
    timeout: int = Field(30, ge=1, le=300, description="请求超时秒数")
    enabled: bool = Field(False, description="是否启用该实例（仅启用参与路由）")
    priority: int = Field(0, ge=0, le=100, description="路由优先级（小者优先，0 最高）")

    @field_validator("base_url", "model", mode="after")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


class LlmConfigResponse(BaseModel):
    """LLM 实例响应（脱敏：不含明文 API Key）。"""

    id: int | None = None
    name: str = ""
    provider: str = ""
    base_url: str = ""
    model: str = ""
    has_api_key: bool = False
    timeout: int = 30
    enabled: bool = False
    priority: int = 0
    source: str = "none"  # db | env | none
    can_edit: bool = False
    updated_by: int | None = None
    updated_at: str | None = None

    @classmethod
    def build(
        cls,
        *,
        id: int | None = None,  # noqa: A002 - Pydantic 字段名，模型亦用 id
        name: str = "",
        provider: str = "",
        base_url: str = "",
        model: str = "",
        has_api_key: bool = False,
        timeout: int = 30,
        enabled: bool = False,
        priority: int = 0,
        source: str = "none",
        can_edit: bool = False,
        updated_by: int | None = None,
        updated_at: str | None = None,
    ) -> LlmConfigResponse:
        return cls(
            id=id,
            name=name,
            provider=provider,
            base_url=base_url,
            model=model,
            has_api_key=has_api_key,
            timeout=timeout,
            enabled=enabled,
            priority=priority,
            source=source,
            can_edit=can_edit,
            updated_by=updated_by,
            updated_at=updated_at,
        )


class LlmConfigListResponse(BaseModel):
    """LLM 配置列表响应（多实例 + 路由策略 + 生效配置）。"""

    items: list[LlmConfigResponse] = Field(default_factory=list, description="全部实例（脱敏）")
    strategy: str = Field("round_robin", description="路由策略（轮询 + 故障转移）")
    effective: dict[str, Any] = Field(
        default_factory=dict, description="当前生效配置（供展示，含 source）"
    )
    can_edit: bool = False


class LlmConfigTestRequest(BaseModel):
    """连通性测试请求（二选一）：

    - instance_id: 测试已保存实例（用其落库密钥）；
    - 或直接给 base_url/model/api_key/timeout 临时测试（不落库，api_key 留空回落已保存密钥）。
    """

    instance_id: int | None = Field(None, description="已保存实例 ID")
    base_url: str = Field("", max_length=256, description="OpenAI 兼容接口基础 URL")
    model: str = Field("", max_length=128, description="模型名称")
    api_key: str = Field("", description="API Key（留空回落已保存/环境密钥）")
    timeout: int = Field(30, ge=1, le=300, description="请求超时秒数")


class LlmConfigSecretResponse(BaseModel):
    """LLM 实例密钥响应（仅按需解密返回，用于编辑回显）。

    列表与常规 GET 一律脱敏（仅 has_api_key）；只有管理员在编辑弹窗点
    「显示密钥」时，才通过 GET /ai/config/{id}/secret 按需取明文，且每次
    查看都写审计日志。
    """

    id: int
    api_key: str = ""


class LlmConfigTestResult(BaseModel):
    """LLM 连通性测试结果（方案 B'：GET /models 快速验证 + 真实 chat 探测）。

    ok=True 表示地址可连、鉴权通过、模型列表可读（GET /models），且模型能真实
    产出推理结果（POST /chat/completions，极小 max_tokens）——让「测试通过」
    等价于「可推理」，避免仅 /models 可达但模型实际不可用（如 400 Model
    unavailable）的假绿；``models`` 为 /models 返回的可用模型 ID 列表，
    ``chat`` 标记真实推理探测是否通过（None=未执行，如 GET /models 已失败）。
    """

    ok: bool
    latency_ms: int = 0
    model: str = ""
    error: str = ""
    detail: dict[str, Any] | None = None
    models: list[str] | None = None
    chat: bool | None = Field(
        None, description="真实推理探测是否通过（None=未执行/无法判定）"
    )


class LlmModelsRequest(BaseModel):
    """一键获取模型列表请求（二选一）：

    - instance_id: 使用已保存实例（用其落库密钥）；
    - 或直接给 base_url/api_key/timeout（api_key 留空回落已保存/环境密钥）。

    provider 用于已知平台（火山方舟/腾讯混元等兼容网关未实现 /models）的
    内置常用模型目录兜底；custom 或未知 provider 不做目录兜底。
    """

    instance_id: int | None = Field(None, description="已保存实例 ID")
    base_url: str = Field("", max_length=256, description="OpenAI 兼容接口基础 URL")
    api_key: str = Field("", description="API Key（留空回落已保存/环境密钥）")
    timeout: int = Field(30, ge=1, le=300, description="请求超时秒数")
    provider: str = Field("", max_length=32, description="提供商标识（目录兜底用）")


class LlmModelsResult(BaseModel):
    """一键获取模型列表结果。

    supported=False 表示网关不支持 ``GET /models`` 端点（或请求失败），
    调用方应回退为手动输入模型名；models 为空列表时同理。
    ``source`` 区分模型来源：live=实时拉取 / catalog=内置常用模型目录
    （火山方舟/腾讯混元等兼容网关未实现 /models 时的兜底）。
    """

    models: list[str] = Field(default_factory=list, description="可用模型名列表")
    supported: bool = Field(False, description="网关是否支持 /models 端点")
    error: str = Field("", description="失败原因（supported=False 时）")
    latency_ms: int = Field(0, description="请求耗时（毫秒）")
    source: str = Field("live", description="模型来源：live=实时拉取 / catalog=内置常用模型目录")
    note: str = Field("", description="来源说明（catalog 时的提示文案）")
