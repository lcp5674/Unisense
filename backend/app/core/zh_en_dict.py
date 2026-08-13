"""中英术语字典：中文业务名词 → 英文编码片段（生产级离线翻译）。

供编码自动生成使用（主题域编码/术语编码/维度编码等）：中文显示名经贪心
最长匹配翻译为英文，未覆盖词回退拼音、纯标点回退默认值。与
``app/core/audit_i18n.py`` 的静态映射模式一致——离线、确定性、零网络依赖，
避免在线翻译 API 在生产环境的可用性/限流/时延风险。

用法::

    >>> zh_to_en("销售订单")
    'sales_order'
    >>> zh_to_en("供应链")
    'supply_chain'
    >>> zh_to_en("风控合规")
    'risk_control_compliance'

注意：前端 ``frontend/src/utils/zhEnDict.ts`` 与本字典保持同步（预览与后端
生成规则对齐）。新增术语请两处同步更新。
"""

from __future__ import annotations

from typing import Any

#: 贪心最长匹配的最大词长（常用中文业务词为 2-4 字）
MAX_WORD = 4

#: 中文术语 → 英文编码片段（小写、下划线连接）
ZH_EN_MAP: dict[str, str] = {
    # 业务域（销售/订单/供应链等）
    "销售": "sales",
    "订单": "order",
    "用户": "user",
    "客户": "customer",
    "会员": "member",
    "商品": "product",
    "库存": "stock",
    "采购": "purchase",
    "供应商": "supplier",
    "供应链": "supply_chain",
    "渠道": "channel",
    "门店": "store",
    "仓库": "warehouse",
    "物流": "logistics",
    "配送": "delivery",
    "售后": "after_sale",
    "退款": "refund",
    "退货": "return",
    "优惠券": "coupon",
    "活动": "campaign",
    "营销": "marketing",
    "推广": "promotion",
    "广告": "ad",
    "流量": "traffic",
    "转化": "conversion",
    "留存": "retention",
    "活跃": "active",
    "流失": "churn",
    "复购": "repurchase",
    "支付": "payment",
    "交易": "transaction",
    "收入": "revenue",
    "支出": "expense",
    "成本": "cost",
    "利润": "profit",
    "毛利": "gross_profit",
    "毛利率": "gross_margin",
    "金额": "amount",
    "数量": "quantity",
    "单价": "unit_price",
    "价格": "price",
    "预算": "budget",
    "财务": "finance",
    "会计": "accounting",
    "税务": "tax",
    "对账": "reconciliation",
    "结算": "settlement",
    "发票": "invoice",
    "定价": "pricing",
    "折扣": "discount",
    "满减": "full_reduction",
    "余额": "balance",
    "账单": "bill",
    "借款": "loan",
    "还款": "repayment",
    "逾期": "overdue",
    "风控": "risk_control",
    "合规": "compliance",
    "反欺诈": "anti_fraud",
    "授信": "credit",
    "额度": "limit",
    # 组织/人员
    "组织": "organization",
    "部门": "department",
    "员工": "employee",
    "团队": "team",
    "项目": "project",
    "角色": "role",
    "权限": "permission",
    "岗位": "position",
    "人力": "human_resource",
    "招聘": "recruitment",
    "绩效": "performance",
    "薪酬": "salary",
    "培训": "training",
    "考勤": "attendance",
    "主管": "supervisor",
    "经理": "manager",
    "负责人": "owner",
    "集团": "group",
    "分公司": "branch",
    "事业部": "business_unit",
    # 平台/系统
    "系统": "system",
    "平台": "platform",
    "应用": "app",
    "数据": "data",
    "服务": "service",
    "接口": "api",
    "任务": "task",
    "流程": "process",
    "审批": "approval",
    "配置": "config",
    "环境": "environment",
    "模块": "module",
    "功能": "feature",
    "菜单": "menu",
    "页面": "page",
    "组件": "component",
    "缓存": "cache",
    "队列": "queue",
    "消息": "message",
    "事件": "event",
    "日志": "log",
    "监控": "monitor",
    "告警": "alert",
    "审计": "audit",
    "备份": "backup",
    "恢复": "restore",
    "迁移": "migration",
    "升级": "upgrade",
    "部署": "deploy",
    "测试": "test",
    "开发": "develop",
    "运维": "ops",
    "安全": "security",
    "加密": "encrypt",
    "脱敏": "mask",
    "标签": "tag",
    "画像": "profile",
    "行为": "behavior",
    "点击": "click",
    "曝光": "impression",
    "浏览": "view",
    "搜索": "search",
    "收藏": "favorite",
    "分享": "share",
    "评论": "comment",
    "点赞": "like",
    "关注": "follow",
    "订阅": "subscribe",
    "推送": "push",
    "通知": "notify",
    # 指标/分析
    "指标": "metric",
    "维度": "dimension",
    "度量": "measure",
    "主题": "theme",
    "粒度": "granularity",
    "单位": "unit",
    "聚合": "aggregation",
    "口径": "caliber",
    "公式": "formula",
    "表达式": "expression",
    "每日": "daily",
    "每周": "weekly",
    "每月": "monthly",
    "每年": "yearly",
    "同期": "same_period",
    "环比": "mom",
    "同比": "yoy",
    "累计": "cumulative",
    "平均": "average",
    "总计": "total",
    "汇总": "summary",
    "明细": "detail",
    "占比": "ratio",
    "趋势": "trend",
    "增长率": "growth_rate",
    "完成率": "completion_rate",
    "达成率": "achievement_rate",
    "目标": "target",
    "计划": "plan",
    "实际": "actual",
    "预测": "forecast",
    "基线": "baseline",
    "阈值": "threshold",
    "波动": "fluctuation",
    "异常": "anomaly",
    "健康": "health",
    "质量": "quality",
    "治理": "governance",
    "规则": "rule",
    "策略": "strategy",
    "标准": "standard",
    "规范": "spec",
    "分级": "tier",
    "核心": "core",
    "基础": "basic",
    "高级": "advanced",
    "概览": "overview",
    "总览": "overview",
    "首页": "home",
    "工作台": "workbench",
    "域": "domain",
    "地理": "geo",
    # 时间
    "时间": "time",
    "日期": "date",
    "年": "year",
    "月": "month",
    "周": "week",
    "日": "day",
    "小时": "hour",
    "分钟": "minute",
    "秒": "second",
    "季度": "quarter",
    "时段": "period",
    "区间": "range",
    "周期": "cycle",
    "时效": "freshness",
    "实时": "realtime",
    "离线": "offline",
    "定时": "scheduled",
    "最近": "recent",
    "当前": "current",
    "历史": "history",
    "未来": "future",
    # 地域
    "区域": "region",
    "省份": "province",
    "城市": "city",
    "地区": "area",
    "国家": "country",
    "国内": "domestic",
    "海外": "overseas",
    "国际": "international",
    "华东": "east_china",
    "华北": "north_china",
    "华南": "south_china",
    "华中": "central_china",
    "西南": "southwest",
    "西北": "northwest",
    "东北": "northeast",
    # 数据源/数仓
    "数据源": "data_source",
    "数据库": "database",
    "表": "table",
    "字段": "field",
    "列": "column",
    "连接": "connection",
    "采集": "collect",
    "同步": "sync",
    "导入": "import",
    "导出": "export",
    "清洗": "clean",
    "转换": "transform",
    "加载": "load",
    "血缘": "lineage",
    "影响": "impact",
    "上游": "upstream",
    "下游": "downstream",
    "依赖": "dependency",
    "冲突": "conflict",
    "评审": "review",
    "发布": "publish",
    "废弃": "deprecate",
    "下线": "retire",
    "草稿": "draft",
    "实验": "experiment",
    "审核": "approve",
    "确认": "confirm",
    "拒绝": "reject",
    "通过": "pass",
    "失败": "fail",
    "成功": "success",
    "待办": "todo",
    "待审": "pending_review",
    "数仓": "warehouse",
    "分层": "layer",
    "贴源": "ods",
    # 常用动作/属性
    "生产": "production",
    "经营": "operation",
    "管理": "management",
    "中心": "center",
    "分析": "analysis",
    "决策": "decision",
    "报表": "report",
    "看板": "dashboard",
    "驾驶舱": "cockpit",
    "资产": "asset",
    "模型": "model",
    "引擎": "engine",
    "模板": "template",
    "指南": "guide",
    "帮助": "help",
    "文档": "doc",
    "版本": "version",
    "记录": "record",
    "列表": "list",
    "详情": "detail",
    "新增": "add",
    "编辑": "edit",
    "删除": "delete",
    "保存": "save",
    "提交": "submit",
    "返回": "back",
    "下一步": "next",
    "上一步": "prev",
    "全部": "all",
    "其他": "other",
    "默认": "default",
    "示例": "example",
}

