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
# 覆盖财务/经营/用户/业务量/度量词根，供 ``validate_metric_name`` 做硬卡校验。
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
        # 用户/客户类
        "用户",
        "客户",
        "会员",
        "粉丝",
        "客单",
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
        "新增",
        "覆盖",
        "达标",
        "份额",
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
    }
)

# 依赖指标允许被消费的状态（与 DependencyChecker 一致）
_ALLOWED_DEP_STATUSES = frozenset({"PUBLISHED", "EXPERIMENTAL"})

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
        for morpheme in CONTROLLED_MORPHEMES:
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
    ) -> dict[str, Any]:
        """将指标编码 + 口径定义转为 conflict.similarity 期望的候选字典。

        ``extra_source_tables``：OneData 挂载层权威（挂载实体的 source_table），
        由调用方在 async 上下文解析后传入——挂载独立更新后 definition_json 的
        source_tables 冗余可能过期，合并挂载源表保证预检比对基于最新物理来源。
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
    ) -> dict[str, Any] | None:
        """异步预检：相似口径 / 敏感级 / 依赖未发布。

        命中冲突→返回冲突详情 dict（供挂 pending_conflict 标记）；无冲突→返回 None。
        未注入 ``existing_loader`` 时显式降级为空操作（返回 None），日志提示降级原因。

        Args:
            metric_code: 指标编码。
            definition_json: 口径定义。
            extra_source_tables: OneData 挂载层权威源表列表（挂载实体的 source_table），
                独立更新后并入比对，避免 definition_json 冗余过期导致漏检。

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
            metric_code, definition_json, extra_source_tables=extra_source_tables
        )
        existing_list = await self._existing_loader()
        if not existing_list:
            return None

        # ① 语义冲突（同名不同义 / PII / 重复建设），复用 conflict 服务相似度规则
        # 注意：不与候选同码条目做排除——同名不同义的合法形态恰是「新提交 vs
        # 已存在同码行」；precheck 仅在创建后调用，此时同码条目即刚创建的自身行，
        # 定义一致不会触发误报（自我冲突防御落在 check 创建入口，见 conflict.service）。
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
