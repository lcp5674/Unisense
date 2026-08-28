"""OpenAPI 契约展示层中文化。

在 FastAPI 自动生成的 OpenAPI schema 上做文档展示本地化：
- ``info.title/description`` → 中文品牌化说明（含认证/版本指引）。
- ``tags`` → 33 个业务分组的中文名称（``x-displayName``，Swagger UI 渲染中文分组名）与分组描述。
- ``operation.summary`` → 英文 summary 映射为中文；未命中的保留原文（不破坏契约）。

设计取舍：
中文化集中在 OpenAPI 生成层而非逐个路由文件改 summary——
- 不侵入 30+ 路由文件（避免并行会话冲突与巨型 diff）。
- 单一事实来源集中在本文档模块，新增接口时在此补充映射即可。
- 不影响 API 契约本身（``summary``/``tags`` 仅是文档展示字段）。
"""

from __future__ import annotations

# ---- info 中文说明 ----
_INFO_DESCRIPTION = """Unisense 指标语义中台 — 统一指标语义管理平台的开放 API。

## 认证
除健康检查等公开端点外，接口均需携带 Bearer Token（`Authorization: Bearer <token>`）。
通过 `POST /api/v1/auth/login` 获取令牌；`GET /api/v1/auth/me` 可校验当前会话。

## 版本
当前版本 v0.1.0，接口路径统一以 `/api/v1` 为前缀。

## 说明
本规范由后端路由自动生成（`/openapi.json`），接口变更后自动同步，无需人工维护。
"""

# ---- 业务分组中文映射：tag -> (中文名, 分组描述) ----
# 中文名经 x-displayName 注入，Swagger UI 左侧分组显示中文；description 展示在分组下方。
TAGS_ZH: dict[str, tuple[str, str]] = {
    "health": ("健康检查", "存活/就绪探针、依赖健康态与 Prometheus 指标"),
    "auth": ("认证", "登录认证、令牌签发与当前会话"),
    "audit": ("审计", "操作审计日志查询与导出"),
    "metric-definitions": ("指标定义", "指标注册、口径定义与全生命周期管理"),
    "metric-definitions-stats": ("指标统计", "指标注册统计与复用分析"),
    "collector-source": ("数据源管理", "数据源接入、连通性检测与采集调度"),
    "collector-catalog": ("采集目录", "采集元数据目录与描述治理"),
    "collector-run": ("采集运行", "采集任务与运行日志"),
    "lineage": ("血缘分析", "字段血缘解析、影响分析与健康度"),
    "conflict": ("指标冲突", "同名指标冲突检测与仲裁"),
    "governance": ("数据治理", "敏感数据分级、PII 复核与治理操作"),
    "quality": ("数据质量", "质量规则、观测与事件闭环"),
    "consume": ("指标消费", "指标查询与消费开放 API"),
    "glossary": ("术语管理", "业务术语治理与同义词管理"),
    "dimension": ("维度管理", "维度、成员与维度映射管理"),
    "measure_catalog": ("逻辑度量", "逻辑度量目录与口径管理"),
    "metric_mount": ("挂载实体", "指标挂载实体（OneData 挂载层）管理"),
    "notify": ("通知中心", "消息通知、订阅与事件日志"),
    "observability": ("可观测性", "用户反馈、NPS 与运营指标"),
    "组织管理": ("组织管理", "组织架构与机构管理"),
    "preferences": ("用户偏好", "个人偏好设置"),
    "assetmap": ("资产地图", "数据资产地图、分级与责任人"),
    "recommend": ("智能推荐", "指标/术语关联推荐"),
    "search": ("全局搜索", "资产检索与索引"),
    "ai": ("AI 能力", "AI 问数（NL2SQL）与 LLM 配置"),
    "tracking": ("埋点统计", "前端埋点上报与统计"),
    "semantics": ("语义服务", "指标语义批量解析与推断"),
    "主题域管理": ("主题域管理", "业务主题域与层级管理"),
    "系统字典管理": ("系统字典管理", "系统字典与枚举值管理"),
    "敏感规则配置": ("敏感规则配置", "敏感数据识别规则管理"),
    "feature-flags": ("特性开关", "功能开关管理"),
    "admin/keys": ("密钥管理", "加密密钥轮换与状态"),
    "users": ("用户管理", "用户、角色与权限管理"),
}

