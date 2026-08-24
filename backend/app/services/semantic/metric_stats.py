"""指标复用度 / 资产账本统计服务（数仓视角治理）。

对齐 TD §12.3 血缘复用与指标资产健康：复用血缘边（DERIVED_FROM / CONSUMED_BY）
量化「原子指标 → 派生指标 → 报表引用」链路的复用度，并复用 HealthScorer 的
活跃度维度识别僵尸指标（长期无更新 + 零引用）与冲突预检的重复建设信号。

数据访问复用 ``LineageRepository``（血缘边的权威存储查询），不新造轮子。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric import Metric
from app.services.lineage.repository import LineageRepository


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
