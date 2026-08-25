"""回填存量「口径冲突」孤儿标记 → conflict 表 OPEN 记录（Option C）。

背景：创建路径的自动冲突预检此前只挂 ``Metric.pending_conflict`` 标记、不落
``conflict`` 表（semantic/service.py 旧实现调 ConflictPrechecker.precheck），
导致「指标目录显示冲突待处理、仲裁模块为空」的孤儿标记——既不可仲裁、
又无法通过正常仲裁流程清除（清除依赖 conflict 表 count_open_for_metric）。

本脚本做**双向一致性回填**（幂等，可重复执行）：
1. **正向**：``pending_conflict=True`` 但无未决冲突记录的指标 → 按
   ``pending_conflict_detail`` 重建 conflict 表 OPEN 记录（source=backfill），
   并把 conflict_id 回填进 detail 供定位；
2. **反向**：conflict 表存在未决冲突、但关联活动指标未挂标记 → 回置标记 +
   从冲突行重建 detail（覆盖「手动预检落库、指标标记缺失」的不对称）。

用法:
    poetry run python -m scripts.backfill_conflict_orphans [--apply] [--limit N]

参数:
    --apply: 真正写库；缺省为 dry-run（只统计不改动）
    --limit: 仅处理前 N 个待回填指标（调试用，默认全部）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

# 将 backend/ 加入 sys.path，确保能 import app
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import structlog  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db.mysql import async_session_factory  # noqa: E402
from app.models.conflict import (  # noqa: E402
    Conflict,
    ConflictStatus,
    ConflictType,
)
from app.models.metric import Metric  # noqa: E402
from app.services.conflict.repository import ConflictRepository  # noqa: E402

logger = structlog.get_logger("unisense.backfill_conflict_orphans")

_OPEN_STATUSES = (
    ConflictStatus.OPEN,
    ConflictStatus.NEGOTIATING,
    ConflictStatus.ESCALATED,
)


def _new_conflict_id() -> str:
    return f"CF-{uuid.uuid4().hex[:12].upper()}"


def _derive_severity(detail: dict[str, Any], ctype: ConflictType) -> str:
    sev = (detail or {}).get("severity")
    if sev in ("hard", "soft"):
        return sev
    return "hard" if ctype == ConflictType.SAME_NAME_DIFF_DEF else "soft"


def _parse_ctype(raw: Any) -> ConflictType:
    if isinstance(raw, ConflictType):
        return raw
    try:
        return ConflictType(str(raw))
    except ValueError:
        return ConflictType.SAME_DEF_DIFF_NAME


def _detail_from_conflict(conflict: Conflict) -> dict[str, Any]:
    """从冲突行重建 pending_conflict_detail（反向回填用）。"""
    codes = conflict.metric_codes or {}
    return {
        "conflict_type": getattr(conflict.type, "value", None),
        "score": conflict.similarity_score,
        "existing_code": codes.get("existing"),
        "existing_metric_id": conflict.metric_b,
        "severity": conflict.severity or "soft",
        "block_publish": bool(conflict.block_publish),
        "reason": conflict.reason or "",
        "source": conflict.source or "manual",
        "conflict_id": conflict.conflict_id,
    }


async def _forward_pass(
    session: Any, repo: ConflictRepository, dry_run: bool, limit: int | None
) -> dict[str, int]:
    """正向：孤儿标记 → 建 conflict OPEN 记录（幂等：已有未决记录则跳过）。"""
    stmt = select(Metric).where(
        Metric.deleted_at.is_(None), Metric.pending_conflict.is_(True)
    )
    metrics = list((await session.execute(stmt)).scalars().all())
    if limit:
        metrics = metrics[:limit]

    stats = {"scanned": len(metrics), "created": 0, "skipped_has_open": 0}
    for metric in metrics:
        code = metric.metric_code
        # 已有未决记录（含人工预检/历史自动）→ 一致，跳过
        if await repo.count_open_for_metric(code) > 0:
            stats["skipped_has_open"] += 1
            continue
        detail = dict(metric.pending_conflict_detail or {})
        ctype = _parse_ctype(detail.get("conflict_type"))
        conflict = Conflict(
            conflict_id=_new_conflict_id(),
            type=ctype,
            status=ConflictStatus.OPEN,
            domain=metric.domain or None,
            similarity_score=float(detail.get("score") or 0.0),
            metric_codes={
                "candidate": code,
                "existing": detail.get("existing_code") or "",
            },
            severity=_derive_severity(detail, ctype),
            source="backfill",
            reason=detail.get("reason") or "",
            block_publish=bool(detail.get("block_publish", ctype == ConflictType.SAME_NAME_DIFF_DEF)),
            metric_a=metric.id,
            metric_b=detail.get("existing_metric_id"),
        )
        detail["conflict_id"] = conflict.conflict_id
        detail["source"] = "backfill"
        stats["created"] += 1
        if not dry_run:
            await repo.create(conflict)
            metric.pending_conflict_detail = detail
    return stats


async def _reverse_pass(
    session: Any, repo: ConflictRepository, dry_run: bool
) -> dict[str, int]:
    """反向：未决冲突引用的活动指标未挂标记 → 回置标记 + 重建 detail。"""
    stmt = (
        select(Conflict)
        .where(Conflict.deleted_at.is_(None), Conflict.status.in_(_OPEN_STATUSES))
        .order_by(Conflict.created_at.desc())
    )
    conflicts = list((await session.execute(stmt)).scalars().all())

    # 收集涉及的指标编码，批量查询活动指标（避免 N+1）
    codes: set[str] = set()
    for c in conflicts:
        mc = c.metric_codes or {}
        for v in (mc.get("candidate"), mc.get("existing")):
            if v:
                codes.add(v)
    if not codes:
        return {"scanned": 0, "flagged": 0}
    metric_rows = list(
        (
            await session.execute(
                select(Metric).where(
                    Metric.metric_code.in_(codes), Metric.deleted_at.is_(None)
                )
            )
        ).scalars().all()
    )
    by_code = {m.metric_code: m for m in metric_rows}

    stats = {"scanned": len(conflicts), "flagged": 0}
    for conflict in conflicts:
        detail = _detail_from_conflict(conflict)
        mc = conflict.metric_codes or {}
        for code in (mc.get("candidate"), mc.get("existing")):
            metric = by_code.get(code)
            if metric is None or metric.pending_conflict:
                continue
            stats["flagged"] += 1
            if not dry_run:
                metric.pending_conflict = True
                metric.pending_conflict_detail = detail
    return stats


async def run(apply: bool, limit: int | None) -> None:
    async with async_session_factory() as session:
        repo = ConflictRepository(session)
        forward = await _forward_pass(session, repo, not apply, limit)
        reverse = await _reverse_pass(session, repo, not apply)
        if apply:
            await session.commit()
        logger.info(
            "backfill_conflict_orphans_done",
            forward=forward,
            reverse=reverse,
            apply=apply,
        )

    from app.db.mysql import engine as db_engine

    await db_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="回填存量口径冲突孤儿标记")
    parser.add_argument("--apply", action="store_true", help="真正写库（缺省 dry-run）")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 个指标")
    args = parser.parse_args()
    asyncio.run(run(args.apply, args.limit))


if __name__ == "__main__":
    main()
