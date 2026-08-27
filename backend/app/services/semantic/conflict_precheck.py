"""冲突预检与命名规范校验（对齐 TD §12.3 / spec FR-012/FR-013）。

指标注册时：
1. metric_code 严格校验"域_业务对象_度量_统计周期"4 段格式
2. 保留词检测（test/temp/dummy/demo/tmp/sample/staging/todo）
3. 异步调 conflict 服务预检相似口径（命中→挂 pending_conflict 标记）

``precheck`` 复用 ``app.services.conflict.similarity.detect_conflict`` 的语义规则：
- 同名不同义（SAME_NAME_DIFF_DEF，硬冲突，阻断发布）
- 敏感级冲突（PII 未授权，hard，转 governance.pii_review）
- 同义不同名 / 综合分高的重复建设（SAME_DEF_DIFF_NAME，软冲突，建议合并）
- 依赖指标未发布（DEPENDENCY_UNPUBLISHED，软提醒）

降级语义：
``precheck`` 依赖构造时注入的 ``existing_loader``（异步返回已存在口径列表）才能工作；
未注入 loader（如 metric 目录不可用）时**显式降级为空操作返回 None**，并在 docstring 中
明确此语义——它不是"忘写"的占位，而是无数据源时的受控降级路径（与发布时
``DependencyChecker`` 的硬校验互不替代）。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.conflict.similarity import detect_conflict
from app.services.semantic.dependency_checker import DependencyChecker

logger = structlog.get_logger("unisense.semantic.conflict_precheck")

# 4 段式 metric_code 正则: 域_业务对象_度量_统计周期
# 每段: 小写字母开头，后跟小写字母或数字
CODE_PATTERN = re.compile(r"^([a-z][a-z0-9]*)(_[a-z][a-z0-9]*){3}$")

# 保留词：命中后软提醒（非硬阻断），但用于命名规范校验
RESERVED_WORDS: frozenset[str] = frozenset(
    {
        "test",
        "temp",
        "dummy",
        "demo",
        "tmp",
        "sample",
        "staging",
        "todo",
    }
)

# 受控词根（词素）表：指标名须命中至少一个词根才视为业务命名（TD §12.3 命名规范强约束）。
# 统一收敛在常量中，避免散落到业务逻辑；英文词根在匹配时统一小写。
# 覆盖财务/经营/用户/业务量/度量/医疗/质量词根，供 ``validate_metric_name`` 做硬卡校验。
# 注意：此处为**内置默认词根**；业务可在「系统设置 → 字典管理」经字典
# dict_type=metric_name_morpheme 在线增删/启停用（见下方 get_controlled_morphemes）。
# 生产场景扩充：对照 auto_fill._CN_COLUMN_LABELS（SQL 推断中文名映射）已产出的
# 医生/科室/疾病/入院/出院/预约等业务对象 + HIS 门诊全流程（挂号/收费/处方/药品/医保/
# 护理/住院）+ 通用财务/运营/用户流量/质量服务指标命名，避免合法生产名被词根表遗漏误拦。
CONTROLLED_MORPHEMES: frozenset[str] = frozenset(
    {
        # 财务/经营类
        "收入",
        "营收",
        "成本",
        "利润",
        "毛利",
        "净利",
        "金额",
        "总额",
        "余额",
        "资产",
        "负债",
        "税费",
        "费用",
        # 财务/经营类（交易与资金流转）
        "交易",
        "成交",
        "支付",
        "收款",
        "付款",
        "应收",
        "应付",
        "预收",
        "预付",
        "折扣",
        "返利",
        "佣金",
        "工资",
        "薪酬",
        "奖金",
        "预算",
        "决算",
        "坏账",
        # 用户/客户类
        "用户",
        "客户",
        "会员",
        "粉丝",
        "客单",
        # 用户/流量类（注册/访问/内容互动）
        "注册",
        "登录",
        "访问",
        "访客",
        "浏览",
        "曝光",
        "下载",
        "安装",
        "启动",
        "分享",
        "点赞",
        "评论",
        "收藏",
        "关注",
        "订阅",
        "观看",
        "播放",
        "完播",
        "停留",
        "跳出",
        "跳失",
        "召回",
        "流失",
        "沉默",
        # 业务量类
        "订单",
        "销售",
        "销量",
        "产量",
        "产值",
        "库存",
        "退款",
        "复购",
        "转化",
        "留存",
        "活跃",
        # 业务量/供应链类
        "发货",
        "签收",
        "退货",
        "换货",
        "售后",
        "进货",
        "补货",
        "铺货",
        "动销",
        "缺货",
        "库龄",
        "单量",
        "件数",
        "箱数",
        "笔数",
        "批次",
        "台次",
        "车次",
        "班次",
        "航次",
        # 数仓活跃类高频缩写（A-6：TD §12.3 硬卡不误拦合法业务命名——建表列注释
        # 「月活/日活/周活/年活/季活」是标准业务词，词根表此前只有「活跃」导致
        # SQL 推断候选 name=「月活」被 METRIC_NAME_NO_MORPHEME 误拦）
        "月活",
        "日活",
        "周活",
        "年活",
        "季活",
        "新增",
        "覆盖",
        "达标",
        "份额",
        # 业务动因词
        "达成",
        "完成",
        "超额",
        "缺口",
        "净增",
        # 度量词根
        "数量",
        "数",
        "量",
        "额",
        "价",
        "费",
        "率",
        "占比",
        "比例",
        "时长",
        "频次",
        "次数",
        "平均",
        "累计",
        "环比",
        "同比",
        "增长",
        "下降",
        # 度量词根（统计/时点）
        "均值",
        "中位数",
        "方差",
        "标准差",
        "百分比",
        "千分比",
        "万分比",
        "单价",
        "均价",
        "时点",
        "期末",
        "期初",
        # 医疗/医保类（HIS 门诊场景指标命名规范）
        "门诊",
        "挂号",
        "就诊",
        "人次",
        "处方",
        "药品",
        "用药",
        "住院",
        "患者",
        "病人",
        "医保",
        "结算",
        "报销",
        "药占比",
        "检查",
        "检验",
        "手术",
        "抗菌",
        "候诊",
        "病种",
        # 医疗/卫健类（人员/机构/资源/流程）
        "医生",
        "护士",
        "医院",
        "机构",
        "科室",
        "病区",
        "床位",
        "疾病",
        "诊断",
        "症状",
        "急诊",
        "体检",
        "病历",
        "入院",
        "出院",
        "预约",
        "复诊",
        "诊疗",
        "治疗",
        "护理",
        "康复",
        "随访",
        "转诊",
        "会诊",
        "抢救",
        "死亡",
        "治愈",
        "好转",
        "留观",
        "取药",
        "发药",
        "退药",
        "耗材",
        "器械",
        "西药",
        "中药",
        "中成药",
        "草药",
        "统筹",
        "自费",
        "自付",
        "个账",
        "床日",
        "周转",
        "次均",
        "诊次",
        # 质量/服务/管理类
        "投诉",
        "客诉",
        "满意度",
        "健康度",
        "响应",
        "工单",
        "咨询",
        "线索",
        "商机",
        "合同",
        "回款",
        "开票",
        "履约",
        "超时",
        "风险",
        "告警",
        # 英文业务词根（小写匹配）
        "gmv",
        "arpu",
        "revenue",
        "cost",
        "profit",
        "user",
        "customer",
        "order",
        "amount",
        "count",
        "rate",
        "ratio",
        "sales",
        "sum",
        "avg",
        # 英文业务词根（医疗/业务对象/通用流量——对照 _CN_COLUMN_LABELS 英文列名）
        "doctor",
        "nurse",
        "patient",
        "prescription",
        "drug",
        "medicine",
        "diagnosis",
        "disease",
        "symptom",
        "dept",
        "department",
        "hospital",
        "hosp",
        "ward",
        "bed",
        "operation",
        "surgery",
        "checkup",
        "admission",
        "discharge",
        "appointment",
        "emergency",
        "visit",
        "register",
        "payment",
        "pay",
        "income",
        "expense",
        "price",
        "total",
        "fee",
        "qty",
        "quantity",
        "num",
        "cnt",
        "percent",
        "duration",
        "hours",
        "minutes",
        "dau",
        "mau",
        "retention",
        "active",
        "refund",
        "stock",
        "inventory",
        "delivery",
        "login",
        "click",
        "view",
        "play",
        "share",
        "rating",
        "satisfaction",
        "coverage",
        "achievement",
    }
)

# 依赖指标允许被消费的状态（与 DependencyChecker 一致）
_ALLOWED_DEP_STATUSES = frozenset({"PUBLISHED", "EXPERIMENTAL"})

# ---------------------------------------------------------------------------
# 词根可管理化（system_dict 字典 dict_type=metric_name_morpheme）：
# ``CONTROLLED_MORPHEMES`` 作为**内置默认词根**保留；业务/运维可在「系统设置 →
# 字典管理」在线增删/启停用词根（对齐 measure_category 字典化先例 0094）。
# 进程内缓存 = 内置默认 ∪ DB active 词根；未加载（None）时仅用内置默认。
# 缓存刷新落点：main.lifespan 启动加载 + 字典 API 变更 metric_name_morpheme 时刷新
# （best-effort：多 worker 下仅刷新当前 worker，字典校验本来就是每次查 DB 的先例）。
# ---------------------------------------------------------------------------
_MORPHEME_OVERRIDES: set[str] | None = None


def get_controlled_morphemes() -> frozenset[str]:
    """返回当前生效词根集合 = 内置默认 ∪ DB 字典 active 词根（进程内缓存）。

    ``validate_metric_name`` 读取此集合而非直接读常量——字典管理新增/停用的词根
    立即对命名校验生效，无需发版。未加载（应用未启动完整 lifespan 或测试环境）时
    回退内置默认，保持既有行为。
    """
    if _MORPHEME_OVERRIDES is None:
        return CONTROLLED_MORPHEMES
    return frozenset(set(CONTROLLED_MORPHEMES) | _MORPHEME_OVERRIDES)


async def load_metric_name_morphemes(db: AsyncSession) -> None:
    """从 system_dict（dict_type=metric_name_morpheme）加载 active 词根进缓存。

    best-effort：字典表不存在/查询失败时保留内置默认，不阻断调用方
    （对齐 lifespan 中其他播种任务的降级语义）。
    """
    global _MORPHEME_OVERRIDES
    try:
        from app.services.system_dict.repository import SystemDictRepository

        items = await SystemDictRepository(db).list_by_type(
            "metric_name_morpheme", status="active"
        )
        _MORPHEME_OVERRIDES = {str(i.code).strip() for i in items if str(i.code).strip()}
        logger.info(
            "metric_name_morphemes_loaded",
            count=len(_MORPHEME_OVERRIDES),
            total=len(get_controlled_morphemes()),
        )
    except Exception:  # noqa: BLE001 - 词根加载失败不应阻断（回退内置默认）
        logger.warning("metric_name_morphemes_load_failed", exc_info=True)


def reset_metric_name_morpheme_cache() -> None:
    """清空词根缓存（测试隔离/手动刷新用）。"""
    global _MORPHEME_OVERRIDES
    _MORPHEME_OVERRIDES = None

#: 已存在口径的加载器：``() -> Awaitable[list[dict]]``，dict 与 conflict.similarity
#: ``detect_conflict`` 期望的字段形状一致（metric_code/domain/definition/source_tables/...）。
ExistingLoader = Callable[[], Awaitable[list[dict[str, Any]]]]


class ConflictPrechecker:
    """冲突预检与命名规范校验。

    用法::

        checker = ConflictPrechecker(existing_loader=load_metrics)
        valid, error = checker.validate_code_format("sales_gmv_amount_day")
        if not valid:
            raise ValidationError(error)

        conflict = await checker.precheck("sales_gmv_amount_day", definition_json)
        if conflict:
            # 挂 pending_conflict 标记
    """

    #: 保留词集合（类级暴露，供命名规范校验与外部断言引用）
    RESERVED_WORDS: frozenset[str] = RESERVED_WORDS
    #: 受控词根集合（类级暴露，供命名规范硬卡与外部断言引用）
    CONTROLLED_MORPHEMES: frozenset[str] = CONTROLLED_MORPHEMES

    def __init__(self, existing_loader: ExistingLoader | None = None) -> None:
        """初始化预检器。

        Args:
            existing_loader: 异步加载已存在口径列表的回调；为 ``None`` 时
                ``precheck`` 显式降级为空操作（返回 None）。
        """
        self._existing_loader = existing_loader

    @staticmethod
    def validate_code_format(code: str) -> tuple[bool, str | None]:
        """校验 metric_code 格式：4 段式"域_业务对象_度量_统计周期"。

        Args:
            code: 指标编码。

        Returns:
            (合法, 错误信息): 合法为 True 时错误信息为 None。
        """
        if not code:
            return False, "metric_code 不能为空"

        if not CODE_PATTERN.match(code):
            parts = code.split("_")
            if len(parts) < 4:
                return (
                    False,
                    f"metric_code 须符合 4 段格式（域_业务对象_度量_统计周期），"
                    f"当前仅 {len(parts)} 段",
                )
            if len(parts) > 4:
                return (
                    False,
                    f"metric_code 须符合 4 段格式（域_业务对象_度量_统计周期），"
                    f"当前 {len(parts)} 段过多",
                )
            return False, "metric_code 每段须以小写字母开头，仅含小写字母和数字"

        # 检查保留词（软提醒：不硬阻断，但在校验中提示）
        segments = code.split("_")
        reserved_hits = [s for s in segments if s.lower() in RESERVED_WORDS]
        if reserved_hits:
            hits = ", ".join(reserved_hits)
            return False, f"metric_code 含保留词: {hits}，请使用业务含义明确的命名"

        return True, None

    @staticmethod
    def validate_metric_name(
        name: str | None,
        *,
        metric_type: str | None = None,
    ) -> tuple[bool, str | None]:
        """校验指标名命中受控词根（TD §12.3 命名规范强约束，硬卡）。

        指标名须包含至少一个受控业务词根（收入/成本/用户/订单/金额/数量/率 等），
        否则返回明确错误——拦截裸词/无意义命名（如 ``新名称``/``abc``）。
        维度类指标（``metric_type == "dimension"``）豁免：纯维度指标可能不含业务词根。

        词根来源可管理：内置默认词根（``CONTROLLED_MORPHEMES``）∪ 系统字典
        ``metric_name_morpheme`` 的 active 项（``get_controlled_morphemes`` 读取
        进程内缓存）——字典管理新增/停用词根即时生效，无需发版。

        Args:
            name: 指标名。
            metric_type: 指标类型；``"dimension"`` 时豁免词根校验。

        Returns:
            (合法, 错误信息): 合法为 True 时错误信息为 None。
        """
        if not name or not str(name).strip():
            return False, "指标名不能为空"
        if metric_type == "dimension":
            return True, None
        lowered = str(name).lower()
        for morpheme in get_controlled_morphemes():
            if morpheme in lowered:
                return True, None
        return False, (
            "指标名未命中受控词根，请使用业务术语（如：收入、成本、用户数、"
            "订单量、金额、占比…）"
        )

    @staticmethod
    def _to_candidate(
        metric_code: str,
        definition_json: dict[str, Any],
        extra_source_tables: list[str] | None = None,
        metric_id: int | None = None,
    ) -> dict[str, Any]:
        """将指标编码 + 口径定义转为 conflict.similarity 期望的候选字典。

        ``extra_source_tables``：OneData 挂载层权威（挂载实体的 source_table），
        由调用方在 async 上下文解析后传入——挂载独立更新后 definition_json 的
        source_tables 冗余可能过期，合并挂载源表保证预检比对基于最新物理来源。
        ``metric_id``：候选的真实指标行 ID（创建后调用须传入）。detect_conflict 的
        自我引用防御依赖「候选/现有双侧 metric_id 相等判定」——候选不携带 ID 时
        防御单侧失效，同码自身条目会被误报为 SAME_NAME_DIFF_DEF（曾致 13 个
        存量指标挂自引用孤儿标记，见 backfill_conflict_orphans）。
        """
        tables = list(definition_json.get("source_tables", []) or [])
        for t in extra_source_tables or []:
            if t and t not in tables:
                tables.append(t)
        return {
            "metric_code": metric_code,
            "domain": definition_json.get("domain", ""),
            "definition": (
                definition_json.get("definition") or definition_json.get("expression") or ""
            ),
            "source_tables": tables,
            "has_pii": bool(definition_json.get("pii", False)),
            "pii_authorized": bool(definition_json.get("pii_authorized", False)),
            "metric_id": metric_id,
        }

    @staticmethod
    def _detection_to_detail(det: Any) -> dict[str, Any]:
        """将 ConflictDetection 转为持久化到 pending_conflict_detail 的字典。"""
        return {
            "conflict_type": det.conflict_type.value,
            "score": det.score,
            "existing_code": det.existing_code,
            "existing_metric_id": det.existing_metric_id,
            "severity": det.severity,
            "block_publish": det.block_publish,
            "reason": det.reason,
        }

    async def precheck(
        self,
        metric_code: str,
        definition_json: dict[str, Any],
        *,
        extra_source_tables: list[str] | None = None,
        metric_id: int | None = None,
    ) -> dict[str, Any] | None:
        """异步预检：相似口径 / 敏感级 / 依赖未发布。

        命中冲突→返回冲突详情 dict（供挂 pending_conflict 标记）；无冲突→返回 None。
        未注入 ``existing_loader`` 时显式降级为空操作（返回 None），日志提示降级原因。

        Args:
            metric_code: 指标编码。
            definition_json: 口径定义。
            extra_source_tables: OneData 挂载层权威源表列表（挂载实体的 source_table），
                独立更新后并入比对，避免 definition_json 冗余过期导致漏检。
            metric_id: 候选的真实指标行 ID（创建后调用须传入自身 id）。候选不携带
                metric_id 时 detect_conflict 的自我引用防御单侧失效——同码自身条目
                会被误判 SAME_NAME_DIFF_DEF（候选 definition_json 缺 domain 键→域空
                与库里域不等，触发「同名定义/域不同」）。传自身 id 后，与 existing 中
                同码自身行（同 metric_id）配对时防御生效返回 None。

        Returns:
            冲突详情或 None。
        """
        if self._existing_loader is None:
            logger.info(
                "conflict_precheck_degraded",
                metric_code=metric_code,
                reason="existing_loader 未注入，预检降级为空操作",
            )
            return None

        candidate = self._to_candidate(
            metric_code,
            definition_json,
            extra_source_tables=extra_source_tables,
            metric_id=metric_id,
        )
        existing_list = await self._existing_loader()
        if not existing_list:
            return None

        # ① 语义冲突（同名不同义 / PII / 重复建设），复用 conflict 服务相似度规则
        # 注意：不与候选同码条目做排除——同名不同义的合法形态恰是「新提交 vs
        # 已存在同码行」（候选未落库、existing 为既有行）。创建后调用须传 metric_id，
        # 使 detect_conflict 对「同码且同 metric_id」的自身条目免疫误报（自我冲突防御
        # 依赖双侧 metric_id 相等判定，见 conflict.similarity.detect_conflict）。
        for ext in existing_list:
            det = detect_conflict(candidate, ext)
            if det is not None:
                logger.info(
                    "conflict_precheck_hit",
                    metric_code=metric_code,
                    conflict_type=det.conflict_type.value,
                    existing_code=det.existing_code,
                )
                return self._detection_to_detail(det)

        # ② 依赖未发布（软提醒，不阻断；发布时由 DependencyChecker 硬校验）
        deps = definition_json.get("dependencies") or []
        metric_deps = [d for d in deps if DependencyChecker._is_metric_code(d)]
        if metric_deps:
            by_code = {e.get("metric_code"): e for e in existing_list}
            unpublished = [
                d
                for d in metric_deps
                if d in by_code and by_code[d].get("status") not in _ALLOWED_DEP_STATUSES
            ]
            if unpublished:
                return {
                    "conflict_type": "DEPENDENCY_UNPUBLISHED",
                    "score": 0.0,
                    "existing_code": "",
                    "existing_metric_id": None,
                    "severity": "soft",
                    "block_publish": False,
                    "reason": f"依赖指标未发布: {', '.join(unpublished)}",
                }

        return None
