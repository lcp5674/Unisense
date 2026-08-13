"""审计日志中文化（FR-16 增强：站在用户角度给出可读中文描述）。

审计记录原始字段（action / entity_type / detail_json）面向系统内部，
直接展示给业务用户不可读。本模块在 API 返回层 enrich：

- ``describe_audit``：将 action + entity_type + detail_json 翻译为一句中文描述。
- ``entity_label``：实体类型中文名。

设计约束：
1. 不修改 WORM 表（audit_log 只写不删，禁 UPDATE/DELETE），仅在查询层翻译。
2. 覆盖全仓所有 action 取值（SCREAMING_SNAKE 通用动作 + 点号风格业务动作），
   未命中时提供可读的兜底描述，绝不抛出异常。
3. detail_json 为自由 dict，翻译时仅提取已知字段做摘要，未知字段忽略。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 实体类型中文名（entity_type -> 中文）
# ---------------------------------------------------------------------------

ENTITY_LABELS: dict[str, str] = {
    "metric_definition": "指标定义",
    "metric": "指标",
    "metric_version": "指标版本",
    "metric_template": "指标模板",
    "template": "指标模板",
    "data_source": "数据源",
    "db_catalog": "数据表目录",
    "catalog": "资产目录",
    "conflict": "指标冲突",
    "lineage_edge": "血缘边",
    "lineage": "数据血缘",
    "grant": "授权",
    "role": "角色",
    "term": "业务术语",
    "glossary": "术语库",
    "dimension": "维度",
    "dimension_mapping": "维度映射",
    "dimension_member": "维度成员",
    "quality_rule": "质量规则",
    "quality_event": "质量事件",
    "quality_observation": "质量观测",
    "benchmark": "外部基准",
    "reconciliation_record": "对账记录",
    "reconciliation": "指标对账",
    "notification": "通知",
    "notification_event": "通知事件",
    "subscription": "通知订阅",
    "feedback": "用户反馈",
    "audit_log": "审计日志",
    "erasure_request": "被遗忘权申请",
    "classification": "敏感分级",
    "consumer_client": "消费接入方",
    "user": "用户",
    "organization": "组织",
    "dashboard": "运营看板",
    "snapshot": "数据快照",
    "metric_template_instantiation": "指标实例",
    "tracking_event": "埋点事件",
}

# ---------------------------------------------------------------------------
# 通用动作中文描述（SCREAMING_SNAKE 风格）
# 模板中的 {entity} 会被替换为实体中文名。
# ---------------------------------------------------------------------------

_ACTION_TEMPLATES: dict[str, str] = {
    "CREATE": "创建了{entity}",
    "LIST": "查询了{entity}列表",
    "READ": "查看了{entity}",
    "UPDATE": "更新了{entity}",
    "PUBLISH": "发布了{entity}",
    "DEPRECATE": "废弃了{entity}",
    "SUBMIT": "提交了{entity}评审",
    "APPROVE": "审核通过了{entity}",
    "REJECT": "驳回了{entity}",
    "DELETE": "删除了{entity}",
    "PROMOTE": "将{entity}全量发布",
    "ROLLBACK": "回滚了{entity}版本",
    "EMERGENCY_PUBLISH": "紧急发布了{entity}",
    "BATCH_REGISTER": "批量注册了{entity}",
    "CONFIRM_VERSION": "确认了{entity}版本",
    "REJECT_VERSION": "拒绝了{entity}版本",
    "EXTEND_VERSION": "延长了{entity}确认期限",
    "PII_REVIEW": "复核了{entity}的 PII 合规",
    "PII_SECONDARY_VALIDATION": "对{entity}做了 PII 二次校验",
    "CLASSIFICATION_RESCAN": "重扫了{entity}的敏感分级",
    "ROLE_CREATE": "创建了角色 {entity}",
    "GRANT_CREATE": "授予了{entity}访问权限",
    "GRANT_REVOKE": "收回了{entity}访问权限",
    "CONFLICT_ARBITRATE": "裁决了{entity}冲突",
    "CONFLICT_ESCALATE": "升级了{entity}冲突",
    "CONFLICT_CLOSE": "关闭了{entity}冲突",
    "REGISTER": "注册了{entity}",
    "COLLECT": "采集了{entity}元数据",
    "COLLECT_SCHEDULE": "为{entity}配置了定时采集",
    "BULK_DEPRECATE": "批量废弃了{entity}",
    "CHECK_CONNECTION": "测试了{entity}连接",
    "LINEAGE_PARSE": "解析并写入{entity}血缘",
    "LINEAGE_IMPACT_PREVIEW": "预览了{entity}变更影响",
    "IMPACT_PREVIEW": "预览了{entity}变更影响",
    "ASSET_ASSIGN_OWNER": "变更了{entity}责任人",
    "ASSET_RECLASSIFY": "重新分级了{entity}",
    "ASSET_BATCH_ASSIGN_OWNER": "批量变更了{entity}责任人",
    "ASSET_BATCH_RECLASSIFY": "批量重新分级了{entity}",
    "PII_ANONYMIZED": "对被遗忘权申请执行了 PII 匿名化",
    "LOGIN": "登录了系统",
    "LOGOUT": "退出了系统",
}

# ---------------------------------------------------------------------------
# 业务动作中文描述（点号风格，如 term.create / quality_event.detect）
# 键为完整 action 字符串。
# ---------------------------------------------------------------------------

_DOT_ACTION_TEMPLATES: dict[str, str] = {
    "term.create": "创建了{entity}",
    "term.submit": "提交了{entity}审核",
    "term.update": "更新了{entity}",
    "term.deprecate": "废弃了{entity}",
    "term.relation.create": "创建了{entity}关联",
    "glossary_conflict.resolve": "解决了{entity}术语冲突",
    "dimension.create": "创建了{entity}",
    "dimension.update": "更新了{entity}",
    "dimension.deprecate": "废弃了{entity}",
    "dimension.publish": "发布了{entity}",
    "dimension.mapping.create": "创建了{entity}映射",
    "dimension.member.create": "新增了{entity}成员",
    "dimension.metric.bind": "将指标绑定到{entity}",
    "reconciliation.submit": "提交了{entity}",
    "reconciliation.review": "复核了{entity}",
    "quality_rule.create": "创建了{entity}",
    "quality_rule.update": "更新了{entity}",
    "quality_rule.delete": "删除了{entity}",
    "quality_observation.record": "记录了{entity}观测值",
    "quality_event.detect": "检测到{entity}异常",
    "quality_event.ack": "确认了{entity}异常",
    "quality_event.resolve": "解决了{entity}异常",
    "quality_event.close": "关闭了{entity}异常",
    "quality_event.repair_confirm": "确认了{entity}修复",
    "benchmark.import": "导入了{entity}",
    "benchmark.bind": "将{entity}绑定到指标",
    "reconciliation.run": "执行了{entity}对账",
    "reconciliation.confirm": "确认了{entity}对账结果",
    "conflict.check": "检查了{entity}冲突",
    "ai.nl2sql": "用 AI 将自然语言转为 SQL",
    "consume.query": "执行了{entity}查询",
    "consume.api_client.create": "创建了{entity}接入方",
    "consume.version.confirm": "确认了{entity}版本",
    "consume.version.reject": "拒绝了{entity}版本",
    "template.create": "创建了{entity}模板",
    "template.instantiate": "从模板实例化了{entity}",
    "notify.publish": "发布了{entity}",
    "notify.mark_sent": "标记{entity}为已送达",
    "notify.mark_failed": "标记{entity}为投递失败",
    "feedback.submit": "提交了{entity}反馈",
    "feedback.status_update": "更新了{entity}反馈状态",
    "nps.submit": "提交了 NPS 评分",
    "erasure.execute": "执行了{entity}匿名化",
}

# ---------------------------------------------------------------------------
# detail_json 摘要函数：为已知动作从 detail 提取可读信息
# ---------------------------------------------------------------------------

_DETAIL_SUMMARIES: dict[str, str] = {
    "version": "版本",
    "metric_code": "指标",
    "decision": "结论",
    "sensitivity_level": "敏感级",
    "masking_policy": "脱敏策略",
    "reason": "原因",
    "note": "备注",
    "scanned": "扫描数",
    "registered": "注册数",
    "pii_registered": "PII 数",
    "failed_count": "失败数",
    "successor_code": "替代指标",
    "owner_id": "责任人",
    "domain": "域",
    "event_type": "事件类型",
    "status": "状态",
    "target_version": "目标版本",
    "grant_type": "授权类型",
    "expires_at": "到期时间",
    "table_edges": "表级血缘边",
    "field_edges": "字段级血缘边",
}


def entity_label(entity_type: str | None) -> str:
    """实体类型 -> 中文名；未知类型原样返回（兜底可读）。"""
    if not entity_type:
        return "记录"
    return ENTITY_LABELS.get(entity_type, entity_type)


def _summarize_detail(detail: dict[str, Any] | None) -> str | None:
    """从 detail_json 提取关键字段，拼接为可读摘要；无已知字段返回 None。"""
    if not detail:
        return None
    parts: list[str] = []
    for key, label in _DETAIL_SUMMARIES.items():
        if key in detail and detail[key] is not None:
            value = detail[key]
            if isinstance(value, (list, tuple)):
                value = ",".join(str(v) for v in value)
            parts.append(f"{label}={value}")
    return "；".join(parts) if parts else None


def describe_audit(
    action: str, entity_type: str | None = None, detail: dict[str, Any] | None = None
) -> str:
    """将审计记录翻译为一句可读中文描述。

    匹配顺序：点号业务动作 -> SCREAMING_SNAKE 通用动作 -> 兜底拆分。
    描述后追加 detail 摘要（如「版本=v2」），让用户无需看原始 JSON 即知变更内容。

    Args:
        action: 审计 action（如 ``PUBLISH`` / ``term.create``）。
        entity_type: 实体类型（如 ``metric_definition``）。
        detail: 操作详情 dict。

    Returns:
        中文描述句。任何输入均不抛异常。
    """
    entity = entity_label(entity_type)
    template = _DOT_ACTION_TEMPLATES.get(action) or _ACTION_TEMPLATES.get(action)
    if template:
        desc = template.format(entity=entity)
    else:
        # 兜底：点号风格取末段，SNAKE 风格转小写空格
        if "." in action:
            verb = action.split(".")[-1]
            desc = f"对{entity}执行了「{verb}」操作"
        else:
            desc = f"对{entity}执行了「{action.replace('_', ' ').lower()}」操作"
    summary = _summarize_detail(detail)
    if summary:
        desc = f"{desc}（{summary}）"
    return desc