#: 未知单字的拼音兜底（延迟导入，避免在无 pypinyin 环境硬依赖）
import contextlib  # noqa: E402

_lazy_pinyin: Any | None = None

with contextlib.suppress(ImportError):
    from pypinyin import lazy_pinyin as _lazy_pinyin


def _pinyin_char(ch: str) -> str:
    """单字 → 拼音（无音调）；无 pypinyin 时回退空串（由调用方再兜底）。"""
    if _lazy_pinyin is not None:
        return str(_lazy_pinyin(ch)[0])
    return ""


def zh_to_en(segment: str) -> str:
    """中文段 → 英文编码片段（贪心最长匹配 + 单字拼音兜底）。

    规则：
    - 整段优先：字典含完整词（如「供应链」→ ``supply_chain``）则整体翻译；
    - 否则从左到右贪心取最长命中词（最多 ``MAX_WORD`` 字），逐词翻译；
    - 未命中的单字用拼音兜底（如「钿」→ ``dian``）；
    - 多个词用下划线连接（如「销售订单」→ ``sales_order``）。

    Args:
        segment: 连续中文段（不含 ASCII/标点）。

    Returns:
        英文编码片段（可能为空串——所有字均无法翻译且无拼音时）。
    """
    parts: list[str] = []
    i, n = 0, len(segment)
    while i < n:
        ch = segment[i]
        if not ("\u4e00" <= ch <= "\u9fff"):
            # 非 CJK（防御调用方误传）：连续字符合并为一个 token 并小写
            j = i
            while j < n and not ("\u4e00" <= segment[j] <= "\u9fff"):
                j += 1
            parts.append(segment[i:j].lower())
            i = j
            continue
        matched = False
        for end in range(min(i + MAX_WORD, n), i, -1):
            en = ZH_EN_MAP.get(segment[i:end])
            if en:
                parts.append(en)
                i = end
                matched = True
                break
        if not matched:
            parts.append(ZH_EN_MAP.get(ch) or _pinyin_char(ch))
            i += 1
    return "_".join(p for p in parts if p)
