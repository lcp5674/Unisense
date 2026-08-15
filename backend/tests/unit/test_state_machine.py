"""状态机单元测试（对齐 TD §12.3 / spec FR-001/FR-002）。

覆盖：
- 全部 9 种合法跃迁
- 非法跃迁（DRAFT→PUBLISHED, DRAFT→DEPRECATED, PUBLISHED→DRAFT 等）
- get_allowed_transitions
- get_action_name
"""

from __future__ import annotations

import pytest

from app.services.semantic.state_machine import MetricState, MetricStateMachine


class TestLegalTransitions:
    """测试全部合法跃迁。"""

    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            ("DRAFT", "REVIEW"),  # submit
            ("REVIEW", "PUBLISHED"),  # approve
            ("REVIEW", "EXPERIMENTAL"),  # approve_gray
            ("REVIEW", "DRAFT"),  # reject
            ("PUBLISHED", "DEPRECATED"),  # deprecate
            ("EXPERIMENTAL", "PUBLISHED"),  # promote
            ("DATA_SOURCE_DROPPED", "PUBLISHED"),  # source_recovered
            ("DATA_SOURCE_DROPPED", "DEPRECATED"),  # confirm_deprecated
        ],
    )
    def test_legal_transition_returns_none(self, from_state: str, to_state: str) -> None:
        result = MetricStateMachine.validate_transition(from_state, to_state)
        assert result is None, f"Expected {from_state}→{to_state} to be legal, got: {result}"

    def test_same_state_is_legal(self) -> None:
        """同状态不视为跃迁，返回 None。"""
        result = MetricStateMachine.validate_transition("DRAFT", "DRAFT")
        assert result is None


class TestIllegalTransitions:
    """测试非法跃迁（返回拒绝原因字符串）。"""

    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            ("DRAFT", "PUBLISHED"),  # 须先 submit→approve
            ("DRAFT", "DEPRECATED"),  # 仅 PUBLISHED 可废弃
            ("DRAFT", "EXPERIMENTAL"),  # 须先 submit→approve 灰度
            ("PUBLISHED", "DRAFT"),  # 已发布不可回退
            ("PUBLISHED", "REVIEW"),  # 已发布不可回退
            ("DEPRECATED", "PUBLISHED"),  # 已废弃不可恢复
            ("DEPRECATED", "DRAFT"),  # 已废弃不可回退
            ("REVIEW", "DEPRECATED"),  # 仅 PUBLISHED 可废弃
        ],
    )
    def test_illegal_transition_returns_message(self, from_state: str, to_state: str) -> None:
        result = MetricStateMachine.validate_transition(from_state, to_state)
        assert result is not None, f"Expected {from_state}→{to_state} to be illegal"
        assert "非法状态跃迁" in result

    def test_illegal_transition_drafted_published_custom_message(self) -> None:
        """DRAFT→PUBLISHED 应返回自定义消息。"""
        result = MetricStateMachine.validate_transition("DRAFT", "PUBLISHED")
        assert result is not None
        assert "须先 submit" in result

    def test_illegal_transition_draft_deprecated_custom_message(self) -> None:
        """DRAFT→DEPRECATED 应返回'仅 PUBLISHED 状态可废弃'。"""
        result = MetricStateMachine.validate_transition("DRAFT", "DEPRECATED")
        assert result is not None
        assert "仅 PUBLISHED" in result


