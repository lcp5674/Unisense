"""质量自动检测调度任务（FR-10 闭环：观测采集与规则评估解耦）。

一期 detect 仅由手动 API（POST /events/detect）触发，生产上无人巡检时
异常事件不会自动产生。本任务提供周期自动评估：

    - 扫描全部启用规则（quality_rule.enabled=true）；
    - 按 (metric_id, rule_type) 去重；
    - 对每个组合取该指标最近一次观测值（quality_observation，由采集/产出
      分区就绪时写入），落在评估窗口内则调用 QualityService.detect 自动评估；
    - 命中则落 QualityEvent（OPEN）+ 触发告警（EventBus → notify），
      与手动检测共用同一检测引擎，保证结果语义一致。

观测缺失（冷启动）不视为异常，仅计数跳过，避免空库误报。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

logger = structlog.get_logger("unisense.quality.tasks")

#: 观测值新鲜度窗口：超过该时长未写入新观测则跳过评估（避免用陈旧值误报）。
_OBS_FRESH_WINDOW = timedelta(hours=48)


async def run_quality_checks(ctx: dict[str, Any]) -> dict[str, int]:
    """周期质量检测：扫描启用规则并用最近观测自动评估。

    Args:
        ctx: arq worker 上下文（本任务自建会话，仅用日志）。

    Returns:
        ``{combos, evaluated, triggered, skipped_no_obs}`` 统计。
    """
    from decimal import Decimal

    from app.db.mysql import async_session_factory
    from app.services.quality.repository import QualityRepository
    from app.services.quality.service import QualityService

    async with async_session_factory() as db:
        repo = QualityRepository(db)
        rules = await repo.list_all_enabled_rules()
        # 去重 (metric_id, rule_type) 组合，避免同指标同类型重复评估
        combos = sorted({(r.metric_id, r.rule_type) for r in rules})

        now = datetime.now(UTC)
        evaluated = 0
        triggered = 0
        skipped_no_obs = 0

        for metric_id, rule_type in combos:
            # 取该指标最近一次观测（升序，取末条为最新）
            observations = await repo.list_recent_observations(metric_id, limit=50)
            latest = observations[-1] if observations else None
            if latest is None or latest.obs_time < now - _OBS_FRESH_WINDOW:
                skipped_no_obs += 1
                continue
            try:
                svc = QualityService(db)
                event = await svc.detect(
                    metric_id,
                    rule_type,
                    Decimal(str(latest.value)),
                    rule_mode=None,
                )
                evaluated += 1
                if event is not None:
                    triggered += 1
                    rt = rule_type.value if hasattr(rule_type, "value") else str(rule_type)
                    lv = event.level.value if hasattr(event.level, "value") else str(event.level)
                    logger.info(
                        "quality_auto_detect_triggered",
                        metric_id=metric_id,
                        rule_type=rt,
                        level=lv,
                    )
            except Exception as exc:  # noqa: BLE001 - 单组合失败不阻断整轮扫描
                logger.warning(
                    "quality_auto_detect_failed",
                    metric_id=metric_id,
                    rule_type=str(rule_type),
                    error=str(exc),
                )

        await db.commit()

    logger.info(
        "quality_checks_done",
        combos=len(combos),
        evaluated=evaluated,
        triggered=triggered,
        skipped_no_obs=skipped_no_obs,
    )
    return {
        "combos": len(combos),
        "evaluated": evaluated,
        "triggered": triggered,
        "skipped_no_obs": skipped_no_obs,
    }