# ---- 英文 summary → 中文（覆盖全部路由英文 summary）----
# 未命中映射的 summary 保留原文（新接口未翻译时不影响契约与可用性）。
SUMMARY_ZH: dict[str, str] = {
    "Ack Event": "确认质量事件",
    "Add Favorite": "添加收藏",
    "Add Manual Edge": "新增手工血缘边",
    "Api Metrics": "API 指标",
    "Apply Pii Template": "应用 PII 模板",
    "Approve Term": "审核通过术语",
    "Arbitrate Conflict": "仲裁冲突",
    "Assign Owner": "指定责任人",
    "Batch Assign Owner": "批量指定责任人",
    "Batch Delete Data Sources": "批量删除数据源",
    "Batch Grants": "批量授权",
    "Batch Reclassify": "批量重分级",
    "Batch Schedule Data Sources": "批量调度数据源",
    "Batch Set Rule Confidence": "批量设置规则置信度",
    "Batch Set Rule Status": "批量设置规则状态",
    "Batch Set User Status": "批量设置用户状态",
    "Batch Test Data Sources": "批量测试数据源",
    "Batch Toggle Data Sources": "批量启停数据源",
    "Bind Benchmark": "绑定质量基准",
    "Bind Metric Dimension": "绑定指标维度",
    "Bulk Deprecate": "批量废弃",
    "Cancel Collection Job": "取消采集任务",
    "Catalog Summary": "目录汇总",
    "Change My Password": "修改我的密码",
    "Check Conflict": "冲突预检",
    "Check Permission": "校验权限",
    "Check Source Connection": "检测数据源连通性",
    "Clarify Feedback": "反馈追问",
    "Classification Rescan": "分级重扫描",
    "Classification Summary": "分级汇总",
    "Clear Batch Infer History": "清空批量推断历史",
    "Close Conflict": "关闭冲突",
    "Close Event": "关闭质量事件",
    "Collect Now": "立即采集",
    "Collect Source": "采集数据源",
    "Collect Source Async": "异步采集数据源",
    "Collection Run Summary": "采集运行汇总",
    "Confirm Reconciliation": "确认对账结果",
    "Confirm Repair": "确认修复",
    "Confirm Stale": "确认失效边",
    "Confirm Version": "确认指标版本",
    "Coverage": "血缘覆盖统计",
    "Coverage Broken": "断链覆盖统计",
    "Coverage Orphans": "孤儿覆盖统计",
    "Create Batch Infer History": "记录批量推断历史",
    "Create Client": "创建消费客户端",
    "Create Data Source": "新建数据源",
    "Create Dimension": "新建维度",
    "Create Event": "上报埋点事件",
    "Create Grant": "新建授权",
    "Create Llm Config": "新建 LLM 配置",
    "Create Mapping": "新建维度映射",
    "Create Measure": "新建逻辑度量",
    "Create Member": "新建维度成员",
    "Create Mount": "新建挂载实体",
    "Create Organization": "新建组织",
    "Create Relation": "新建术语关系",
    "Create Role": "新建角色",
    "Create Rule": "新建质量规则",
    "Create Term": "新建术语",
    "Create User": "新建用户",
    "Del Favorite": "删除收藏",
    "Delete All Notifications": "清空全部通知",
    "Delete Data Source": "删除数据源",
    "Delete Edges By Node": "按节点删除血缘边",
    "Delete Llm Config": "删除 LLM 配置",
    "Delete Mapping": "删除维度映射",
    "Delete Member": "删除维度成员",
    "Delete Mount": "删除挂载实体",
    "Delete Notification": "删除通知",
    "Delete Preference": "删除偏好设置",
    "Delete Role": "删除角色",
    "Delete Rule": "删除质量规则",
    "Delete Single Edge": "删除单条血缘边",
    "Deprecate Dimension": "废弃维度",
    "Deprecate Measure": "废弃逻辑度量",
    "Deprecate Member": "废弃维度成员",
    "Deprecate Term": "废弃术语",
    "Detect": "触发质量检测",
    "Dry Run": "查询预演（Dry Run）",
    "Dry Run Batch Grants": "批量授权预演",
    "Edge Detail": "血缘边详情",
    "Escalate Conflict": "冲突升级",
    "Export Audit Logs": "导出审计日志",
    "Export Pii Csv": "导出 PII 资产 CSV",
    "Export Tables": "导出表清单",
    "Fetch Llm Models": "拉取 LLM 模型列表",
    "Force Close Conflict": "强制关闭冲突",
    "Get Catalog Detail": "目录详情",
    "Get Collection Job": "采集任务详情",
    "Get Collection Run": "采集运行详情",
    "Get Collection Run Logs": "采集运行日志",
    "Get Data Source": "数据源详情",
    "Get Description Coverage": "描述覆盖统计",
    "Get Dimension": "维度详情",
    "Get Entity Detail": "资产实体详情",
    "Get Favorite Details": "收藏详情",
    "Get Favorites": "我的收藏列表",
    "Get Graph": "资产图谱",
    "Get Health": "数据源健康状态",
    "Get Heatmap": "资产热力图",
    "Get Heatmap Matrix": "资产热力矩阵",
    "Get Llm Config": "查询 LLM 配置",
    "Get Llm Config Secret": "查询 LLM 配置密钥",
    "Get Measure": "逻辑度量详情",
    "Get Mount": "挂载实体详情",
    "Get Owner View": "责任人视图",
    "Get Rule": "质量规则详情",
    "Get Semantic": "指标语义信息",
    "Get Source Overview": "数据源概览",
    "Get Stats": "埋点统计",
    "Get Term": "术语详情",
    "Get User Permissions": "查询用户权限",
    "Get Watermark": "数据水位",
    "Health Summary": "健康汇总",
    "Impact": "血缘影响分析",
    "Impact Preview": "影响预演",
    "Import Benchmark": "导入质量基准",
    "Infer Column Description": "推断字段描述",
    "Infer Descriptions Batch": "批量推断描述",
    "Infer Table Description": "推断表描述",
    "Infer Term Suggestion": "推断术语建议",
    "Issue Token": "签发客户端令牌",
    "Key Status": "密钥状态",
    "Lineage Export": "血缘导出",
    "Lineage Graph": "血缘图谱",
    "Lineage Health": "血缘健康度",
    "Lineage Metrics": "血缘指标",
    "Lineage Path": "血缘路径",
    "Lineage Path Terminals": "血缘路径端点",
    "List Action Registry": "动作注册表",
    "List Admin Users": "管理端用户列表",
    "List Audit Logs": "审计日志列表",
    "List Batch Infer History": "批量推断历史列表",
    "List Benchmarks": "质量基准列表",
    "List Catalog Databases": "目录库列表",
    "List Catalogs": "目录列表",
    "List Categories": "敏感规则分类",
    "List Channel Runs": "通道运行记录",
    "List Channels": "采集通道列表",
    "List Clients": "消费客户端列表",
    "List Collection Jobs": "采集任务列表",
    "List Collection Runs": "采集运行列表",
    "List Conflicts": "冲突列表",
    "List Data Sources": "数据源列表",
    "List Databases": "库列表",
    "List Dimension Metrics": "维度绑定指标列表",
    "List Dimensions": "维度列表",
    "List Drift Logs": "漂移日志列表",
    "List Edges": "血缘边列表",
    "List Event Logs": "事件日志列表",
    "List Event Types": "订阅事件类型列表",
    "List Events": "质量事件列表",
    "List Feature Flags": "特性开关列表",
    "List Feedback": "反馈列表",
    "List Grants": "授权列表",
    "List Mappings": "维度映射列表",
    "List Measures": "逻辑度量列表",
    "List Members": "维度成员列表",
    "List Metric Dimensions": "指标维度绑定列表",
    "List Metric Snapshots": "指标快照列表",
    "List Mounts": "挂载实体列表",
    "List Nodes": "血缘节点列表",
    "List Notifications": "通知列表",
    "List Organizations": "组织列表",
    "List Pii Assets": "PII 资产列表",
    "List Pii Templates": "PII 模板列表",
    "List Preferences": "偏好设置列表",
    "List Reconciliation Records": "对账记录列表",
    "List Reconciliations": "口径对账列表",
    "List Role Options": "角色选项列表",
    "List Role Permissions": "角色权限列表",
    "List Rules": "质量规则列表",
    "List Rulings": "仲裁裁定列表",
    "List Source Catalogs": "数据源目录列表",
    "List Source Types": "数据源类型列表",
    "List Stale": "失效血缘边列表",
    "List Subscriptions": "订阅列表",
    "List Tables": "表列表",
    "List Term Relations": "术语关系列表",
    "List Terms": "术语列表",
    "List Users": "用户列表",
    "Login": "登录",
    "Logout": "登出",
    "Mark All Read": "全部标为已读",
    "Mark Failed": "标记投递失败",
    "Mark Handled": "标记已处理",
    "Mark Read": "标记已读",
    "Mark Sent": "标记已发送",
    "Me": "当前登录用户",
    "Metric Dimension Summary": "指标维度汇总",
    "Metric Summary": "指标汇总",
    "Migrate Secrets": "迁移加密密钥",
    "My Assets": "我的资产",
    "My Permissions": "我的权限",
    "Nl2Sql": "AI 问数（NL2SQL）",
    "Notification Metrics": "通知指标",
    "Nps Stats": "NPS 统计",
    "Orphan Assets": "孤儿资产",
    "Overview Metrics": "运营总览指标",
    "Parse Lineage": "解析血缘 SQL",
    "Parse Lineage Batch": "批量解析血缘",
    "Pii Overview": "PII 概览",
    "Pii Review": "PII 复核",
    "Pii Validate": "PII 校验",
    "Preview Dimension Values": "预览维度取值",
    "Publish All Members": "批量发布维度成员",
    "Publish Dimension": "发布维度",
    "Publish Event": "发布业务事件",
    "Publish Measure": "发布逻辑度量",
    "Publish Member": "发布维度成员",
    "Publish Term": "发布术语",
    "Quality Events List": "质量事件列表（运营）",
    "Quality Metrics": "质量指标",
    "Query": "指标查询",
    "Query Metric Internal": "指标查询（内部）",
    "Recent Changes": "最近变更",
    "Reclassify Sensitivity": "敏感级别重分级",
    "Recommend Metrics": "推荐指标",
    "Recommend Terms": "推荐术语",
    "Record Observation": "记录质量观测",
    "Refresh": "刷新令牌",
    "Refresh Entity": "刷新采集实体",
    "Register Catalog": "注册采集目录",
    "Reject Term": "驳回术语",
    "Reject Version": "驳回指标版本",
    "Related Metrics": "关联指标推荐",
    "Remove Pii Override": "移除 PII 覆盖",
    "Reopen Conflict": "重新打开冲突",
    "Reset Password": "重置密码",
    "Reset Role Permissions": "重置角色权限",
    "Resolve Conflict": "解决冲突",
    "Resolve Event": "解决质量事件",
    "Response Time Stats": "响应耗时统计",
    "Restore Stale": "恢复失效边",
    "Retry Delivery": "重试投递",
    "Review Catalog Entity": "审核目录实体",
    "Review Reconciliation": "审核对账结果",
    "Revoke Grant": "撤销授权",
    "Rotate Key": "轮换密钥",
    "Run Detail": "血缘运行详情",
    "Run Reconciliation": "执行口径对账",
    "Scan Lineage Directory": "扫描血缘目录",
    "Schedule Collection": "配置采集调度",
    "Search Assets": "搜索资产",
    "Set Masking Policy": "设置脱敏策略",
    "Set Retention": "设置保留周期",
    "Set Role Permissions": "设置角色权限",
    "Set Rule Status": "设置规则状态",
    "Set User Permissions": "设置用户权限",
    "Set User Status": "设置用户状态",
    "Stream Collection Job": "采集任务实时流",
    "Submit Feedback": "提交反馈",
    "Submit Nps": "提交 NPS 评分",
    "Submit Reconciliation": "提交口径对账",
    "Submit Term": "提交术语审核",
    "Sync Metric Consumers": "同步指标消费方",
    "Test Connection": "测试连通性",
    "Test Llm Config": "测试 LLM 配置",
    "Test Rule": "测试敏感规则",
    "Unbind Metric Dimension": "解绑指标维度",
    "Unread Count": "未读通知数",
    "Update Column Description": "更新字段描述",
    "Update Data Source": "更新数据源",
    "Update Dimension": "更新维度",
    "Update Feature Flag": "更新特性开关",
    "Update Feedback Status": "更新反馈状态",
    "Update Llm Config": "更新 LLM 配置",
    "Update Mapping": "更新维度映射",
    "Update Measure": "更新逻辑度量",
    "Update Member": "更新维度成员",
    "Update Mount": "更新挂载实体",
    "Update Organization": "更新组织",
    "Update Rule": "更新质量规则",
    "Update Table Description": "更新表描述",
    "Update Term": "更新术语",
    "Update User": "更新用户",
    "Upsert Pii Override": "新增/更新 PII 覆盖",
    "Upsert Preference": "新增/更新偏好设置",
    "Upsert Subscription": "新增/更新订阅",
    "Validate Regex": "校验正则表达式",
}


