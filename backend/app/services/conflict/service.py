"""冲突服务编排（TD §12.4 / FR-09）。

职责：四类冲突检测 + 仲裁状态机（OPEN→NEGOTIATING→ESCALATED→RULED→CLOSED，
CLOSED→OPEN 可重新打开重审）+ 裁决知识库沉淀。仲裁结论写入 conflict 表，并通过
注入回调联动指标侧（落败方废弃/作废、胜方标记权威，见 conflict.arbitration），
保证口径收敛。PII 冲突特殊路由至 governance.pii_review，不进普通仲裁。
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
        metric_conflict_marker: Callable[[str, Conflict], Awaitable[None]] | None = None,
        # 仲裁联动指标回调（TD §12.4）：落败方废弃/作废、胜方标记权威；
        # 支持「保留差异+指定一方改名」时传入 rename_code（可选关键字参数）。
        # 由上层注入真实实现（见 conflict.arbitration.apply_arbitration_impact）；
        # None 时跳过（仲裁仍完成，仅不联动指标）。
        # Callable[..., ...] 放宽签名以兼容带/不带 rename_code 的实现。
        arbitration_applier: Callable[..., Awaitable[None]] | None = None,
        metric_archived_checker: Callable[[str], Awaitable[bool]] | None = None,
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
        # 对称的回置回调：重新打开已关闭冲突后，回置候选指标 pending_conflict 标记
        # （指标详情页须重新显示「口径冲突待处理」）。None 时跳过。
        self._metric_conflict_marker = metric_conflict_marker
        # 仲裁联动指标的回调（TD §12.4）：落败方废弃/作废、胜方标记权威。
        # 由上层注入真实实现（见 conflict.arbitration.apply_arbitration_impact）；
        # None 时跳过（仲裁仍完成，仅不联动指标）。
        self._arbitration_applier = arbitration_applier
        # 跨服务一致性：重新打开冲突（reopen）前置校验——关联指标是否已因仲裁
        # 被软删作废（deleted_at + successor）。仲裁联动把落败方作废后，冲突状态
        # 与指标状态必须同步：已作废指标无法再参与仲裁，reopen 应拒绝而非产生
        # 「OPEN 冲突引用已作废指标」的矛盾状态。None 时跳过（保持服务解耦）。
        self._metric_archived_checker = metric_archived_checker

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
        # 「保留差异+指定一方改名」（TD §12.4 扩展）：改名目标须是冲突双方之一。
        # 同名不同义冲突下 candidate/existing 的 metric_code 天然相同（检测以
        # cand_code==ext_code 触发），用 code 无法区分 → 优先以 rename_target 角色定位，
        # rename_metric_code 作为兼容旧调用的后备（仍校验须命中双方之一）。
        # 二者均未提供 → 无改名语义；仅 keep_diff 决策下可指定改名。
        codes = conflict.metric_codes or {}
        rename_code = req.rename_metric_code
        rename_target = req.rename_target
        if rename_target is not None:
            if rename_target not in ("candidate", "existing"):
                raise ConflictError(f"改名目标角色非法: {rename_target}")
            rename_code = codes.get(rename_target)
            if not rename_code:
                raise ConflictError(
                    f"冲突 {conflict.conflict_id} 缺少 {rename_target} 指标，无法指定改名"
                )
        if rename_code:
            if rename_code not in (codes.get("candidate"), codes.get("existing")):
                raise ConflictError(
                    f"改名目标 {rename_code} 不在冲突双方（候选/现有）中，无法指定改名"
                )
            if decision != "keep_diff":
                raise ConflictError(
                    "仅在「保留差异」决策下可指定一方改名，请选择保留差异后指定改名目标"
                )
        decision_json = {
            "decision": decision,
            "canonical_metric_code": req.canonical_metric_code,
            "reason": req.reason,
            "rule_template": req.rule_template,
        }
        if rename_code:
            decision_json["rename_metric_code"] = rename_code
            if rename_target is not None:
                decision_json["rename_target"] = rename_target
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
                    "rename_metric_code": rename_code,
                },
            }
        )
        await self._sync_metric_conflict_flag(conflict)
        await self._apply_arbitration(
            conflict, decision, req.canonical_metric_code, arbitrator_id, rename_code=rename_code
        )
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

    async def _apply_arbitration(
        self,
        conflict: Conflict,
        decision: str,
        canonical_code: str | None,
        arbitrator_id: int,
        *,
        rename_code: str | None = None,
    ) -> None:
        """仲裁成功后联动指标（TD §12.4）：落败方废弃/作废、胜方标记权威。

        由注入的 arbitration_applier 回调执行（API 层组合真实实现）；best-effort：
        联动失败不阻断仲裁主流程（仲裁结论已落库），留日志告警。
        """
        if self._arbitration_applier is None:
            return
        try:
            await self._arbitration_applier(
                conflict, decision, canonical_code, arbitrator_id, rename_code=rename_code
            )
            logger.info(
                "arbitration_metric_applied conflict_id=%s canonical=%s decision=%s rename_code=%s",
                conflict.conflict_id,
                canonical_code,
                decision,
                rename_code,
            )
        except Exception as exc:  # noqa: BLE001 - 联动降级，不阻断仲裁
            logger.warning("仲裁后联动指标失败（best-effort 跳过）：%s", exc)

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

    async def reopen(self, conflict_id: str) -> Conflict:
        """重新打开已关闭冲突为待处理（CLOSED → OPEN），供重新裁决。

        历史裁决记录保留在 ruling_record 作为知识库；候选指标的
        pending_conflict 标记对称回置，指标详情页重新显示「口径冲突待处理」。

        Raises:
            ConflictError: 关联指标已因仲裁作废时拒绝重新打开（跨服务一致性）。
        """
        conflict = await self.get(conflict_id)
        if conflict.status != ConflictStatus.CLOSED:
            raise ConflictError(f"仅 CLOSED 状态可重新打开，当前 {conflict.status.value}")
        await self._ensure_linked_metrics_active(conflict)
        conflict = await self._repo.reopen(conflict)
        await self._mark_metric_conflict(conflict)
        await self._safe_publish(
            {
                "event_type": "conflict_reopened",
                "payload": {
                    "conflict_id": conflict.conflict_id,
                    "candidate": (conflict.metric_codes or {}).get("candidate"),
                },
            }
        )
        return conflict

    async def _ensure_linked_metrics_active(self, conflict: Conflict) -> None:
        """reopen 前置校验：关联指标若已被仲裁作废（软删）则拒绝重新打开。

        跨服务一致性（TD §12.4）：冲突状态与指标状态须同步。仲裁联动已把
        落败方软删作废（successor 指向胜方）时，若把冲突拉回 OPEN 会产生
        「OPEN 冲突引用已作废指标」的矛盾状态——仲裁弹窗/对比将裸 404。
        已作废指标无法再参与仲裁，故拒绝 reopen，引导查看裁决记录知识库。
        """
        if self._metric_archived_checker is None:
            return
        codes = conflict.metric_codes or {}
        for role, code in (
            ("候选", codes.get("candidate")),
            ("现有", codes.get("existing")),
        ):
            if not code:
                continue
            try:
                archived = await self._metric_archived_checker(code)
            except Exception as exc:  # noqa: BLE001 - 校验失败降级放行，不阻断 reopen
                logger.warning("reopen 关联指标校验失败（降级放行）：%s", exc)
                continue
            if archived:
                raise ConflictError(
                    f"关联{role}指标 {code} 已因仲裁作废，无法重新打开；"
                    "历史裁决记录已沉淀在裁决记录知识库，可先恢复该指标或新建冲突。"
                )

    async def _mark_metric_conflict(self, conflict: Conflict) -> None:
        """重新打开冲突后联动回置候选指标的 pending_conflict 冗余标记。

        与仲裁/关闭时的清除对称：冲突重新打开为待处理，指标详情页须再次显示
        「口径冲突待处理」。best-effort：标记失败不阻断重新打开（同事件降级语义）。
        """
        if self._metric_conflict_marker is None:
            return
        codes = conflict.metric_codes or {}
        candidate = codes.get("candidate")
        if not candidate:
            return
        try:
            await self._metric_conflict_marker(candidate, conflict)
            logger.info(
                "metric_conflict_flag_marked",
                metric_code=candidate,
                conflict_id=conflict.conflict_id,
            )
        except Exception as exc:  # noqa: BLE001 - 联动回置降级，不阻断重新打开
            logger.warning("重新打开后同步指标冲突标记失败（best-effort 跳过）：%s", exc)

    async def get_rulings(self, conflict_id: str) -> list[RulingRecord]:
        return await self._repo.get_rulings(conflict_id)
