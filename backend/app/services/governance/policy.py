"""治理策略核心：PDP 决策 + PII 规则识别（TD §12.5 / FR-11）。

本模块为**纯函数**，不依赖 DB / 网络 / 时间以外的任何外部资源，便于单测穷举边界。

两块能力：

1. **PDP（Policy Decision Point）**：``decide()`` 依据主体属性（角色/本域/授权列表）
   与资源属性（域/指标/敏感级/合规复核标记）产出允许或拒绝，**默认拒绝（fail-closed）**。
2. **敏感识别规则引擎**：``detect_pii_columns()`` / ``infer_sensitivity()``，
   对应 TD §12.5.4 分级重扫；规则字典可配置，误报由治理人工修正后回填。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.models.governance import GrantType, RoleName, SensitivityLevel

# ---------------------------------------------------------------- 敏感识别规则

#: 规则字典：``(规则名, 字段名正则, 取值样本正则 | None, 置信度)``。
#: 字段名命中即判 PII；若同时提供样本正则且样本命中，置信度提升。
PII_RULES: tuple[tuple[str, str, str | None, float], ...] = (
    ("id_card", r"(id_?card|identity_?no|shenfen|sfz)", r"^\d{17}[\dXx]$", 0.95),
    ("phone", r"(phone|mobile|tel|telephone)", r"^1[3-9]\d{9}$", 0.9),
    ("email", r"(email|mail_?addr)", r"^[^@\s]+@[^@\s]+\.[^@\s]+$", 0.9),
    ("bank_card", r"(bank_?card|card_?no|account_?no)", r"^\d{16,19}$", 0.9),
    ("real_name", r"(real_?name|user_?name|cust_?name|full_?name)", None, 0.7),
    ("address", r"(addr|address|location_detail)", None, 0.7),
    ("passport", r"(passport)", None, 0.85),
    ("gps", r"(lat|lng|longitude|latitude|geo_?point)", None, 0.6),
)

#: 规则版本号，随规则字典变更递增，落库到 ``classification.model_version``。
RULES_VERSION = "rules-v1"

#: PII 命中判定阈值：低于该置信度仅记录不升级敏感级。
PII_CONFIDENCE_THRESHOLD = 0.7


@dataclass(frozen=True, slots=True)
class PiiHit:
    """一次 PII 字段命中。"""

    column: str
    rule: str
    confidence: float
    matched_by: str  # name | name+sample


def detect_pii_columns(schema_json: dict[str, Any]) -> list[PiiHit]:
    """从表结构中识别 PII 字段。

    Args:
        schema_json: ``db_catalog.schema_json``，期望形如
            ``{"columns": [{"name": "phone", "type": "varchar", "sample": "13800000000"}]}``。
            兼容 ``{"fields": [...]}`` 与纯字符串列表。

    Returns:
        命中列表，按置信度倒序；无命中返回空列表。
    """
    columns = _extract_columns(schema_json)
    hits: list[PiiHit] = []
    for col in columns:
        name = str(col.get("name", "")).strip()
        if not name:
            continue
        sample = str(col.get("sample", "") or "")
        lowered = name.lower()
        for rule, name_re, sample_re, base_conf in PII_RULES:
            if not re.search(name_re, lowered):
                continue
            confidence = base_conf
            matched_by = "name"
            if sample_re and sample and re.match(sample_re, sample):
                confidence = min(1.0, base_conf + 0.05)
                matched_by = "name+sample"
            hits.append(
                PiiHit(column=name, rule=rule, confidence=confidence, matched_by=matched_by)
            )
            break
    hits.sort(key=lambda h: (-h.confidence, h.column))
    return hits


def _extract_columns(schema_json: dict[str, Any]) -> list[dict[str, Any]]:
    """归一化列定义结构，容忍多种上游写法。"""
    raw: Any = None
    for key in ("columns", "fields"):
        if isinstance(schema_json, dict) and isinstance(schema_json.get(key), list):
            raw = schema_json[key]
            break
    if raw is None:
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            normalized.append({"name": item})
        elif isinstance(item, dict):
            normalized.append(item)
    return normalized


def infer_sensitivity(hits: list[PiiHit], current: str | None = None) -> SensitivityLevel:
    """根据 PII 命中推断敏感级别。

    Args:
        hits: ``detect_pii_columns`` 的结果。
        current: 现有敏感级别，用于「只升不降」保护（人工上调过的不被规则引擎降级）。

    Returns:
        推断出的敏感级别。
    """
    inferred = SensitivityLevel.INTERNAL
    if any(h.confidence >= PII_CONFIDENCE_THRESHOLD for h in hits):
        inferred = SensitivityLevel.PII
    elif hits:
        inferred = SensitivityLevel.CONFIDENTIAL
    if current in (SensitivityLevel.PII.value, SensitivityLevel.CONFIDENTIAL.value):
        # 人工/历史已判定为高敏，规则引擎不得自动降级（避免误放开）
        order = [
            SensitivityLevel.PUBLIC,
            SensitivityLevel.INTERNAL,
            SensitivityLevel.CONFIDENTIAL,
            SensitivityLevel.PII,
        ]
        cur = SensitivityLevel(current)
        if order.index(cur) > order.index(inferred):
            return cur
    return inferred


def masking_for(level: SensitivityLevel | str) -> str:
    """敏感级 → 默认脱敏策略。"""
    mapping = {
        SensitivityLevel.PUBLIC.value: "none",
        SensitivityLevel.INTERNAL.value: "none",
        SensitivityLevel.CONFIDENTIAL.value: "mask",
        SensitivityLevel.PII.value: "hash",
        SensitivityLevel.UNKNOWN.value: "mask",
    }
    key = level.value if isinstance(level, SensitivityLevel) else str(level)
    return mapping.get(key, "mask")


# ------------------------------------------------------------------- PDP 决策

#: 角色 → 本域内允许的动作集合（跨域须另有 grants 授权）。
#:
#: 这是**默认基线**：``role_permission`` 表（RBAC 可配置化，见 GovernanceService
#: ``list_role_permissions`` / ``set_role_permissions``）可对其做覆盖，``decide()``
#: 通过 ``role_actions`` 参数接收合并后的映射；未配置覆盖的角色沿用本默认值。
ROLE_ACTIONS: dict[str, frozenset[str]] = {
    RoleName.PLATFORM_ADMIN.value: frozenset({"read", "write", "approve", "export", "review"}),
    RoleName.DOMAIN_ADMIN.value: frozenset({"read", "write", "approve", "export"}),
    RoleName.METRIC_OWNER.value: frozenset({"read", "write"}),
    RoleName.REVIEWER.value: frozenset({"read", "approve"}),
    RoleName.COMPLIANCE_OFFICER.value: frozenset({"read", "review"}),
    # analyst：只读消费者角色（对齐 User.role 枚举 0001 迁移）
    RoleName.ANALYST.value: frozenset({"read"}),
    RoleName.VIEWER.value: frozenset({"read"}),
}

#: PDP 可配置动作白名单：角色权限点配置只能从该集合内勾选（杜绝写入未知动作）。
CONFIGURABLE_ACTIONS: frozenset[str] = frozenset({"read", "write", "approve", "export", "review"})

#: 禁止通过配置收窄的平台级保护角色（权限点配置对这些角色不生效，防自锁/提权失控）。
#: platform_admin 在 ``decide`` 中本就硬编码跨域直通，不受 role_actions 影响。
PROTECTED_ROLES: frozenset[str] = frozenset({RoleName.PLATFORM_ADMIN.value})

#: 授权类型 → 允许的动作集合。
GRANT_TYPE_ACTIONS: dict[str, frozenset[str]] = {
    GrantType.READ.value: frozenset({"read", "export"}),
    GrantType.WRITE.value: frozenset({"write"}),
    GrantType.READ_WRITE.value: frozenset({"read", "write", "export"}),
}

# --------------------------------------------------------------- UI 权限点注册表
#
# 资源级动作（read/write/approve/export/review）用于 PDP 指标资源判定；
# 此处新增的 ``模块:功能`` UI 权限点用于**前端按模块/组件/按钮级管控**
# （路由守卫 / 菜单显隐 / 页面 Tab / 按钮），由 ``GET /action-registry`` 暴露给
# 角色管理做**可视化配置**，经 ``role_permission`` 表覆盖（``ROLE_UI_ACTIONS`` 为默认基线）。

#: UI 权限点注册表：``动作键 -> {module, label, description}``。
#: 键命名 ``模块:功能[:子项]``；``module`` 用于前端分组渲染，``label`` 为中文名，
#: ``description`` 为配置时的悬停说明。
UI_ACTION_REGISTRY: dict[str, dict[str, str]] = {
    # ---- 总览 / 待办 / 通知
    "dashboard:view": {"module": "总览", "label": "查看总览", "description": "访问总览仪表盘"},
    "todo:view": {"module": "总览", "label": "查看待办", "description": "访问待办列表"},
    "notifications:view": {"module": "总览", "label": "查看通知", "description": "访问通知中心"},
    "favorites:view": {"module": "总览", "label": "查看收藏", "description": "访问我的收藏"},
    # ---- 指标
    "catalog:view": {"module": "指标", "label": "查看指标目录", "description": "访问指标目录列表"},
    "compare:view": {"module": "指标", "label": "指标对比", "description": "访问指标对比页"},
    "templates:view": {"module": "指标", "label": "查看指标模板", "description": "访问指标模板页"},
    "metric:create": {"module": "指标", "label": "创建指标", "description": "新增指标（含口径定义）"},  # noqa: E501
    "metric:edit": {"module": "指标", "label": "编辑指标", "description": "修改指标口径 / 描述"},
    "metric:approve": {"module": "指标", "label": "审批指标", "description": "通过 / 驳回指标审核"},
    "metric:deprecate": {"module": "指标", "label": "废弃指标", "description": "下线并废弃指标"},
    "metric:export": {"module": "指标", "label": "导出指标", "description": "导出指标清单"},
    "metric:review": {"module": "指标", "label": "评审指标", "description": "参与指标口径评审"},
    "metric:emergency-publish": {"module": "指标", "label": "紧急发布", "description": "跳过常规流程紧急发布"},  # noqa: E501
    "metric:rollback": {"module": "指标", "label": "指标回滚", "description": "回滚已发布指标版本"},
    "metric:import": {"module": "指标", "label": "批量导入", "description": "批量注册 / 导入指标"},
    # ---- 资产地图 / 血缘
    "assetmap:view": {"module": "资产地图", "label": "查看资产地图", "description": "访问资产地图"},
    "assetmap:edit": {"module": "资产地图", "label": "编辑资产", "description": "修改资产描述 / 元数据"},  # noqa: E501
    "assetmap:export": {"module": "资产地图", "label": "导出资产", "description": "导出资产清单"},
    "lineage:view": {"module": "资产地图", "label": "查看血缘", "description": "访问血缘图谱"},
    # ---- 质量 / 冲突
    "quality:view": {"module": "质量中心", "label": "查看质量中心", "description": "访问质量中心"},
    "quality:run-check": {"module": "质量中心", "label": "执行质量校验", "description": "运行数据质量规则"},  # noqa: E501
    "quality:config-rule": {"module": "质量中心", "label": "配置质量规则", "description": "新增 / 编辑质量规则"},  # noqa: E501
    "review:view": {"module": "质量中心", "label": "查看冲突仲裁", "description": "访问口径冲突仲裁页"},  # noqa: E501
    # ---- 查询 / AI / 维度 / 术语
    "query:view": {"module": "分析", "label": "指标查询", "description": "访问指标查询工作台"},
    "ai:view": {"module": "分析", "label": "AI 助手", "description": "访问 AI 助手"},
    "dimensions:view": {"module": "分析", "label": "查看维度", "description": "访问维度管理"},
    "glossary:view": {"module": "分析", "label": "查看术语表", "description": "访问术语表"},
    # ---- 数据源 / 采集
    "data-sources:view": {"module": "采集", "label": "查看数据源", "description": "访问数据源管理"},
    "data-source:create": {"module": "采集", "label": "新增数据源", "description": "创建数据源连接"},  # noqa: E501
    "data-source:edit": {"module": "采集", "label": "编辑数据源", "description": "修改数据源连接配置"},  # noqa: E501
    "data-source:delete": {"module": "采集", "label": "删除数据源", "description": "删除数据源连接"},  # noqa: E501
    "data-source:test-connection": {"module": "采集", "label": "测试连接", "description": "测试数据源连通性"},  # noqa: E501
    "data-source:collect": {"module": "采集", "label": "执行采集", "description": "触发采集任务"},
    "catalogs:view": {"module": "采集", "label": "查看采集目录", "description": "访问采集目录"},
    "collection-tasks:view": {"module": "采集", "label": "查看采集任务", "description": "访问采集任务中心"},  # noqa: E501
    "collection-history:view": {"module": "采集", "label": "查看采集记录", "description": "访问采集历史"},  # noqa: E501
    "catalog:deprecate": {"module": "采集", "label": "废弃目录", "description": "废弃采集到的目录表"},  # noqa: E501
    "catalog:edit-description": {"module": "采集", "label": "编辑字段描述", "description": "编辑目录字段描述"},  # noqa: E501
    # ---- 用户 / 组织
    "users:view": {"module": "账号", "label": "查看用户管理", "description": "访问用户管理页"},
    "user:create": {"module": "账号", "label": "创建用户", "description": "新增用户账号"},
    "user:edit": {"module": "账号", "label": "编辑用户", "description": "修改用户信息 / 角色"},
    "user:disable": {"module": "账号", "label": "启停用户", "description": "启用 / 禁用用户"},
    "user:reset-password": {"module": "账号", "label": "重置密码", "description": "重置用户密码"},
    "user:batch-status": {"module": "账号", "label": "批量启停", "description": "批量启用 / 禁用用户"},  # noqa: E501
    "organizations:view": {"module": "账号", "label": "查看组织管理", "description": "访问组织管理页"},  # noqa: E501
    "org:create": {"module": "账号", "label": "创建组织", "description": "新增组织（租户）"},
    "org:edit": {"module": "账号", "label": "编辑组织", "description": "修改组织信息 / 状态"},
    "org:disable": {"module": "账号", "label": "启停组织", "description": "启用 / 停用组织"},
    # ---- 权限治理 / 审计 / 配置
    "governance:view": {"module": "治理", "label": "查看权限治理", "description": "访问权限治理页"},
    "grant:create": {"module": "治理", "label": "创建授权", "description": "域授权 / 指标白名单授权"},  # noqa: E501
    "grant:revoke": {"module": "治理", "label": "回收授权", "description": "回收既有授权"},
    "grant:export": {"module": "治理", "label": "导出授权", "description": "导出授权清单"},
    "role:create": {"module": "治理", "label": "创建角色", "description": "新建自定义角色"},
    "role:edit": {"module": "治理", "label": "配置角色权限", "description": "配置角色权限点"},
    "role:delete": {"module": "治理", "label": "删除角色", "description": "删除自定义角色"},
    "pii:review": {"module": "治理", "label": "PII 合规复核", "description": "复核敏感指标"},
    "pii:validate": {"module": "治理", "label": "PII 二次校验", "description": "字段级脱敏校验"},
    "classification:rescan": {"module": "治理", "label": "分级重扫", "description": "重算资产敏感级别"},  # noqa: E501
    "erasure:execute": {"module": "治理", "label": "执行被遗忘权", "description": "数据主体删除请求"},  # noqa: E501
    "audit:view": {"module": "治理", "label": "查看审计日志", "description": "访问审计日志"},
    "audit:export": {"module": "治理", "label": "导出审计", "description": "导出审计日志"},
    "domains:view": {"module": "治理", "label": "查看主题域", "description": "访问主题域管理"},
    "domain:create": {"module": "治理", "label": "管理主题域", "description": "新增 / 启停主题域"},
    "dicts:view": {"module": "治理", "label": "查看系统字典", "description": "访问系统字典"},
    "dict:create": {"module": "治理", "label": "管理字典", "description": "新增 / 编辑字典项"},
    "api-clients:view": {"module": "治理", "label": "查看接入方", "description": "访问 API 客户端"},
    "system-config:view": {"module": "治理", "label": "查看系统配置", "description": "访问系统配置"},  # noqa: E501
    "system-config:edit": {"module": "治理", "label": "编辑系统配置", "description": "修改系统配置（LLM Key 等）"},  # noqa: E501
    "observability:view": {"module": "治理", "label": "查看可观测", "description": "访问可观测性"},
    "tracking-stats:view": {"module": "治理", "label": "查看埋点统计", "description": "访问埋点统计"},  # noqa: E501
    "feedback:view": {"module": "总览", "label": "查看用户反馈", "description": "访问用户反馈"},
    "guide:view": {"module": "总览", "label": "查看使用指南", "description": "访问使用指南"},
}

#: 角色 → UI 权限点默认基线（与 ``ROLE_ACTIONS`` 正交：前者供前端 UI 管控，
#: 后者供 PDP 资源级判定；``role_permission`` 表可对两者分别覆盖）。
ROLE_UI_ACTIONS: dict[str, frozenset[str]] = {
    RoleName.PLATFORM_ADMIN.value: frozenset(UI_ACTION_REGISTRY.keys()),
    RoleName.DOMAIN_ADMIN.value: frozenset(
        {
            "dashboard:view", "todo:view", "notifications:view", "favorites:view",
            "catalog:view", "compare:view", "templates:view", "metric:create", "metric:edit",
            "metric:approve", "metric:deprecate", "metric:export", "metric:review",
            "metric:emergency-publish", "metric:rollback", "metric:import",
            "assetmap:view", "assetmap:edit", "assetmap:export", "lineage:view",
            "quality:view", "quality:run-check", "quality:config-rule", "review:view",
            "query:view", "ai:view", "dimensions:view", "glossary:view",
            "data-sources:view", "data-source:create", "data-source:edit",
            "data-source:delete", "data-source:test-connection", "data-source:collect",
            "catalogs:view", "collection-tasks:view", "collection-history:view",
            "catalog:deprecate", "catalog:edit-description",
            "organizations:view", "org:create", "org:edit", "org:disable",
            "governance:view", "grant:create", "grant:revoke", "grant:export",
            "audit:view", "domains:view", "domain:create", "dicts:view", "dict:create",
            "api-clients:view", "system-config:view", "observability:view",
            "tracking-stats:view", "feedback:view", "guide:view",
        }
    ),
    RoleName.METRIC_OWNER.value: frozenset(
        {
            "dashboard:view", "todo:view", "notifications:view", "favorites:view",
            "catalog:view", "compare:view", "metric:create", "metric:edit",
            "metric:deprecate", "metric:export", "metric:review",
            "assetmap:view", "lineage:view", "quality:view", "review:view",
            "query:view", "ai:view", "dimensions:view", "glossary:view",
            "data-sources:view", "catalogs:view", "collection-tasks:view",
            "collection-history:view", "feedback:view", "guide:view",
        }
    ),
    RoleName.REVIEWER.value: frozenset(
        {
            "dashboard:view", "todo:view", "notifications:view", "favorites:view",
            "catalog:view", "compare:view", "metric:review", "metric:approve",
            "quality:view", "review:view", "query:view", "ai:view",
            "dimensions:view", "glossary:view", "feedback:view", "guide:view",
        }
    ),
    RoleName.COMPLIANCE_OFFICER.value: frozenset(
        {
            "dashboard:view", "todo:view", "notifications:view", "favorites:view",
            "catalog:view", "compare:view", "assetmap:view", "lineage:view",
            "quality:view", "query:view", "ai:view", "dimensions:view", "glossary:view",
            "governance:view", "pii:review", "pii:validate", "classification:rescan",
            "erasure:execute", "audit:view", "audit:export",
            "feedback:view", "guide:view",
        }
    ),
    RoleName.ANALYST.value: frozenset(
        {
            "dashboard:view", "todo:view", "notifications:view", "favorites:view",
            "catalog:view", "compare:view", "assetmap:view", "lineage:view",
            "quality:view", "query:view", "ai:view", "dimensions:view", "glossary:view",
            "feedback:view", "guide:view",
        }
    ),
    RoleName.VIEWER.value: frozenset(
        {
            "dashboard:view", "todo:view", "notifications:view", "favorites:view",
            "catalog:view", "compare:view", "quality:view", "query:view",
            "dimensions:view", "glossary:view", "guide:view",
        }
    ),
}

#: UI 权限点可配置白名单：角色权限点可视化配置只能从该集合勾选。
CONFIGURABLE_UI_ACTIONS: frozenset[str] = frozenset(UI_ACTION_REGISTRY.keys())


def is_ui_action(action: str) -> bool:
    """判断动作是否为 UI 权限点（``模块:功能`` 形态），而非资源级动词。"""
    return action in CONFIGURABLE_UI_ACTIONS


def all_configurable_actions() -> frozenset[str]:
    """全部可配置权限点 = 资源级动词 ∪ UI 权限点（角色权限点配置的合法集合）。"""
    return CONFIGURABLE_ACTIONS | CONFIGURABLE_UI_ACTIONS


@dataclass(frozen=True, slots=True)
class Subject:
    """决策主体属性（TD §12.5.6：user_id / role / domain / grants）。"""

    user_id: int
    role: str
    domain: str | None = None
    grants: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Resource:
    """决策客体属性。"""

    domain: str | None = None
    metric_code: str | None = None
    sensitivity: str = SensitivityLevel.INTERNAL.value
    compliance_reviewed: bool = True
    owner_id: int | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """决策结果。

    Attributes:
        allow: 是否放行。
        reason: 判定依据（审计可读）。
        error_code: 拒绝时的错误码（放行为空串）。
        restricted: 行级权限标记。restricted 授权命中时为 True；consume 内部查询
            路径已落地基础安全兜底（结果行按命中授权的 metric_whitelist 过滤），
            完整 RLS（维度值级过滤 + 脱敏）为二期范围（TD §12.5）。
        masking: 建议脱敏策略。
    """

    allow: bool
    reason: str
    error_code: str = ""
    restricted: bool = False
    masking: str = "none"


def decide(
    subject: Subject,
    action: str,
    resource: Resource,
    role_actions: dict[str, frozenset[str]] | None = None,
) -> Decision:
    """权限决策入口，默认拒绝（fail-closed）。

    判定顺序：动作合法性 → PII 合规门禁 → platform_admin 直通 → 本域角色 → 跨域授权 → 拒绝。

    Args:
        subject: 主体属性。
        action: 动作，取值 read/write/approve/export/review。
        resource: 客体属性。
        role_actions: 可配置的角色动作映射（RBAC 配置化，来自 ``role_permission`` 表
            与默认基线的合并）。缺省使用模块级 ``ROLE_ACTIONS``，保持纯函数与既有调用兼容。

    Returns:
        决策结果，``allow=False`` 时 ``error_code`` 给出拒绝原因码。
    """
    act = (action or "").strip().lower()
    if act not in CONFIGURABLE_ACTIONS:
        return Decision(False, f"未知动作 {action!r}", error_code="VALIDATION_ERROR")

    masking = masking_for(resource.sensitivity)

    # PII 合规门禁（COMPL-1）：未经合规复核的 PII 资产，除合规官复核动作外一律拒绝
    if resource.sensitivity == SensitivityLevel.PII.value and not resource.compliance_reviewed:
        is_compliance_review = act == "review" and subject.role == RoleName.COMPLIANCE_OFFICER.value
        if not is_compliance_review:
            return Decision(
                False,
                "PII 资产未通过合规复核，禁止访问",
                error_code="FORBIDDEN_PII",
                masking=masking,
            )

    effective_actions = role_actions if role_actions is not None else ROLE_ACTIONS
    role_actions_for_role = effective_actions.get(subject.role, frozenset())

    if subject.role == RoleName.PLATFORM_ADMIN.value:
        return Decision(True, "platform_admin 跨域运维直通", masking=masking)

    same_domain = (
        subject.domain is not None
        and resource.domain is not None
        and subject.domain == resource.domain
    )
    if same_domain and act in role_actions_for_role:
        owner_mismatch = (
            subject.role == RoleName.METRIC_OWNER.value
            and act == "write"
            and resource.owner_id is not None
            and resource.owner_id != subject.user_id
        )
        if owner_mismatch:
            return Decision(
                False,
                "metric_owner 仅可编辑本人负责的指标",
                error_code="FORBIDDEN",
                masking=masking,
            )
        return Decision(True, f"本域角色 {subject.role} 允许 {act}", masking=masking)

    grant = _match_grant(subject.grants, act, resource)
    if grant is not None:
        return Decision(
            True,
            f"命中授权 grant#{grant.get('id', '?')}（{grant.get('grant_type')}）",
            restricted=bool(grant.get("row_level")),
            masking=masking,
        )

    target = f"{resource.domain or '-'}/{resource.metric_code or '-'}"
    return Decision(
        False,
        f"角色 {subject.role} 对 {target} 无 {act} 权限",
        error_code="FORBIDDEN",
        masking=masking,
    )


def _match_grant(
    grants: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    action: str,
    resource: Resource,
) -> dict[str, Any] | None:
    """在授权列表中查找可覆盖本次访问的 ACTIVE 授权。"""
    now = datetime.now(UTC)
    for g in grants:
        if str(g.get("status", "ACTIVE")) != "ACTIVE":
            continue
        if not is_grant_effective(g.get("expires_at"), now=now):
            continue
        if action not in GRANT_TYPE_ACTIONS.get(str(g.get("grant_type", "")), frozenset()):
            continue
        whitelist = g.get("metric_whitelist") or []
        if whitelist:
            if resource.metric_code and resource.metric_code in whitelist:
                return g
            continue
        if g.get("domain") and resource.domain and g["domain"] == resource.domain:
            return g
    return None


def is_grant_effective(expires_at: Any, now: datetime | None = None) -> bool:
    """判断授权是否仍在有效期内（``None`` 视为永久有效）。"""
    if expires_at is None:
        return True
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    if not isinstance(expires_at, datetime):
        return False
    ref = now or datetime.now(UTC)
    deadline: datetime = expires_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return bool(deadline > ref)
