"""冲突服务编排（TD §12.4 / FR-09）。

职责：四类冲突检测 + 仲裁状态机（OPEN→NEGOTIATING→ESCALATED→RULED→CLOSED）
+ 裁决知识库沉淀。不修改指标本身，仅写裁决结论 + 发通知。
PII 冲突特殊路由至 governance.pii_review，不进普通仲裁。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.exceptions import ConflictError, NotFoundError
from app.models.conflict import Conflict, ConflictStatus, ConflictType, RulingRecord
from app.services.conflict.events import ConflictEventPublisher
from app.services.conflict.llm_client import (
    ConflictLlmClient,
    DeterministicFallbackLlmClient,
)
from app.services.conflict.repository import ConflictRepository
from app.services.conflict.schemas import (
    ArbitrateRequest,
    ConflictCheckResult,
    DetectionOut,
    EscalateRequest,
    MetricInput,
)
from app.services.conflict.similarity import detect_conflict, is_borderline_match

logger = logging.getLogger("unisense.conflict.service")


def _new_conflict_id() -> str:
    return f"CF-{uuid.uuid4().hex[:12].upper()}"


class ConflictService(BaseService):
    def __init__(
        self,
        db: AsyncSession,
        events: ConflictEventPublisher | None = None,
        llm: ConflictLlmClient | None = None,
        metric_conflict_clearer: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(db)
        self._db = db
        self._repo = ConflictRepository(db)
        self._events = events or ConflictEventPublisher()
        # LLM 语义补位客户端：缺省为弃权实现（不调用外部服务，行为等价词法版）
        self._llm = llm or DeterministicFallbackLlmClient()
        # 跨服务一致性（TD §12.4）：仲裁/关闭后清除指标表 pending_conflict 标记的回调。
        # 由上层注入真实实现（更新 Metric 表）；None 时跳过（保持服务解耦、可测）。
        self._metric_conflict_clearer = metric_conflict_clearer

    async def _safe_publish(self, event: dict[str, Any]) -> None:
        """事件发布为 best-effort：通知/治理服务不可达时静默降级，不阻断主流程。

        优先经统一 EventBus 发布（供 notify 消费者落库投递），legacy HTTP 通道保留兼容。
        """
        try:
            event_type = event.get("event_type", "")
            if event_type:
                payload = {k: v for k, v in event.items() if k != "event_type"}
                await self._publish_event(event_type, payload)
        except Exception as exc:  # noqa: BLE001 - 事件降级，不向上抛
            logger.warning("conflict EventBus 发布失败（best-effort 跳过）：%s", exc)
        try:
            await self._events.publish(event)
        except Exception as exc:  # noqa: BLE001 - 事件降级，不向上抛
            logger.warning("conflict 事件发布失败（best-effort 跳过）：%s", exc)

    async def check(
        self, candidate: MetricInput, existing_list: list[MetricInput]
    ) -> ConflictCheckResult:
        detections: list[DetectionOut] = []
        blocked = False
        for ex in existing_list:
            cand_dict = candidate.model_dump()
            ext_dict = ex.model_dump()
            det = detect_conflict(cand_dict, ext_dict)
            # ---- LLM 语义补位（异步）----
            # 词法无冲突、但落入补位触发区且双方有定义时，调用 LLM 判定语义同义。
            if det is None and is_borderline_match(cand_dict, ext_dict):
                try:
                    confirmed = await self._llm.judge_same_semantics(
                        cand_dict.get("definition", ""), ext_dict.get("definition", "")
                    )
                except Exception as exc:  # noqa: BLE001 - LLM 异常降级为词法判定
                    logger.warning("冲突 LLM 补位失败（降级）：%s", exc)
                    confirmed = None
                if confirmed is True:
                    det = detect_conflict(cand_dict, ext_dict, llm_judge=lambda a, b: True)
            if det is None:
                continue
            detections.append(
                DetectionOut(
                    conflict_type=det.conflict_type,
                    score=det.score,
                    existing_code=det.existing_code,
                    existing_metric_id=det.existing_metric_id,
                    severity=det.severity,
                    block_publish=det.block_publish,
                    reason=det.reason,
                    llm_confirmed=det.llm_confirmed,
                )
            )
            if det.block_publish:
                blocked = True
            if det.conflict_type == ConflictType.PII:
                # 特殊路由：转交 governance.pii_review，不入普通冲突表
                await self._safe_publish(
                    {
                        "event_type": "pii_conflict",
                        "payload": {
                            "candidate": candidate.metric_code,
                            "existing": det.existing_code,
                            "domain": candidate.domain,
                        },
                    }
                )
                continue
            conflict = Conflict(
                conflict_id=_new_conflict_id(),
                type=det.conflict_type,
                status=ConflictStatus.OPEN,
                domain=candidate.domain or None,
                similarity_score=det.score,
                metric_codes={"candidate": candidate.metric_code, "existing": det.existing_code},
            )
            await self._repo.create(conflict)
            await self._safe_publish(
                {
                    "event_type": "conflict_open",
                    "payload": {
                        "conflict_id": conflict.conflict_id,
                        "type": det.conflict_type.value,
                        "domain": candidate.domain,
                        "score": det.score,
                    },
                }
            )
        return ConflictCheckResult(detections=detections, blocked=blocked)

    async def list_conflicts(self, params: Any) -> tuple[list[Conflict], int]:
        return await self._repo.list_conflicts(
            params.status, params.type, params.domain, params.page, params.page_size
        )

    async def get(self, conflict_id: str) -> Conflict:
        conflict = await self._repo.get_by_conflict_id(conflict_id)
        if conflict is None:
            raise NotFoundError(f"冲突不存在: {conflict_id}")
        return conflict

    async def arbitrate(
        self,
        conflict_id: str,
        req: ArbitrateRequest,
        *,
        actor_id: int | None = None,
    ) -> Conflict:
        conflict = await self.get(conflict_id)
        if conflict.status not in (
            ConflictStatus.OPEN,
            ConflictStatus.NEGOTIATING,
            ConflictStatus.ESCALATED,
        ):
            raise ConflictError(f"当前状态 {conflict.status.value} 不可裁决")
        # B1-2: 前端传 "ACCEPT"/"REJECT" 归一化为内部枚举
        decision = req.decision
        if decision in ("ACCEPT", "accept"):
            decision = "choose_canonical"
        elif decision in ("REJECT", "reject"):
            decision = "keep_diff"
        # PLAT-2: 以服务端认证身份 actor_id 为权威归因，忽略客户端伪造的 req.arbitrator_id
        arbitrator_id = actor_id if actor_id is not None else req.arbitrator_id
        decision_json = {
            "decision": decision,
            "canonical_metric_code": req.canonical_metric_code,
            "reason": req.reason,
            "rule_template": req.rule_template,
        }
        conflict = await self._repo.update_status(
            conflict,
            ConflictStatus.RULED,
            arbitrator_id=arbitrator_id,
            decision_json=decision_json,
            resolved=True,
        )
        await self._repo.create_ruling(
            RulingRecord(
                conflict_id=conflict.conflict_id,
                metric_codes=conflict.metric_codes,
                dispute_desc=f"{conflict.type.value}",
                decision=decision,
                reason=req.reason,
                arbitrator_id=arbitrator_id,
                decided_at=datetime.now(UTC),
            )
        )
        await self._safe_publish(
            {
                "event_type": "conflict_ruled",
                "payload": {
                    "conflict_id": conflict.conflict_id,
                    "canonical": req.canonical_metric_code,
                    "decision": decision,
                },
            }
        )
        await self._sync_metric_conflict_flag(conflict)
        return conflict

    async def _sync_metric_conflict_flag(self, conflict: Conflict) -> None:
        """仲裁/关闭成功后联动清除候选指标的 pending_conflict 冗余标记。

        仅当该候选指标不再有任何未决冲突时清除（避免误清仍关联其他冲突的指标）。
        best-effort：清除失败不阻断仲裁主流程（同事件发布降级语义），留日志告警。
        """
        if self._metric_conflict_clearer is None:
            return
        codes = conflict.metric_codes or {}
        candidate = codes.get("candidate")
        if not candidate:
            return
        try:
            remaining = await self._repo.count_open_for_metric(candidate)
            if remaining == 0:
                await self._metric_conflict_clearer(candidate)
                logger.info(
                    "metric_conflict_flag_cleared",
                    metric_code=candidate,
                    conflict_id=conflict.conflict_id,
                )
            else:
                logger.info(
                    "metric_conflict_flag_kept",
                    metric_code=candidate,
                    remaining_open_conflicts=remaining,
                )
        except Exception as exc:  # noqa: BLE001 - 联动清除降级，不阻断仲裁
            logger.warning("仲裁后同步指标冲突标记失败（best-effort 跳过）：%s", exc)

    async def escalate(self, conflict_id: str, req: EscalateRequest) -> Conflict:
        conflict = await self.get(conflict_id)
        if conflict.status not in (ConflictStatus.OPEN, ConflictStatus.NEGOTIATING):
            raise ConflictError(f"当前状态 {conflict.status.value} 不可升级")
        decision_json = conflict.decision_json or {}
        decision_json.setdefault("escalation_notes", []).append(req.note)
        conflict = await self._repo.update_status(
            conflict, ConflictStatus.ESCALATED, decision_json=decision_json
        )
        await self._safe_publish(
            {
                "event_type": "conflict_escalated",
                "payload": {"conflict_id": conflict.conflict_id, "note": req.note},
            }
        )
        return conflict

    async def close(self, conflict_id: str) -> Conflict:
        conflict = await self.get(conflict_id)
        if conflict.status != ConflictStatus.RULED:
            raise ConflictError(f"仅 RULED 状态可关闭，当前 {conflict.status.value}")
        conflict = await self._repo.update_status(conflict, ConflictStatus.CLOSED, resolved=True)
        # 兜底联动：覆盖历史冲突（旧代码仲裁时未清标记）关闭时再补清一次（幂等）
        await self._sync_metric_conflict_flag(conflict)
        return conflict

    async def get_rulings(self, conflict_id: str) -> list[RulingRecord]:
        return await self._repo.get_rulings(conflict_id)
