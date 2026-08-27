"""指标健康度评分引擎（对齐 TD §12.3 五维加权模型）。

五维加权：口径完整度 25% / 活跃度 20% / 质量 25% / Owner 响应 15% / 血缘覆盖 15%。
缺失维度记 0 并标 "数据不足"。
分级：≥85 优(EXCELLENT) / 70-84 良(GOOD) / 55-69 警(WARNING) / <55 危(CRITICAL)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
_ACTIVITY_RECENT_UPDATE = 80  # 近 30 天有变更或消费查询
_ACTIVITY_QUERY_AGED = 50  # 近 30 天无变更/查询，但近 90 天有消费查询
_ACTIVITY_STALE = 20  # 无近期变更且无消费查询

# 质量评分
_QUALITY_PII_UNREVIEWED = 30  # 含 PII 但未合规审核
_QUALITY_REVIEWED = 90  # 已合规审核
_QUALITY_DEFAULT = 70  # 无 PII 且未审核
# 近 30 天 quality_event 未关闭异常反比扣分（TD §12.3：异常越多质量分越低）
_QUALITY_CRITICAL_DEDUCT = 45  # P0 每件扣 45
_QUALITY_MAJOR_DEDUCT = 25  # P1 每件扣 25
_QUALITY_MINOR_DEDUCT = 12  # P2 每件扣 12

# Owner 响应评分
_OWNER_HAS_BACKUP = 85  # 配置了 backup_owner（基础分）
_OWNER_NO_BACKUP = 45  # 无 backup_owner（基础分）
# 质量告警响应闭环（TD §4.1 quality_event.ack_at/resolved_at 真实响应记录）：
_OWNER_OPEN_EVENT_DEDUCT = 15  # 近 90 天每件未 ACK 的 OPEN 事件扣 15
_OWNER_ALL_CLOSED_BONUS = 10  # 近 90 天告警全部 ACK/RESOLVE/CLOSE → 响应及时加分

# 血缘覆盖评分
_LINEAGE_EDGES = 90  # 有真实血缘边（lineage_edge，TD §12.2）
_LINEAGE_FULL = 80  # 无真实边但有 dependencies + expression
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

        # 3. 质量（合规状态 + 近 30 天 quality_event 异常反比，TD §12.3）
        scores["quality"], qual_missing = await self._calc_quality(metric)
        if qual_missing:
            missing_dimensions.append("quality")

        # 4. Owner 响应（backup_owner 配置 + 质量告警响应闭环 quality_event）
        scores["owner_response"], own_missing = await self._calc_owner_response(metric)
        if own_missing:
            missing_dimensions.append("owner_response")

        # 5. 血缘覆盖（真实血缘边 lineage_edge + 口径依赖声明）
        scores["lineage_coverage"], lin_missing = await self._calc_lineage_coverage(metric)
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
        """活跃度：近 30 天指标变更或消费查询（真实使用信号）。

        数据源：``metric.updated_at``（变更）+ ``query_log``（消费查询日志，
        TD §12.6——真实执行查询后 best-effort 落库）。此前仅看 updated_at，
        指标创建后无人编辑则活跃度永远 20——现接入 query_log：近 30 天有变更
        或查询 → 高活跃；仅近 90 天有查询 → 中活跃；两者皆无 → 低活跃。
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

        # 无近期变更 → 查消费查询日志（真实使用信号）
        from sqlalchemy import func, select

        from app.models.consume import QueryLog

        since_30 = now - timedelta(days=30)
        cnt_30 = (
            await self._db.execute(
                select(func.count())
                .select_from(QueryLog)
                .where(
                    QueryLog.metric_code == metric.metric_code,
                    QueryLog.created_at >= since_30,
                )
            )
        ).scalar_one()
        if cnt_30 > 0:
            return _ACTIVITY_RECENT_UPDATE, None

        since_90 = now - timedelta(days=90)
        cnt_90 = (
            await self._db.execute(
                select(func.count())
                .select_from(QueryLog)
                .where(
                    QueryLog.metric_code == metric.metric_code,
                    QueryLog.created_at >= since_90,
                )
            )
        ).scalar_one()
        if cnt_90 > 0:
            return _ACTIVITY_QUERY_AGED, None
        return _ACTIVITY_STALE, None

    async def _calc_quality(self, metric: Metric) -> tuple[int, list[str] | None]:
        """质量：合规审核状态为基础，近 30 天未关闭 quality_event 异常反比扣分。

        修复前（P2-12）：仅看 pii/compliance 状态，quality 服务写入的异常事件
        不影响健康度——"质量"维度与 quality_event 脱节。现按 TD §12.3 接入：
        近 30 天 OPEN/ACK 的质量异常按等级扣分（P0 重 / P1 中 / P2 轻），
        异常越多质量分越低；无异常时维持合规基础分。
        """
        # 基础分：合规审核状态
        if metric.pii_flag and not metric.compliance_reviewed:
            base = _QUALITY_PII_UNREVIEWED
        elif metric.compliance_reviewed:
            base = _QUALITY_REVIEWED
        else:
            base = _QUALITY_DEFAULT

        # 近 30 天未关闭质量异常 → 按等级反比扣分
        from sqlalchemy import func, select

        from app.models.quality import (
            QualityEvent,
            QualityEventStatus,
            QualitySeverity,
        )

        since = datetime.now(UTC) - timedelta(days=30)
        stmt = (
            select(QualityEvent.level, func.count())
            .where(
                QualityEvent.metric_id == metric.id,
                QualityEvent.created_at >= since,
                QualityEvent.status.in_(
                    [QualityEventStatus.OPEN, QualityEventStatus.ACK]
                ),
            )
            .group_by(QualityEvent.level)
        )
        rows = (await self._db.execute(stmt)).all()
        deductions = {
            QualitySeverity.P0: _QUALITY_CRITICAL_DEDUCT,
            QualitySeverity.P1: _QUALITY_MAJOR_DEDUCT,
            QualitySeverity.P2: _QUALITY_MINOR_DEDUCT,
        }
        deduction = sum(
            deductions.get(level, 0) * int(count) for level, count in rows
        )
        return max(0, base - deduction), None

    async def _calc_owner_response(self, metric: Metric) -> tuple[int, list[str] | None]:
        """Owner 响应：备份 Owner 配置 + 质量告警响应闭环。

        数据源：``backup_owner_id``（兜底配置）+ ``quality_event`` 的
        ack_at/resolved_at（真实响应记录，TD §4.1）。此前仅看有无 backup_owner，
        与"响应"无关——现接入告警闭环：近 90 天无告警 → 维持配置基础分；
        有告警且全部 ACK/RESOLVE/CLOSE → 响应及时加分；有未 ACK 的 OPEN 事件
        → 每件扣分（Owner 对告警迟迟不响应，响应度低）。
        """
        base = (
            _OWNER_HAS_BACKUP
            if metric.backup_owner_id is not None
            else _OWNER_NO_BACKUP
        )

        from sqlalchemy import select

        from app.models.quality import QualityEvent, QualityEventStatus

        since = datetime.now(UTC) - timedelta(days=90)
        stmt = (
            select(QualityEvent.status)
            .where(
                QualityEvent.metric_id == metric.id,
                QualityEvent.created_at >= since,
            )
        )
        rows = list((await self._db.execute(stmt)).scalars().all())
        total = len(rows)
        if total == 0:
            return base, None

        open_unacked = sum(1 for s in rows if s == QualityEventStatus.OPEN)
        if open_unacked == 0:
            # 告警全部闭环 → 响应及时
            return min(100, base + _OWNER_ALL_CLOSED_BONUS), None
        return max(0, base - open_unacked * _OWNER_OPEN_EVENT_DEDUCT), None

    async def _calc_lineage_coverage(self, metric: Metric) -> tuple[int, list[str] | None]:
        """血缘覆盖：真实血缘边（lineage_edge）+ 口径依赖声明。

        数据源：``lineage_edge`` 表（TD §12.2，指标节点 ``metric:{code}``）。
        此前仅看 definition_json 声明，与真实血缘脱节——现查真实血缘边
        （含失效队列过滤）：有真实边 → 最高档；无真实边才回退口径声明档。
        """
        definition = metric.definition_json or {}
        has_deps = bool(definition.get("dependencies"))
        has_expr = bool(definition.get("expression"))

        from sqlalchemy import func, or_, select

        from app.models.lineage import LineageEdge

        node = f"metric:{metric.metric_code}"
        edge_count = (
            await self._db.execute(
                select(func.count())
                .select_from(LineageEdge)
                .where(
                    or_(
                        LineageEdge.source_node == node,
                        LineageEdge.target_node == node,
                    ),
                    LineageEdge.deleted_at.is_(None),
                    LineageEdge.stale.is_(False),  # 失效队列不计入覆盖
                )
            )
        ).scalar_one()

        if edge_count > 0:
            return _LINEAGE_EDGES, None
        if has_deps and has_expr:
            return _LINEAGE_FULL, None
        if has_expr:
            return _LINEAGE_EXPRESSION_ONLY, None
        return _LINEAGE_NONE, ["lineage_coverage"]
