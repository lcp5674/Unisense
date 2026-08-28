"""审计日志中文化（FR-16 增强：站在用户角度给出可读中文描述）。

审计记录原始字段（action / entity_type / detail_json）面向系统内部，
直接展示给业务用户不可读。本模块在 API 返回层 enrich：

- ``describe_audit``：将 action + entity_type + detail_json 翻译为一句中文描述。
- ``entity_label``：实体类型中文名。

命名规范（2026-08 根治，对齐企业级审计标准）：
- 新产生动作统一为 ``{entity}.{verb}`` 点号小写原形（如 ``metric_definition.create``、
  ``data_source.batch_enable``、``auth.login``），entity 前缀与 entity_type 对齐，
  verb 取自统一动词词表（见 ``_VERB_TEMPLATES``）。
- ``_VERB_TEMPLATES`` 为动词模板表：任何 ``{prefix}.{verb}`` 只要 verb 命中即可翻译，
  无需为每个动作单独堆中文条目（此前 173 个动作逐条人工维护导致覆盖不全、命名漂移）。
- 历史兼容：旧 SCREAMING_SNAKE 通用动作（``CREATE``/``PUBLISH``…）与旧点号动作
  （``term.create``…）模板保留，查询层直接命中翻译，历史 WORM 记录仍可读。

设计约束：
1. 不修改 WORM 表（audit_log 只写不删，禁 UPDATE/DELETE），仅在查询层翻译。
2. 覆盖全仓所有 action 取值（新点号命名 + 旧 SCREAMING_SNAKE 通用动作 + 旧点号业务动作），
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
    "metric_description": "指标描述",
    "metric_term": "指标术语",
    "metric_dimension": "指标维度",
    "data_source": "数据源",
    "db_catalog": "数据表目录",
    "catalog": "资产目录",
    "column_description": "字段",
    "conflict": "指标冲突",
    "lineage_edge": "血缘边",
    "lineage": "数据血缘",
    "grant": "授权",
    "grants": "授权",
    "role": "角色",
    "term": "业务术语",
    "glossary": "术语库",
    "glossary_conflict": "术语冲突",
    "term_relation": "术语关联",
    "dimension": "维度",
    "dimension_mapping": "维度映射",
    "dimension_member": "维度成员",
    "quality_rule": "质量规则",
    "quality_event": "质量事件",
    "quality_observation": "质量观测",
    "benchmark": "外部基准",
    "external_benchmark": "外部基准",
    "reconciliation_record": "对账记录",
    "reconciliation": "指标对账",
    "notification": "通知",
    "notification_event": "通知事件",
    "event_log": "通知事件",
    "subscription": "通知订阅",
    "feedback": "用户反馈",
    "audit_log": "审计日志",
    "erasure_request": "被遗忘权申请",
    "classification": "敏感分级",
    "consumer_client": "消费接入方",
    "api_client": "消费接入方",
    "user": "用户",
    "organization": "组织",
    "dashboard": "运营看板",
    "snapshot": "数据快照",
    "metric_template_instantiation": "指标实例",
    "tracking_event": "埋点事件",
    "sensitive_rule": "敏感规则",
    "asset_pii": "资产 PII",
    "system_dict": "参照数据",
    "dict_item": "参照数据项",
    "subject_domain": "主题域",
    "llm_config": "AI 配置",
    "nl_query": "AI 问数",
    "quickbi_report": "QuickBI 报表",
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
    "TEST_CONNECTION": "测试了{entity}连接",
    "BATCH_ENABLE": "批量启用了{entity}",
    "BATCH_DISABLE": "批量停用了{entity}",
    "BATCH_DELETE": "批量删除了{entity}",
    "BATCH_PROBE": "批量探活了{entity}连接",
    "BATCH_SCHEDULE": "批量配置了{entity}调度",
    "REFRESH": "刷新了{entity}元数据",
    "COLLECT_ASYNC": "异步采集了{entity}元数据",
    "COLLECT_NOW": "立即采集了{entity}元数据",
    "INFER_DESCRIPTION": "推断{entity}描述",
    "INFER_DESCRIPTIONS_BATCH": "批量推断{entity}描述",
    "UPDATE_DESCRIPTION": "更新了{entity}描述",
    "UPDATE_TABLE_DESCRIPTION": "更新了{entity}表级描述",
    "INFER_TABLE_DESCRIPTION": "推断{entity}表级描述",
    "LINEAGE_PARSE": "解析并写入{entity}",
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
    "term.relation.create": "创建了{entity}",
    "glossary_conflict.resolve": "解决了{entity}",
    "dimension.create": "创建了{entity}",
    "dimension.update": "更新了{entity}",
    "dimension.deprecate": "废弃了{entity}",
    "dimension.publish": "发布了{entity}",
    "dimension.mapping.create": "创建了{entity}",
    "dimension.member.create": "新增了{entity}",
    "dimension.metric.bind": "将指标绑定到{entity}",
    "reconciliation.submit": "提交了{entity}",
    "reconciliation.review": "复核了{entity}",
    "quality_rule.create": "创建了{entity}",
    "quality_rule.update": "更新了{entity}",
    "quality_rule.delete": "删除了{entity}",
    "quality_observation.record": "记录了{entity}",
    "quality_event.detect": "检测到{entity}异常",
    "quality_event.ack": "确认了{entity}异常",
    "quality_event.resolve": "解决了{entity}异常",
    "quality_event.close": "关闭了{entity}异常",
    "quality_event.repair_confirm": "确认了{entity}修复",
    "benchmark.import": "导入了{entity}",
    "benchmark.bind": "将{entity}绑定到指标",
    "reconciliation.run": "执行了{entity}",
    "reconciliation.confirm": "确认了{entity}结果",
    "conflict.check": "检查了{entity}",
    "conflict.auto_detect": "创建指标时自动检测到{entity}冲突",
    "catalog.batch_infer_history": "记录了{entity}批量描述推断",
    "catalog.batch_infer_history_clear": "清空了{entity}的批量描述推断历史",
    "ai.nl2sql": "用 AI 将自然语言转为 SQL",
    "consume.query": "执行了{entity}查询",
    "consume.api_client.create": "创建了{entity}",
    "consume.version.confirm": "确认了{entity}版本",
    "consume.version.reject": "拒绝了{entity}版本",
    "template.create": "创建了{entity}",
    "template.instantiate": "从模板实例化了{entity}",
    "notify.publish": "发布了{entity}",
    "notify.mark_sent": "标记{entity}为已送达",
    "notify.mark_failed": "标记{entity}为投递失败",
    "feedback.submit": "提交了{entity}",
    "feedback.status_update": "更新了{entity}反馈状态",
    "nps.submit": "提交了 NPS 评分",
    "erasure.execute": "执行了{entity}匿名化",
    "conflict.force_close": "强制关闭了{entity}",
    "metric_definition.sql_infer_eval_run": "运行了{entity}评测",
    "metric_definition.sql_infer_eval_sample_create": "创建了{entity}评测样本",
    "metric_definition.sql_infer_eval_sample_update": "更新了{entity}评测样本",
    "metric_definition.sql_infer_eval_sample_delete": "删除了{entity}评测样本",
    "admin_key.rotate": "轮换了{entity}密钥",
    "admin_key.migrate": "迁移了{entity}密钥",
    "admin_key.migrate_dry_run": "预演了{entity}密钥迁移",
}

# ---------------------------------------------------------------------------
# 统一动词模板表（新命名规范核心，2026-08 根治）
# 新产生动作统一为 {entity}.{verb}，verb 取自下表；任何 {prefix}.{verb}
# 只要 verb 命中即可翻译，无需逐条维护完整 action 字符串。
# entity 前缀通常与 entity_type 对齐，翻译实体名优先用 entity_type 参数。
# ---------------------------------------------------------------------------

_VERB_TEMPLATES: dict[str, str] = {
    # 基础 CRUD / 读
    "create": "创建了{entity}",
    "update": "更新了{entity}",
    "delete": "删除了{entity}",
    "read": "查看了{entity}",
    "list": "查询了{entity}列表",
    "export": "导出了{entity}",
    # 审核流
    "submit": "提交了{entity}评审",
    "approve": "审核通过了{entity}",
    "reject": "驳回了{entity}",
    "clarify": "澄清了{entity}（质疑回复）",
    "review": "复核了{entity}",
    "confirm": "确认了{entity}",
    "resubmit": "重新提交了{entity}评审",
    "confirm_version": "确认了{entity}版本",
    "reject_version": "拒绝了{entity}版本",
    "extend_version": "延长了{entity}确认期限",
    # 生命周期 / 发布
    "publish": "发布了{entity}",
    "deprecate": "废弃了{entity}",
    "promote": "将{entity}全量发布",
    "rollback": "回滚了{entity}版本",
    "restore": "恢复了{entity}",
    "purge": "彻底删除了{entity}",
    "recover": "恢复了{entity}",
    "activate": "激活了{entity}",
    "reactivate": "重新启用了{entity}",
    "deactivate": "停用了{entity}",
    "emergency_publish": "紧急发布了{entity}",
    "emergency_review": "紧急复核了{entity}",
    "promote_version": "将{entity}版本全量发布",
    "recover_source_dropped": "恢复了{entity}（数据源已恢复）",
    "confirm_deprecate_dropped": "确认退役了{entity}",
    "mark_source_dropped": "标记{entity}数据源下线",
    # 采集 / 连接 / 调度
    "register": "注册了{entity}",
    "collect": "采集了{entity}元数据",
    "collect_now": "立即采集了{entity}元数据",
    "collect_async": "异步采集了{entity}元数据",
    "refresh": "刷新了{entity}元数据",
    "schedule": "配置了{entity}定时采集",
    "enable": "启用了{entity}",
    "disable": "停用了{entity}",
    "test": "测试了{entity}",
    "test_connection": "测试了{entity}连接",
    "cancel_job": "取消了{entity}任务",
    "list_databases": "查询了{entity}数据库列表",
    "list_tables": "查询了{entity}表列表",
    "check": "检查了{entity}",
    "check_connection": "检查了{entity}连接",
    "probe": "探活了{entity}连接",
    "sync": "同步了{entity}",
    "update_term": "更新了{entity}术语",
    "bind_dimension": "将{entity}绑定到维度",
    "unbind_dimension": "将{entity}从维度解绑",
    "update_status": "更新了{entity}状态",
    "query_internal": "执行了{entity}内部查询",
    "bulk_deprecate": "批量废弃了{entity}",
    "collect_schedule": "配置了{entity}定时采集",
    "submit_nps": "提交了 NPS 评分",
    # 描述 / LLM
    "infer": "推断{entity}",
    "infer_description": "推断{entity}描述",
    "infer_descriptions": "批量推断{entity}描述",
    "update_description": "更新了{entity}描述",
    "refine": "完善了{entity}",
    # 关系 / 归属
    "bind": "绑定了{entity}",
    "unbind": "解绑了{entity}",
    "assign": "变更了{entity}责任人",
    "assign_owner": "变更了{entity}责任人",
    "reassign": "变更了{entity}责任人",
    "reclassify": "重新分级了{entity}",
    "create_relation": "创建了{entity}",
    # 治理 / 安全 / 合规
    "grant": "授予了{entity}访问权限",
    "revoke": "收回了{entity}访问权限",
    "review_pii": "复核了{entity}的 PII 合规",
    "secondary_validate_pii": "对{entity}做了 PII 二次校验",
    "rescan": "重扫了{entity}的敏感分级",
    "override_pii": "覆盖了{entity}的 PII 标记",
    "remove_pii_override": "移除了{entity}的 PII 覆盖",
    "set_masking": "配置了{entity}脱敏策略",
    "set_retention": "配置了{entity}保留期",
    "apply_pii_template": "应用了{entity} PII 模板",
    "anonymize": "对{entity}执行了 PII 匿名化",
    "execute": "执行了{entity}",
    "reveal": "查看了{entity}密钥",
    "reveal_secret": "查看了{entity}密钥",
    "pii_view": "查看了{entity} PII 信息",
    "pii_list": "查询了{entity} PII 列表",
    "pii_templates": "查看了{entity} PII 模板",
    "pii_export": "导出了{entity} PII 清单",
    "resolve_conflict": "解决了{entity}",
    "rescan_classification": "重扫了{entity}的敏感分级",
    "delete_all": "删除了全部{entity}",
    # 冲突 / 任务
    "resolve": "解决了{entity}",
    "escalate": "升级了{entity}",
    "close": "关闭了{entity}",
    "reopen": "重新打开了{entity}",
    "arbitrate": "裁决了{entity}",
    # 血缘
    "parse": "解析并写入{entity}",
    "parse_batch": "批量解析并写入{entity}",
    "scan": "扫描了{entity}",
    "preview_impact": "预览了{entity}变更影响",
    "preview_values": "预览了{entity}维度值",
    "add_edge": "新增了{entity}边",
    "delete_edge": "删除了{entity}边",
    "sync_consumer": "同步了{entity}消费关系",
    "confirm_stale": "确认了{entity}为失效",
    "restore_stale": "恢复了{entity}",
    # 用户 / 组织 / 认证
    "change_password": "修改了{entity}密码",
    "reset_password": "重置了{entity}密码",
    "update_permissions": "更新了{entity}权限",
    "reset_permissions": "重置了{entity}权限",
    "login": "登录了系统",
    "logout": "退出了系统",
    "login_failed": "登录失败",
    # 通知
    "mark_read": "标记{entity}为已读",
    "mark_all_read": "标记全部{entity}为已读",
    "mark_sent": "标记{entity}为已送达",
    "mark_failed": "标记{entity}为投递失败",
    "retry_delivery": "重试了{entity}投递",
    "mark_handled": "标记{entity}为已处理",
    # 质量 / 对账 / 模板
    "record": "记录了{entity}",
    "detect": "检测到{entity}异常",
    "ack": "确认了{entity}异常",
    "confirm_repair": "确认了{entity}修复",
    "import": "导入了{entity}",
    "run": "执行了{entity}",
    "query": "执行了{entity}查询",
    "instantiate": "从模板实例化了{entity}",
    "set_active": "启用了{entity}",
    "get_ticket": "获取了{entity}票据",
    "update_defaults": "更新了{entity}默认配置",
    "notify_unknown": "通知了{entity}未知词",
    "reject_unknown": "驳回了{entity}未知词",
    # 批量动作（前缀 batch_）
    "batch_create": "批量创建了{entity}",
    "batch_register": "批量注册了{entity}",
    "batch_import": "批量导入了{entity}",
    "csv_import": "通过 CSV 批量导入了{entity}",
    "sql_batch_parse": "解析了{entity}候选（SQL 批量）",
    "sql_batch_register": "从 SQL 批量注册了{entity}",
    "batch_submit": "批量提交了{entity}评审",
    "batch_approve": "批量审核通过了{entity}",
    "batch_reject": "批量驳回了{entity}",
    "batch_deprecate": "批量废弃了{entity}",
    "batch_publish": "批量发布了{entity}",
    "batch_enable": "批量启用了{entity}",
    "batch_disable": "批量停用了{entity}",
    "batch_delete": "批量删除了{entity}",
    "batch_probe": "批量探活了{entity}连接",
    "batch_schedule": "批量配置了{entity}调度",
    "batch_update_status": "批量更新了{entity}状态",
    "batch_update_confidence": "批量更新了{entity}置信度",
    "batch_assign_owner": "批量变更了{entity}责任人",
    "batch_reclassify": "批量重新分级了{entity}",
    "batch_grant": "批量授予了{entity}访问权限",
    "batch_revoke": "批量收回了{entity}访问权限",
    # 批量动作部分成功 / 全失败（对齐 _batch_audit_action 动态后缀）
    "batch_submit_partial": "部分提交{entity}评审成功（存在失败项）",
    "batch_submit_failed": "批量提交{entity}评审失败",
    "batch_approve_partial": "部分审核通过{entity}（存在失败项）",
    "batch_approve_failed": "批量审核{entity}失败",
    "batch_reject_partial": "部分驳回{entity}（存在失败项）",
    "batch_reject_failed": "批量驳回{entity}失败",
    "batch_deprecate_partial": "部分废弃{entity}（存在失败项）",
    "batch_deprecate_failed": "批量废弃{entity}失败",
    "batch_import_partial": "部分导入{entity}成功（存在失败项）",
    "batch_import_failed": "批量导入{entity}失败",
    "csv_import_partial": "部分 CSV 导入{entity}成功（存在失败项）",
    "csv_import_failed": "CSV 批量导入{entity}失败",
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
    # 数据源/描述相关（采集目录三模块审计摘要，TD §12.1）
    "name": "名称",
    "source_type": "类型",
    "mode": "模式",
    "cron": "调度",
    "latency_ms": "耗时",
    "ok": "结果",
    "config_changed": "配置变更",
    "succeeded": "成功",
    "failed": "失败",
    "inferred": "推断",
    "skipped": "跳过",
    "confidence": "置信度",
    "source": "来源",
    "job_id": "任务ID",
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

    匹配顺序：旧点号业务动作 -> 旧 SCREAMING_SNAKE 通用动作 -> 新命名动词模板
    （``{prefix}.{verb}`` 拆末段查 ``_VERB_TEMPLATES``，任意实体前缀均可命中）
    -> 兜底拆分。描述后追加 detail 摘要（如「版本=v2」）。

    Args:
        action: 审计 action（如 ``PUBLISH`` / ``term.create`` / ``metric_definition.create``）。
        entity_type: 实体类型（如 ``metric_definition``）。
        detail: 操作详情 dict。

    Returns:
        中文描述句。任何输入均不抛异常。
    """
    entity = entity_label(entity_type)
    template = _DOT_ACTION_TEMPLATES.get(action) or _ACTION_TEMPLATES.get(action)
    if not template and "." in action:
        # 新命名 {prefix}.{verb}：取末段动词查统一动词模板（忽略大小写，兼容旧命名残值）
        verb = action.rsplit(".", 1)[-1].lower()
        template = _VERB_TEMPLATES.get(verb)
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
