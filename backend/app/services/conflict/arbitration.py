"""仲裁裁决联动指标（TD §12.4「落败方 metric 转别名/废弃，胜方标 canonical」）。

冲突仲裁原本只写 conflict 表结论、不动指标本身，导致落败口径仍以原名被
消费方查询引用，口径分裂风险未根治。本模块把仲裁结果落到指标侧，与
conflict.service 解耦（由 API 层注入回调），并抽为纯函数便于单测。

决策语义（以 canonical_code 判定胜负，decision 仅作标记溯源）：
- canonical_code = 现有编码 → 选现有为权威 / 合并到现有，候选落败
- canonical_code = 候选编码 → 选候选为权威，现有落败
- canonical_code 为空（keep_diff）→ 双方保留，标记「已裁定共存」，不废弃任何一方

落败方处置（保证消费侧口径收敛）：
- PUBLISHED   → deprecate（DEPRECATED + successor=胜方 + sunset，发 metric.deprecated
                事件，消费方经详情页 DeprecatedChain 自动引导到胜方）
- DRAFT/REVIEW/EXPERIMENTAL（从未生效）→ 软删作废
- DEPRECATED / 已软删 → 跳过（幂等）

胜方处置：写 arbitration_mark（status=canonical），详情页展示「权威口径」。
keep_diff：双方写 arbitration_mark（status=coexist）。

强韧性保护：
- 胜方未 PUBLISHED 时不废弃 PUBLISHED 落败方（避免产生无替代指标的废弃）。
- 全部 best-effort：任一步失败仅告警，不阻断仲裁主流程（仲裁结论已落库）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conflict import Conflict
from app.models.metric import Metric

logger = logging.getLogger("unisense.conflict.arbitration")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _invalidate_affected(metric_svc: Any | None, codes: list[str]) -> None:
    """失效受影响指标的读缓存（best-effort）。

    仲裁联动对指标做了软删/废弃/标记，须主动失效 Redis 缓存，否则详情/健康
    读接口（cache-aside）会继续返回旧数据，与 DB 不一致（时效性/一致性要求）。
    """
    if not codes:
        return
    invalidator = getattr(metric_svc, "invalidate_cache", None)
    if invalidator is None:
        return
    try:
        await invalidator(list(dict.fromkeys(codes)))
    except Exception:
        logger.warning("arbitration_cache_invalidate_failed", codes=codes)


async def _write_mark(db: AsyncSession, metric_code: str, mark: dict[str, Any]) -> None:
    """写仲裁裁决标记（幂等，多次裁决后次覆盖为最新）。"""
    await db.execute(
        update(Metric)
        .where(Metric.metric_code == metric_code, Metric.deleted_at.is_(None))
        .values(arbitration_mark=mark)
    )


async def apply_arbitration_impact(
    db: AsyncSession,
    conflict: Conflict,
    decision: str,
    canonical_code: str | None,
    actor_id: int,
    metric_svc: Any | None = None,
) -> None:
    """仲裁裁决联动指标（best-effort，失败仅告警不阻断仲裁）。

    Args:
        db: 异步数据库会话（与仲裁同一事务，随端点 commit 一并落库）。
        conflict: 已裁决的冲突对象（RULED）。
        decision: 裁决决策（choose_canonical / merge / keep_diff 等，仅作标记溯源）。
        canonical_code: 权威方指标编码；None 表示保留差异（双方共存）。
        actor_id: 仲裁人 ID（服务端认证身份）。
        metric_svc: 指标服务实例；落败方 PUBLISHED 废弃时用于 deprecate_metric。
            为 None 时跳过废弃（其余标记/作废仍执行）。
    """
    codes = conflict.metric_codes or {}
    candidate = codes.get("candidate")
    existing = codes.get("existing")
    if not candidate or not existing:
        logger.warning("仲裁联动跳过：冲突 %s 缺少候选/现有指标", conflict.conflict_id)
        return
    conflict_id = conflict.conflict_id

    # ---- keep_diff：双方保留，标记已裁定共存 ----
    if not canonical_code:
        for code in (candidate, existing):
            await _write_mark(
                db,
                code,
                {
                    "status": "coexist",
                    "conflict_id": conflict_id,
                    "decision": decision,
                    "ruled_at": _now_iso(),
                    "opposite_code": existing if code == candidate else candidate,
                },
            )
        await _invalidate_affected(metric_svc, [candidate, existing])
        return

    # ---- 确定胜方/落败方 ----
    if canonical_code not in (candidate, existing):
        logger.warning(
            "仲裁联动跳过：权威方 %s 不在冲突双方 %s/%s 中",
            canonical_code,
            candidate,
            existing,
        )
        return
    winner_code = canonical_code
    loser_code = existing if canonical_code == candidate else candidate

    if winner_code == loser_code:
        # 自我冲突（existing == candidate 同码）：胜方即落败方。
        # 仅标记权威，不作废唯一指标——否则会把胜方自己软删作废，产生
        # successor 指向自身的死循环（曾致真实胜方指标被误删、详情引导失效）。
        await _write_mark(
            db,
            winner_code,
            {
                "status": "canonical",
                "conflict_id": conflict_id,
                "decision": decision,
                "ruled_at": _now_iso(),
                "opposite_code": loser_code,
            },
        )
        logger.warning(
            "仲裁联动跳过落败方处置：冲突 %s 双方同码 %s（自我冲突），仅标记权威",
            conflict_id,
            winner_code,
        )
        await _invalidate_affected(metric_svc, [winner_code])
        return

    winner = (
        await db.execute(
            select(Metric).where(
                Metric.metric_code == winner_code, Metric.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    loser = (
        await db.execute(
            select(Metric).where(Metric.metric_code == loser_code, Metric.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if winner is None or loser is None:
        logger.warning("仲裁联动跳过：胜方/落败方指标不存在（%s/%s）", winner_code, loser_code)
        return

    # ---- 胜方标记权威口径 ----
    await _write_mark(
        db,
        winner_code,
        {
            "status": "canonical",
            "conflict_id": conflict_id,
            "decision": decision,
            "ruled_at": _now_iso(),
            "opposite_code": loser_code,
        },
    )

    # ---- 落败方处置 ----
    if loser.status == "DEPRECATED":
        await _invalidate_affected(metric_svc, [winner_code])
        return
    if loser.status == "PUBLISHED":
        if winner.status != "PUBLISHED":
            # 强韧性保护：胜方未发布时废弃 PUBLISHED 落败方会产生"无替代指标的废弃"，
            # 消费方无处可去，故跳过，待胜方发布后由 Owner 手动废弃。
            logger.warning(
                "仲裁联动：胜方 %s 未发布（%s），不废弃 PUBLISHED 落败方 %s（避免无替代指标）",
                winner_code,
                winner.status,
                loser_code,
            )
            await _invalidate_affected(metric_svc, [winner_code])
            return
        if metric_svc is None:
            logger.warning("仲裁联动：缺少 metric_svc，跳过落败方 %s 废弃", loser_code)
            await _invalidate_affected(metric_svc, [winner_code])
            return
        # 以平台治理权限执行落败方废弃（仲裁本身为 GOV 动作；successor 指向胜方）
        await metric_svc.deprecate_metric(
            loser_code, winner_code, actor_id, role="platform_admin"
        )
        await _invalidate_affected(metric_svc, [winner_code, loser_code])
        return
    # 未发布（DRAFT/REVIEW/EXPERIMENTAL）：从未生效，软删作废。
    # 同条 UPDATE 补写 successor_code（指向胜方）与 defeated 仲裁标记——
    # 否则落败方被软删后无任何指向胜方的指针，详情直访只能给出裸 404，
    # 消费方/仲裁人无处可去（TD §12.4「落败方 metric 转别名/废弃」的可寻址落地）。
    await db.execute(
        update(Metric)
        .where(Metric.id == loser.id, Metric.deleted_at.is_(None))
        .values(
            deleted_at=datetime.now(UTC),
            successor_code=winner_code,
            arbitration_mark={
                "status": "defeated",
                "conflict_id": conflict_id,
                "decision": decision,
                "ruled_at": _now_iso(),
                "opposite_code": winner_code,
            },
        )
    )
    logger.info(
        "arbitration_metric_voided",
        metric_code=loser_code,
        conflict_id=conflict_id,
        status=loser.status,
        successor_code=winner_code,
    )
    await _invalidate_affected(metric_svc, [winner_code, loser_code])
