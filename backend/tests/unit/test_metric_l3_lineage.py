"""L3 指标血缘自动接线单测（metrics.py 的 _register_metric_l3_lineage）。

覆盖：
- 口径定义含 source_table/source_tables → 调用 LineageService 注册（commit=False）
- 无有效定义 → 仍尝试注册（register 内部对空表返回 []，此处验证调用语义）
- 注册抛异常 → 不阻断主流程（try/except 吞掉并记日志）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.api.metrics import _register_metric_l3_lineage


def _metric(code: str = "m1", definition: dict | None = None) -> MagicMock:
    m = MagicMock()
    m.metric_code = code
    m.definition_json = definition or {}
    return m


class TestRegisterMetricL3Lineage:
    async def test_calls_service_with_commit_false(self) -> None:
        db = MagicMock()
        metric = _metric("sales_e2e_gmv_day", {"source_table": "dws_x", "source_tables": ["ods_y"]})
        with patch("app.services.lineage.service.LineageService") as ls_cls:
            svc = ls_cls.return_value
            svc.register_metric_from_definition = AsyncMock(return_value=[object()])
            await _register_metric_l3_lineage(db, metric)
            ls_cls.assert_called_once_with(db)
            svc.register_metric_from_definition.assert_awaited_once_with(metric, commit=False)

    async def test_no_definition_still_calls(self) -> None:
        """无 source_table 时 register 内部返回空列表，但调用语义保持一致（幂等）。"""
        db = MagicMock()
        metric = _metric("m_empty", {})
        with patch("app.services.lineage.service.LineageService") as ls_cls:
            svc = ls_cls.return_value
            svc.register_metric_from_definition = AsyncMock(return_value=[])
            await _register_metric_l3_lineage(db, metric)
            svc.register_metric_from_definition.assert_awaited_once_with(metric, commit=False)

    async def test_registration_error_does_not_block(self) -> None:
        """血缘注册失败（如 mock db 不支持）不阻断指标主流程。"""
        db = MagicMock()
        metric = _metric("m_fail", {"source_table": "dws_x"})
        with patch("app.services.lineage.service.LineageService") as ls_cls:
            svc = ls_cls.return_value
            svc.register_metric_from_definition = AsyncMock(
                side_effect=RuntimeError("boom")
            )
            await _register_metric_l3_lineage(db, metric)  # 不应抛异常
            svc.register_metric_from_definition.assert_awaited_once_with(metric, commit=False)