def localize_openapi(schema: dict[str, object]) -> dict[str, object]:
    """对 FastAPI 生成的 OpenAPI schema 做展示层中文化（原地修改并返回）。

    Args:
        schema: ``app.openapi()`` 生成的 OpenAPI 3.x 字典。

    Returns:
        中文化后的同一字典（便于链式调用）。
    """
    # ---- info ----
    info = schema.setdefault("info", {})
    info["title"] = "Unisense 指标语义中台"
    info["description"] = _INFO_DESCRIPTION

    # ---- tags：中文名（name 中文化 + x-displayName 供兼容工具）+ 分组描述 ----
    # Swagger UI 5.x 渲染分组标题时使用 tag 的 name（不支持 x-displayName），
    # 因此直接把 name 替换为中文，并同步替换 operation.tags 引用，保证分组正确。
    # 幂等：已中文化的 tag（name 为中文）不再处理，避免二次调用清空 description。
    tag_name_zh: dict[str, str] = {}
    for tag in schema.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        name = str(tag.get("name", ""))
        zh_name, description = TAGS_ZH.get(name, (None, None))
        if zh_name:
            tag_name_zh[name] = zh_name
            tag["name"] = zh_name
            tag["description"] = description
            tag["x-displayName"] = zh_name

    # ---- operation.tags：引用同步替换为中文名 ----
    for path_item in schema.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            tags = operation.get("tags")
            if isinstance(tags, list):
                operation["tags"] = [tag_name_zh.get(t, t) for t in tags]

    # ---- operation.summary：英文 → 中文 ----
    for path_item in schema.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            summary = operation.get("summary")
            if isinstance(summary, str) and summary in SUMMARY_ZH:
                operation["summary"] = SUMMARY_ZH[summary]

    return schema
