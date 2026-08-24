"""指标发布术语绑定软提醒单测（P1：已有口径但 term_id 为空 → 引导提示，不硬卡）。

覆盖：有口径未绑定术语 → 返回提示；已绑定术语 → None；空口径 → None。
"""

from __future__ import annotations

from tests.conftest import make_metric

from app.services.semantic.service import MetricService


class TestTermBindingReminder:
    def test_definition_without_term_returns_reminder(self) -> None:
        metric = make_metric(term_id=None, definition_json={"expression": "SUM(amount)"})
        reminder = MetricService.term_binding_reminder(metric)
        assert reminder is not None
        assert "术语" in reminder
        assert "绑定" in reminder

    def test_bound_term_returns_none(self) -> None:
        metric = make_metric(term_id=55, definition_json={"expression": "SUM(amount)"})
        assert MetricService.term_binding_reminder(metric) is None

    def test_no_definition_returns_none(self) -> None:
        metric = make_metric(term_id=None, definition_json={})
        assert MetricService.term_binding_reminder(metric) is None