class TestGetAllowedTransitions:
    """测试 get_allowed_transitions。"""

    def test_draft_allows_review(self) -> None:
        allowed = MetricStateMachine.get_allowed_transitions("DRAFT")
        assert MetricState.REVIEW in allowed
        assert len(allowed) == 1

    def test_review_allows_published_experimental_draft(self) -> None:
        allowed = MetricStateMachine.get_allowed_transitions("REVIEW")
        assert MetricState.PUBLISHED in allowed
        assert MetricState.EXPERIMENTAL in allowed
        assert MetricState.DRAFT in allowed
        assert len(allowed) == 3

    def test_published_allows_deprecated(self) -> None:
        allowed = MetricStateMachine.get_allowed_transitions("PUBLISHED")
        assert MetricState.DEPRECATED in allowed
        assert len(allowed) == 1

    def test_deprecated_allows_review_resubmit(self) -> None:
        """DEPRECATED 允许重评审闭环跃迁（DEPRECATED→REVIEW，TD §13 resubmit）。"""
        allowed = MetricStateMachine.get_allowed_transitions("DEPRECATED")
        assert allowed == [MetricState.REVIEW]

    def test_experimental_allows_published(self) -> None:
        allowed = MetricStateMachine.get_allowed_transitions("EXPERIMENTAL")
        assert MetricState.PUBLISHED in allowed
        assert len(allowed) == 1

    def test_data_source_dropped_allows_published_and_deprecated(self) -> None:
        allowed = MetricStateMachine.get_allowed_transitions("DATA_SOURCE_DROPPED")
        assert MetricState.PUBLISHED in allowed
        assert MetricState.DEPRECATED in allowed
        assert len(allowed) == 2


class TestGetActionName:
    """测试 get_action_name。"""

    @pytest.mark.parametrize(
        ("from_state", "to_state", "expected_action"),
        [
            ("DRAFT", "REVIEW", "submit"),
            ("REVIEW", "PUBLISHED", "approve"),
            ("REVIEW", "EXPERIMENTAL", "approve_gray"),
            ("REVIEW", "DRAFT", "reject"),
            ("PUBLISHED", "DEPRECATED", "deprecate"),
            ("EXPERIMENTAL", "PUBLISHED", "promote"),
            ("DATA_SOURCE_DROPPED", "PUBLISHED", "source_recovered"),
            ("DATA_SOURCE_DROPPED", "DEPRECATED", "confirm_deprecated"),
        ],
    )
    def test_valid_action_name(self, from_state: str, to_state: str, expected_action: str) -> None:
        result = MetricStateMachine.get_action_name(from_state, to_state)
        assert result == expected_action

    def test_invalid_action_name_returns_none(self) -> None:
        result = MetricStateMachine.get_action_name("DRAFT", "PUBLISHED")
        assert result is None


class TestExperimentalTransitions:
    """灰度发布与回滚跃迁测试（对齐 FR-019/FR-020）。"""

    def test_experimental_to_published_is_legal(self) -> None:
        """EXPERIMENTAL→PUBLISHED（promote 全量发布）是合法跃迁。"""
        result = MetricStateMachine.validate_transition("EXPERIMENTAL", "PUBLISHED")
        assert result is None

    def test_experimental_to_draft_is_illegal(self) -> None:
        """EXPERIMENTAL→DRAFT 是非法跃迁。"""
        result = MetricStateMachine.validate_transition("EXPERIMENTAL", "DRAFT")
        assert result is not None

    def test_experimental_to_deprecated_is_illegal(self) -> None:
        """EXPERIMENTAL→DEPRECATED 是非法跃迁（须先 promote→PUBLISHED→DEPRECATED）。"""
        result = MetricStateMachine.validate_transition("EXPERIMENTAL", "DEPRECATED")
        assert result is not None

    def test_experimental_to_review_is_illegal(self) -> None:
        """EXPERIMENTAL→REVIEW 是非法跃迁。"""
        result = MetricStateMachine.validate_transition("EXPERIMENTAL", "REVIEW")
        assert result is not None

    def test_promote_action_name(self) -> None:
        """EXPERIMENTAL→PUBLISHED 的动作名为 promote。"""
        action = MetricStateMachine.get_action_name("EXPERIMENTAL", "PUBLISHED")
        assert action == "promote"

    def test_experimental_only_allows_published(self) -> None:
        """EXPERIMENTAL 状态只允许跃迁到 PUBLISHED。"""
        allowed = MetricStateMachine.get_allowed_transitions("EXPERIMENTAL")
        assert len(allowed) == 1
        assert MetricState.PUBLISHED in allowed
