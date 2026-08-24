"""指标复用度 / 资产账本统计服务（数仓视角治理）。

对齐 TD §12.3 血缘复用与指标资产健康：复用血缘边（DERIVED_FROM / CONSUMED_BY）
量化「原子指标 → 派生指标 → 报表引用」链路的复用度，并复用 HealthScorer 的
活跃度维度识别僵尸指标（长期无更新 + 零引用）与冲突预检的重复建设信号。

数据访问复用 ``LineageRepository``（血缘边的权威存储查询），不新造轮子。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric import Metric
from app.services.lineage.repository import LineageRepository
from app.services.semantic.health_scorer import _ACTIVITY_STALE, HealthScorer

#: 重复建设冲突类型（对齐 conflict/similarity.py 的 SAME_DEF_DIFF_NAME，软冲突信号）。
_DUPLICATE_CONFLICT_TYPE = "same_def_diff_name"


def _days_since(dt: datetime | None) -> int | None:
    """距现在的自然天数（naive 时间按 UTC 解释；无时间返回 None）。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - dt).days)


class MetricStatsService:
    """指标复用度与资产账本统计。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repo = LineageRepository(db)

    async def reuse_summary(self) -> dict[str, Any]:
        """每个指标的被引用统计清单（P0：数仓视角复用度）。

        Returns:
            ``{"total", "referenced", "zero_reuse", "items"}``；``items`` 为每个
            指标的 ``metric_code/name/domain/type/status`` + ``derived_by_count``
            （被派生指标 DERIVED_FROM 引用数）+ ``consumed_by_count``（被报表
            CONSUMED_BY 引用数）+ ``reuse_count``（总复用度），按复用度降序
            （并列按 metric_code 升序），高复用核心指标与零复用指标一目了然。
        """
        metrics = (
            await self._db.execute(
                select(
                    Metric.metric_code,
                    Metric.name,
                    Metric.domain,
                    Metric.type,
                    Metric.status,
                ).where(Metric.deleted_at.is_(None))
            )
        ).all()
        reuse = await self._repo.metric_reuse_counts()
        items: list[dict[str, Any]] = []
        for code, name, domain, mtype, status in metrics:
            counts = reuse.get(code, {"derived_by": 0, "consumed_by": 0})
            reuse_count = counts["derived_by"] + counts["consumed_by"]
            items.append(
                {
                    "metric_code": code,
                    "name": name,
                    "domain": domain,
                    "type": mtype,
                    "status": status,
                    "derived_by_count": counts["derived_by"],
                    "consumed_by_count": counts["consumed_by"],
                    "reuse_count": reuse_count,
                }
            )
        items.sort(key=lambda it: (-it["reuse_count"], it["metric_code"]))
        return {
            "total": len(items),
            "referenced": sum(1 for it in items if it["reuse_count"] > 0),
            "zero_reuse": sum(1 for it in items if it["reuse_count"] == 0),
            "items": items,
        }

    async def asset_ledger(self) -> dict[str, Any]:
        """指标资产账本（P1：管理者视角的活跃/僵尸/重复建设清单）。

        僵尸判定**复用 HealthScorer 活跃度维度**（近 30 天无更新 → ``_ACTIVITY_STALE``）
        且零引用（``reuse_count == 0``）——长期无更新也无派生/报表引用的指标；
        重复建设以冲突预检挂载的 ``pending_conflict_detail``（conflict_type=
        ``same_def_diff_name``）为信号，不深入仲裁侧保持低耦合。

        Returns:
            ``{"total", "active_count", "zombie_count", "duplicate_count",
            "zombies", "duplicates"}``；僵尸明细含最后更新与被引用次数。
        """
        metrics = (
            await self._db.execute(select(Metric).where(Metric.deleted_at.is_(None)))
        ).scalars().all()
        reuse = await self._repo.metric_reuse_counts()
        scorer = HealthScorer(self._db)
        zombies: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        active = 0
        for metric in metrics:
            counts = reuse.get(metric.metric_code, {"derived_by": 0, "consumed_by": 0})
            reuse_count = counts["derived_by"] + counts["consumed_by"]
            # 复用 HealthScorer 活跃度维度（_calc_activity 仅读 updated_at，不触库）
            activity_score, _ = await scorer._calc_activity(metric)  # noqa: SLF001
            if activity_score == _ACTIVITY_STALE and reuse_count == 0:
                zombies.append(self._zombie_item(metric, counts, reuse_count))
            else:
                active += 1
            dup = self._duplicate_signal(metric)
            if dup is not None:
                duplicates.append(dup)
        return {
            "total": active + len(zombies),
            "active_count": active,
            "zombie_count": len(zombies),
            "duplicate_count": len(duplicates),
            "zombies": zombies,
            "duplicates": duplicates,
        }

    @staticmethod
    def _zombie_item(metric: Metric, counts: dict[str, int], reuse_count: int) -> dict[str, Any]:
        """僵尸指标明细（最后一次更新 + 被引用次数）。"""
        return {
            "metric_code": metric.metric_code,
            "name": metric.name,
            "domain": metric.domain,
            "type": metric.type,
            "status": metric.status,
            "last_updated_at": metric.updated_at.isoformat() if metric.updated_at else None,
            "days_since_update": _days_since(metric.updated_at),
            "derived_by_count": counts["derived_by"],
            "consumed_by_count": counts["consumed_by"],
            "reuse_count": reuse_count,
        }

    @staticmethod
    def _duplicate_signal(metric: Metric) -> dict[str, Any] | None:
        """重复建设信号：命中 SAME_DEF_DIFF_NAME 冲突预检标记时返回明细，否则 None。"""
        detail = metric.pending_conflict_detail or {}
        if not metric.pending_conflict or detail.get("conflict_type") != _DUPLICATE_CONFLICT_TYPE:
            return None
        return {
            "metric_code": metric.metric_code,
            "name": metric.name,
            "domain": metric.domain,
            "conflict_score": detail.get("score"),
            "existing_code": detail.get("existing_code"),
            "reason": detail.get("reason"),
        }
