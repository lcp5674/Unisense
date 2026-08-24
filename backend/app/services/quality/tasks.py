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
            # 取该指标最近一次观测（最新，非升序前 N 条的末条）
            latest = await repo.latest_observation(metric_id)
            if latest is None:
                skipped_no_obs += 1
                continue
            # MySQL DATETIME(timezone=True) 读出为 naive：统一按 UTC 解释再与 aware now 比较，
            # 避免 offset-naive vs offset-aware TypeError（曾导致整轮自动检测崩溃）。
            obs_time = latest.obs_time
            if obs_time.tzinfo is None:
                obs_time = obs_time.replace(tzinfo=UTC)
            if obs_time < now - _OBS_FRESH_WINDOW:
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


async def run_reconciliation_checks(
    ctx: dict[str, Any],
    *,
    period_days: int = 7,
    tier2_ratio: float = 0.3,
    max_targets: int = 100,
) -> dict[str, int]:
    """对账触发任务：扫描已 bind benchmark 且超过对账周期的指标，生成待对账提醒。

    仅做调度触发与提醒，不执行外部数据对比（外部对比需真实数据源接入）；
    T1 全量、T2/T3 按比例抽样，命中则发布 ``reconciliation.due`` 事件
    （EventBus → notify），与手动对账链路（import_benchmark → bind →
    run_reconciliation → confirm）解耦互补。
    """
    from app.db.mysql import async_session_factory
    from app.services.quality.repository import QualityRepository
    from app.services.quality.service import QualityService

    cutoff = datetime.now(UTC) - timedelta(days=period_days)
    async with async_session_factory() as db:
        repo = QualityRepository(db)
        due_ids = set(await repo.list_due_benchmark_ids(cutoff))
        svc = QualityService(db)
        targets = await svc.sample_reconciliation_targets(
            tier2_ratio=tier2_ratio,
            due_benchmark_ids=due_ids,
            max_targets=max_targets,
        )
        reminded = 0
        for target in targets:
            # 生成待对账提醒（best-effort，publisher 内部熔断降级）
            await svc._publisher.publish(  # noqa: SLF001 - 同模块编排任务访问服务内部发布器
                {
                    "event_type": "reconciliation.due",
                    "benchmark_id": target["benchmark_id"],
                    "metric_code": target["metric_code"],
                    "metric_tier": target["metric_tier"],
                }
            )
            reminded += 1
        await db.commit()

    logger.info(
        "reconciliation_checks_done",
        due=len(due_ids),
        sampled=len(targets),
        reminded=reminded,
    )
    return {"due": len(due_ids), "sampled": len(targets), "reminded": reminded}
