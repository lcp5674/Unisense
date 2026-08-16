"""指标状态机（6 态 + 合法跃迁矩阵）。

对齐 TD §12.3 / spec FR-001/FR-002：完整 6 态状态机
(DRAFT/REVIEW/PUBLISHED/EXPERIMENTAL/DEPRECATED/DATA_SOURCE_DROPPED)
含 10 种合法跃迁，非法跃迁返回 409。

跃迁矩阵::

    DRAFT             → REVIEW              (submit)
    REVIEW            → PUBLISHED           (approve)
    REVIEW            → EXPERIMENTAL        (approve_gray)
    REVIEW            → DRAFT               (reject)
    PUBLISHED         → DEPRECATED          (deprecate)
    PUBLISHED         → PENDING_CONFIRMATION(breaking_change)
    EXPERIMENTAL      → PUBLISHED           (promote)
    EXPERIMENTAL      → PUBLISHED           (rollback)
    DATA_SOURCE_DROPPED → PUBLISHED         (source_recovered)
    DATA_SOURCE_DROPPED → DEPRECATED        (confirm_deprecated)
    DEPRECATED        → REVIEW              (resubmit)  # 状态闭环：废弃后可重评审
"""

from __future__ import annotations

import enum


class MetricState(enum.StrEnum):
    """指标状态机 6 态枚举。"""

    DRAFT = "DRAFT"
    REVIEW = "REVIEW"
    PUBLISHED = "PUBLISHED"
    EXPERIMENTAL = "EXPERIMENTAL"
    DEPRECATED = "DEPRECATED"
    DATA_SOURCE_DROPPED = "DATA_SOURCE_DROPPED"


class MetricStateMachine:
    """指标状态机：合法跃迁矩阵 + 校验方法。

    用法::

        action = MetricStateMachine.validate_transition("DRAFT", "PUBLISHED")
        if action is not None:
            raise BusinessError(action, error_code="INVALID_TRANSITION")

        allowed = MetricStateMachine.get_allowed_transitions("DRAFT")
        # → [MetricState.REVIEW]
    """

    # 合法跃迁矩阵: {from_state: {to_state: action_name}}
    # 共 9 种合法跃迁（metric 级别）；PENDING_CONFIRMATION 是 version 级别状态
    TRANSITIONS: dict[str, dict[str, str]] = {
        MetricState.DRAFT: {
            MetricState.REVIEW: "submit",
        },
        MetricState.REVIEW: {
            MetricState.PUBLISHED: "approve",
            MetricState.EXPERIMENTAL: "approve_gray",
            MetricState.DRAFT: "reject",
        },
        MetricState.PUBLISHED: {
            MetricState.DEPRECATED: "deprecate",
            MetricState.DATA_SOURCE_DROPPED: "source_dropped",
        },
        MetricState.DEPRECATED: {
            # TD §13 状态闭环：废弃指标可重新发起评审（DEPRECATED → REVIEW），
            # 审核通过后经 REVIEW → PUBLISHED 恢复发布（重评审）。
            MetricState.REVIEW: "resubmit",
        },
        MetricState.EXPERIMENTAL: {
            MetricState.PUBLISHED: "promote",
        },
        MetricState.DATA_SOURCE_DROPPED: {
            MetricState.PUBLISHED: "source_recovered",
            MetricState.DEPRECATED: "confirm_deprecated",
        },
    }

    # 非法跃迁典型错误消息（用于 409 响应）
    ILLEGAL_TRANSITION_MESSAGES: dict[tuple[str, str], str] = {
        ("DRAFT", "PUBLISHED"): "须先 submit→REVIEW，再 approve→PUBLISHED",
        ("DRAFT", "DEPRECATED"): "仅 PUBLISHED 状态可废弃",
        ("DRAFT", "EXPERIMENTAL"): "须先 submit→REVIEW，再 approve 灰度模式",
        ("PUBLISHED", "DRAFT"): "已发布指标不可回退到 DRAFT",
        ("PUBLISHED", "REVIEW"): "已发布指标不可回退到 REVIEW",
        ("DEPRECATED", "PUBLISHED"): (
            "已废弃指标不可直接恢复为 PUBLISHED，须先重新发起评审（DEPRECATED→REVIEW→PUBLISHED）"
        ),
        ("DEPRECATED", "DRAFT"): "已废弃指标不可回退到 DRAFT",
        ("REVIEW", "DEPRECATED"): "仅 PUBLISHED 状态可废弃，须先 approve",
    }

    @classmethod
    def validate_transition(
        cls, from_state: str | MetricState, to_state: str | MetricState
    ) -> str | None:
        """校验状态跃迁合法性。

        Args:
            from_state: 当前状态。
            to_state: 目标状态。

        Returns:
            None 表示合法；字符串表示拒绝原因（用于 409 错误消息）。
        """
        from_str = str(from_state)
        to_str = str(to_state)

        if from_str == to_str:
            return None  # 同状态不视为跃迁

        allowed = cls.TRANSITIONS.get(from_str, {})
        if to_str in allowed:
            return None

        # 生成可读的拒绝原因
        custom_msg = cls.ILLEGAL_TRANSITION_MESSAGES.get((from_str, to_str))
        if custom_msg:
            return f"非法状态跃迁: {from_str}→{to_str}，{custom_msg}"

        from_allowed = cls.TRANSITIONS.get(from_str, {})
        if from_allowed:
            allowed_names = list(from_allowed)
            return (
                f"非法状态跃迁: {from_str}→{to_str}，"
                f"当前状态 {from_str} 允许的跃迁为: {', '.join(allowed_names)}"
            )
        return f"非法状态跃迁: {from_str}→{to_str}，当前状态 {from_str} 无允许的跃迁"

    @classmethod
    def get_allowed_transitions(cls, state: str | MetricState) -> list[MetricState]:
        """获取指定状态允许的跃迁目标列表。

        Args:
            state: 当前状态。

        Returns:
            允许的目标状态列表。
        """
        allowed = cls.TRANSITIONS.get(str(state), {})
        return [MetricState(s) for s in allowed]

    @classmethod
    def get_action_name(
        cls, from_state: str | MetricState, to_state: str | MetricState
    ) -> str | None:
        """获取跃迁对应的动作名。

        Args:
            from_state: 当前状态。
            to_state: 目标状态。

        Returns:
            动作名或 None。
        """
        allowed = cls.TRANSITIONS.get(str(from_state), {})
        return allowed.get(str(to_state))
