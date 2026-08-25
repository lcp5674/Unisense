"""冲突仲裁 owner 定向通知单测（conflict.api 辅助函数，无真实 DB）。

覆盖 TD §12.4 两条通知链路：
1. 「保留差异+指定一方改名」→ 通知被改名方 Owner。rename_metric_code 以
   service 层解析后的 decision_json 为准——前端传 rename_target（角色）或
   rename_metric_code（编码）均已被 service 归一化写入 decision_json。
2. 「选权威」→ 通知落败方 Owner：DEPRECATED → metric.deprecated（已废弃）、
   软删（deleted_at 非空）→ metric.voided（已作废）；强韧性保护（未实际处置）
   与自我冲突 → 不通知。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.api import conflict as conflict_api
from app.models.conflict import Conflict, ConflictStatus, ConflictType
from app.models.metric import Metric
from app.services.conflict.schemas import ArbitrateRequest


def _conflict(
    candidate: str, existing: str, decision_json: dict[str, Any] | None = None
) -> Conflict:
    return Conflict(
        conflict_id="CF-TEST",
        type=ConflictType.SAME_NAME_DIFF_DEF,
        status=ConflictStatus.RULED,
        metric_codes={"candidate": candidate, "existing": existing},
        decision_json=decision_json,
    )


def _metric(
    code: str,
    status: str,
    owner_id: int | None = 7,
    deleted_at: Any = None,
) -> Metric:
    return Metric(metric_code=code, status=status, owner_id=owner_id, deleted_at=deleted_at)


class FakeResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class FakeDb:
    """最小假 DB：execute 恒返回预设行（模拟 select Metric）。"""

    def __init__(self, row: Any) -> None:
        self._row = row

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> FakeResult:
        return FakeResult(self._row)


class TestNotifyArbitrationOwners:
    """编排层：改名 + 落败方通知的触发条件（patch 两个叶子通知函数）。"""

    async def test_rename_from_decision_json_triggers_rename_notify(self) -> None:
        """decision_json 含 rename_metric_code（rename_target 已被 service 归一化）→ 触发改名通知。

        回归：修复前 API 层只判断原始请求体 payload.rename_metric_code，
        前端传 rename_target 时该字段为 None → 改名通知永不触发。
        """
        conflict = _conflict(
            "cand", "exist", {"rename_metric_code": "cand", "rename_target": "candidate"}
        )
        payload = ArbitrateRequest(decision="keep_diff")
        with (
            patch.object(conflict_api, "_notify_rename_owner", new=AsyncMock()) as rename,
            patch.object(conflict_api, "_notify_loser_owner", new=AsyncMock()) as loser,
        ):
            await conflict_api._notify_arbitration_owners(FakeDb(None), conflict, payload, "tid")
        rename.assert_awaited_once()
        assert rename.await_args.args[1:] == ("cand", "CF-TEST", "tid")
        loser.assert_not_awaited()

    async def test_choose_existing_notifies_candidate_loser(self) -> None:
        """选现有为权威 → 落败方=候选，通知候选 owner。"""
        conflict = _conflict("cand", "exist")
        payload = ArbitrateRequest(decision="choose_canonical", canonical_metric_code="exist")
        with (
            patch.object(conflict_api, "_notify_rename_owner", new=AsyncMock()) as rename,
            patch.object(conflict_api, "_notify_loser_owner", new=AsyncMock()) as loser,
        ):
            await conflict_api._notify_arbitration_owners(FakeDb(None), conflict, payload, "tid")
        rename.assert_not_awaited()
        loser.assert_awaited_once()
        assert loser.await_args.args[1:] == ("cand", "exist", "CF-TEST", "tid")

    async def test_choose_candidate_notifies_existing_loser(self) -> None:
        """选候选为权威 → 落败方=现有，通知现有 owner。"""
        conflict = _conflict("cand", "exist")
        payload = ArbitrateRequest(decision="choose_canonical", canonical_metric_code="cand")
        with (
            patch.object(conflict_api, "_notify_rename_owner", new=AsyncMock()) as rename,
            patch.object(conflict_api, "_notify_loser_owner", new=AsyncMock()) as loser,
        ):
            await conflict_api._notify_arbitration_owners(FakeDb(None), conflict, payload, "tid")
        rename.assert_not_awaited()
        loser.assert_awaited_once()
        assert loser.await_args.args[1:] == ("exist", "cand", "CF-TEST", "tid")

    async def test_keep_diff_no_loser_notify(self) -> None:
        """keep_diff（canonical 空）→ 无落败方，仅可能改名通知。"""
        conflict = _conflict("cand", "exist")
        payload = ArbitrateRequest(decision="keep_diff")
        with (
            patch.object(conflict_api, "_notify_rename_owner", new=AsyncMock()) as rename,
            patch.object(conflict_api, "_notify_loser_owner", new=AsyncMock()) as loser,
        ):
            await conflict_api._notify_arbitration_owners(FakeDb(None), conflict, payload, "tid")
        rename.assert_not_awaited()
        loser.assert_not_awaited()

    async def test_canonical_not_in_pair_skipped(self) -> None:
        """权威方不在冲突双方 → 不触发任何通知。"""
        conflict = _conflict("cand", "exist")
        payload = ArbitrateRequest(decision="choose_canonical", canonical_metric_code="other")
        with (
            patch.object(conflict_api, "_notify_rename_owner", new=AsyncMock()) as rename,
            patch.object(conflict_api, "_notify_loser_owner", new=AsyncMock()) as loser,
        ):
            await conflict_api._notify_arbitration_owners(FakeDb(None), conflict, payload, "tid")
        rename.assert_not_awaited()
        loser.assert_not_awaited()

    async def test_self_conflict_no_loser_notify(self) -> None:
        """自我冲突（双方同码）→ 无独立落败方，不通知。"""
        conflict = _conflict("same", "same")
        payload = ArbitrateRequest(decision="choose_canonical", canonical_metric_code="same")
        with (
            patch.object(conflict_api, "_notify_rename_owner", new=AsyncMock()) as rename,
            patch.object(conflict_api, "_notify_loser_owner", new=AsyncMock()) as loser,
        ):
            await conflict_api._notify_arbitration_owners(FakeDb(None), conflict, payload, "tid")
        rename.assert_not_awaited()
        loser.assert_not_awaited()


class TestNotifyLoserOwner:
    """落败方状态判定：DEPRECATED / 软删 → 通知；强韧性保护/缺失 → 不通知。"""

    async def test_deprecated_loser_notifies_owner(self) -> None:
        """落败方 PUBLISHED 被废弃（status=DEPRECATED）→ metric.deprecated 定向通知 owner。"""
        row = _metric("cand", "DEPRECATED", owner_id=7)
        with patch("app.services.notify.service.NotifyService") as ns:
            ns.return_value.notify_user = AsyncMock()
            await conflict_api._notify_loser_owner(FakeDb(row), "cand", "exist", "CF-TEST", "tid")
        ns.return_value.notify_user.assert_awaited_once()
        kwargs = ns.return_value.notify_user.await_args.kwargs
        assert kwargs["user_id"] == 7
        assert kwargs["event_type"] == "metric.deprecated"
        assert kwargs["channel"] == "IN_APP"
        assert kwargs["payload"]["successor_code"] == "exist"

    async def test_voided_loser_notifies_owner(self) -> None:
        """落败方 DRAFT 软删作废（deleted_at 非空）→ metric.voided 定向通知 owner。"""
        row = _metric("cand", "DRAFT", owner_id=7, deleted_at=datetime.now(UTC))
        with patch("app.services.notify.service.NotifyService") as ns:
            ns.return_value.notify_user = AsyncMock()
            await conflict_api._notify_loser_owner(FakeDb(row), "cand", "exist", "CF-TEST", "tid")
        ns.return_value.notify_user.assert_awaited_once()
        kwargs = ns.return_value.notify_user.await_args.kwargs
        assert kwargs["user_id"] == 7
        assert kwargs["event_type"] == "metric.voided"
        assert kwargs["channel"] == "IN_APP"

    async def test_unaffected_loser_not_notified(self) -> None:
        """强韧性保护：胜方未发布、落败方仍 PUBLISHED（未处置）→ 不通知。"""
        row = _metric("cand", "PUBLISHED", owner_id=7)
        with patch("app.services.notify.service.NotifyService") as ns:
            ns.return_value.notify_user = AsyncMock()
            await conflict_api._notify_loser_owner(FakeDb(row), "cand", "exist", "CF-TEST", "tid")
        ns.return_value.notify_user.assert_not_awaited()

    async def test_missing_owner_not_notified(self) -> None:
        """落败方指标无 owner → 不通知。"""
        row = _metric("cand", "DEPRECATED", owner_id=None)
        with patch("app.services.notify.service.NotifyService") as ns:
            ns.return_value.notify_user = AsyncMock()
            await conflict_api._notify_loser_owner(FakeDb(row), "cand", "exist", "CF-TEST", "tid")
        ns.return_value.notify_user.assert_not_awaited()

    async def test_missing_metric_not_notified(self) -> None:
        """落败方指标不存在 → 不通知。"""
        with patch("app.services.notify.service.NotifyService") as ns:
            ns.return_value.notify_user = AsyncMock()
            await conflict_api._notify_loser_owner(FakeDb(None), "ghost", "exist", "CF-TEST", "tid")
        ns.return_value.notify_user.assert_not_awaited()


class TestNotifyReopenOwners:
    """冲突重开定向通知：双方指标 Owner 各收一条 conflict_reopened（IN_APP 不依赖订阅）。"""

    async def test_both_owners_notified(self) -> None:
        """candidate/existing 双方 owner 各收一条 conflict_reopened。"""
        row = _metric("cand", "PUBLISHED", owner_id=7)
        with patch("app.services.notify.service.NotifyService") as ns:
            ns.return_value.notify_user = AsyncMock()
            conflict = _conflict("cand", "exist")
            await conflict_api._notify_reopen_owners(FakeDb(row), conflict, "tid")
        assert ns.return_value.notify_user.await_count == 2
        for call in ns.return_value.notify_user.await_args_list:
            kwargs = call.kwargs
            assert kwargs["event_type"] == "conflict_reopened"
            assert kwargs["user_id"] == 7
            assert kwargs["channel"] == "IN_APP"
            assert kwargs["payload"]["conflict_id"] == "CF-TEST"
            assert kwargs["payload"]["metric_code"] in ("cand", "exist")

    async def test_owner_missing_skips_that_side(self) -> None:
        """指标无 owner → 该侧不通知（FakeDb 恒返回同一 row → 两侧都跳过）。"""
        row = _metric("cand", "PUBLISHED", owner_id=None)
        with patch("app.services.notify.service.NotifyService") as ns:
            ns.return_value.notify_user = AsyncMock()
            conflict = _conflict("cand", "exist")
            await conflict_api._notify_reopen_owners(FakeDb(row), conflict, "tid")
        ns.return_value.notify_user.assert_not_awaited()

    async def test_metric_missing_not_notified(self) -> None:
        """指标不存在 → 不通知。"""
        with patch("app.services.notify.service.NotifyService") as ns:
            ns.return_value.notify_user = AsyncMock()
            conflict = _conflict("cand", "exist")
            await conflict_api._notify_reopen_owners(FakeDb(None), conflict, "tid")
        ns.return_value.notify_user.assert_not_awaited()

    async def test_empty_metric_codes_not_notified(self) -> None:
        """metric_codes 为空 → 无 code 可查，不通知。"""
        conflict = _conflict("cand", "exist")
        conflict.metric_codes = None
        with patch("app.services.notify.service.NotifyService") as ns:
            ns.return_value.notify_user = AsyncMock()
            await conflict_api._notify_reopen_owners(
                FakeDb(_metric("cand", "PUBLISHED", owner_id=7)), conflict, "tid"
            )
        ns.return_value.notify_user.assert_not_awaited()


class TestCheckConflictAutoLoad:
    """P0-A：手动"冲突预检"existing 为空时服务端自动加载，杜绝空转。

    修复前前端 handlePrecheck 只传 candidate（existing 缺省空列表），check 遍历
    空列表永远返回"未检测到冲突"——用户主动求证的入口形同虚设。
    """

    async def test_existing_empty_auto_loads_active_metrics(self) -> None:
        """existing 为空 → 调用 MetricService.load_conflict_existing 并传入 check。"""
        from app.api.conflict import check_conflict
        from app.services.conflict.schemas import (
            ConflictCheckRequest,
            ConflictCheckResult,
            MetricInput,
        )
        from app.services.semantic.service import MetricService

        candidate = MetricInput(
            metric_code="new_metric_day", domain="sales", definition="sum(amount)"
        )
        payload = ConflictCheckRequest(candidate=candidate)  # existing 缺省空
        loaded = [
            MetricInput(
                metric_code="existing_metric_day", domain="sales", definition="sum(amount)"
            )
        ]
        svc = AsyncMock()
        svc.check = AsyncMock(return_value=ConflictCheckResult(detections=[], blocked=False))
        db = MagicMock()
        db.commit = AsyncMock()
        with (
            patch.object(conflict_api, "_svc", return_value=svc),
            patch.object(
                MetricService, "load_conflict_existing", AsyncMock(return_value=loaded)
            ) as loader,
        ):
            resp = await check_conflict(
                payload, request=MagicMock(), db=db, user=MagicMock(id=1), trace_id="tid"
            )
        loader.assert_awaited_once()
        # check 收到自动加载的 existing（而非空列表）
        assert svc.check.await_args.args[1] == loaded
        assert resp.data is not None

    async def test_existing_provided_does_not_auto_load(self) -> None:
        """调用方已传 existing → 不自动加载（保留显式对比对象语义）。"""
        from app.api.conflict import check_conflict
        from app.services.conflict.schemas import (
            ConflictCheckRequest,
            ConflictCheckResult,
            MetricInput,
        )
        from app.services.semantic.service import MetricService

        candidate = MetricInput(
            metric_code="new_metric_day", domain="sales", definition="sum(amount)"
        )
        provided = [MetricInput(metric_code="other_day", domain="sales", definition="x")]
        payload = ConflictCheckRequest(candidate=candidate, existing=provided)
        svc = AsyncMock()
        svc.check = AsyncMock(return_value=ConflictCheckResult(detections=[], blocked=False))
        db = MagicMock()
        db.commit = AsyncMock()
        with (
            patch.object(conflict_api, "_svc", return_value=svc),
            patch.object(MetricService, "load_conflict_existing", AsyncMock()) as loader,
        ):
            resp = await check_conflict(
                payload, request=MagicMock(), db=db, user=MagicMock(id=1), trace_id="tid"
            )
        loader.assert_not_awaited()
        assert svc.check.await_args.args[1] == provided
        assert resp.data is not None
