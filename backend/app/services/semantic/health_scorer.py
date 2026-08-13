"""指标健康度评分引擎（对齐 TD §12.3 五维加权模型）。

五维加权：口径完整度 25% / 活跃度 20% / 质量 25% / Owner 响应 15% / 血缘覆盖 15%。
缺失维度记 0 并标 "数据不足"。
分级：≥85 优(EXCELLENT) / 70-84 良(GOOD) / 55-69 警(WARNING) / <55 危(CRITICAL)。
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric import Metric
from app.models.metric_health import MetricHealthScore

logger = structlog.get_logger("unisense.semantic.health_scorer")

# 一等字段列表（口径完整度校验）
_ESSENTIAL_FIELDS = (
    "granularity",
    "unit",
    "aggregation",
    "time_semantics",
    "freshness",
    "sla",
    "dw_layer",
    "serving_mode",
    "additivity",
)

# 加权系数
_WEIGHTS = {
    "completeness": 0.25,
    "activity": 0.20,
    "quality": 0.25,
    "owner_response": 0.15,
    "lineage_coverage": 0.15,
}

# ---- 评分配置（消除 magic number，集中管理，可按域覆盖）----

# 口径完整度：每缺失一个一等字段扣分
_COMPLETENESS_DEDUCTION_PER_MISSING = 12

# 活跃度评分
_ACTIVITY_RECENT_UPDATE = 80  # 近 30 天有更新
_ACTIVITY_STALE = 20  # 无近期更新

# 质量评分
_QUALITY_PII_UNREVIEWED = 30  # 含 PII 但未合规审核
_QUALITY_REVIEWED = 90  # 已合规审核
_QUALITY_DEFAULT = 70  # 无 PII 且未审核

# Owner 响应评分
_OWNER_HAS_BACKUP = 85  # 配置了 backup_owner
_OWNER_NO_BACKUP = 45  # 无 backup_owner

# 血缘覆盖评分
_LINEAGE_FULL = 80  # 有 dependencies + expression
_LINEAGE_EXPRESSION_ONLY = 50  # 仅 expression
_LINEAGE_NONE = 10  # 无血缘信息


def _grade(score: int) -> str:
    if score >= 85:
        return "EXCELLENT"
    if score >= 70:
        return "GOOD"
    if score >= 55:
        return "WARNING"
    return "CRITICAL"


class HealthScorer:
    """指标健康度评分引擎。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def calculate(self, metric_id: int) -> MetricHealthScore:
        """计算单个指标的健康度评分。

        Args:
            metric_id: 指标ID。

        Returns:
            健康度评分对象。
        """
        metric = await self._db.get(Metric, metric_id)
        if metric is None:
            raise ValueError(f"指标不存在: {metric_id}")

        missing_dimensions: list[str] = []
        scores: dict[str, int] = {}

        # 1. 口径完整度
        scores["completeness"], comp_missing = self._calc_completeness(metric)
        if comp_missing:
            missing_dimensions.extend(comp_missing)

        # 2. 活跃度（简化：基于 updated_at 判断近 30 天是否有变更/查询）
        scores["activity"], act_missing = await self._calc_activity(metric)
        if act_missing:
            missing_dimensions.append("activity")

        # 3. 质量（简化：基于 compliance_reviewed 和 pii_flag 状态）
        scores["quality"], qual_missing = self._calc_quality(metric)
        if qual_missing:
            missing_dimensions.append("quality")

        # 4. Owner 响应（简化：基于 backup_owner_id 是否配置）
        scores["owner_response"], own_missing = self._calc_owner_response(metric)
        if own_missing:
            missing_dimensions.append("owner_response")

        # 5. 血缘覆盖（简化：基于 definition_json.dependencies 是否填充）
        scores["lineage_coverage"], lin_missing = self._calc_lineage_coverage(metric)
        if lin_missing:
            missing_dimensions.append("lineage_coverage")

        # 加权总分
        total = sum(scores[dim] * _WEIGHTS[dim] for dim in _WEIGHTS)
        total_score = int(round(total))

        now = datetime.now(UTC)
        health = MetricHealthScore(
            metric_id=metric_id,
            score=total_score,
            level=_grade(total_score),
            completeness_score=scores["completeness"],
            activity_score=scores["activity"],
            quality_score=scores["quality"],
            owner_response_score=scores["owner_response"],
            lineage_coverage_score=scores["lineage_coverage"],
            missing_dimensions=missing_dimensions if missing_dimensions else None,
            calculated_at=now,
        )
        return health

    async def batch_calculate(self, metric_ids: list[int]) -> list[MetricHealthScore]:
        """批量计算指标健康度。

        Args:
            metric_ids: 指标ID列表。

        Returns:
            健康度评分列表。
        """
        results: list[MetricHealthScore] = []
        for mid in metric_ids:
            try:
                health = await self.calculate(mid)
                results.append(health)
            except Exception:
                logger.warning("health_calculate_failed", metric_id=mid)
        return results

    # ---- 维度计算方法 ----

    @staticmethod
    def _calc_completeness(metric: Metric) -> tuple[int, list[str] | None]:
        """口径完整度：一等字段齐全率。"""
        missing = []
        for field in _ESSENTIAL_FIELDS:
            val = getattr(metric, field, None)
            if val is None or val == "" or val == "NONE":
                missing.append(field)
        if not missing:
            return 100, None
        # 每缺失一个字段扣分
        score = max(0, 100 - len(missing) * _COMPLETENESS_DEDUCTION_PER_MISSING)
        return score, missing

    async def _calc_activity(self, metric: Metric) -> tuple[int, list[str] | None]:
        """活跃度：近 30 天是否有更新/查询。

        近 30 天有更新 → _ACTIVITY_RECENT_UPDATE，
        无更新 → _ACTIVITY_STALE，无 consume 数据 → 标缺失。
        """
        now = datetime.now(UTC)
        if metric.updated_at:
            updated = (
                metric.updated_at.replace(tzinfo=UTC)
                if metric.updated_at.tzinfo is None
                else metric.updated_at
            )
            if (now - updated).days < 30:
                return _ACTIVITY_RECENT_UPDATE, None
        return _ACTIVITY_STALE, None

    @staticmethod
    def _calc_quality(metric: Metric) -> tuple[int, list[str] | None]:
        """质量：基于合规审核状态。"""
        if metric.pii_flag and not metric.compliance_reviewed:
            return _QUALITY_PII_UNREVIEWED, None
        if metric.compliance_reviewed:
            return _QUALITY_REVIEWED, None
        return _QUALITY_DEFAULT, None

    @staticmethod
    def _calc_owner_response(metric: Metric) -> tuple[int, list[str] | None]:
        """Owner 响应：基于 backup_owner 是否配置。"""
        if metric.backup_owner_id is not None:
            return _OWNER_HAS_BACKUP, None
        return _OWNER_NO_BACKUP, None

    @staticmethod
    def _calc_lineage_coverage(metric: Metric) -> tuple[int, list[str] | None]:
        """血缘覆盖：基于依赖信息是否填充。"""
        definition = metric.definition_json or {}
        has_deps = bool(definition.get("dependencies"))
        has_expr = bool(definition.get("expression"))
        if has_deps and has_expr:
            return _LINEAGE_FULL, None
        if has_expr:
            return _LINEAGE_EXPRESSION_ONLY, None
        return _LINEAGE_NONE, ["lineage_coverage"]
