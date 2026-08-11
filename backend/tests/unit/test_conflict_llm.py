"""冲突检测 LLM 补位单测（TD §12.4 / FR-09 语义补位）。

LLM 补位触发条件：词法未达软冲突阈值（composite < 0.6），但定义语义相似
（def_sim ∈ [0.45, 0.85)），即「同义异名 / 表述差异大」的漏报区。为使 composite
足够低，候选与已存在口径需使用**不同** metric_code 与**不同** source_tables。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.conflict.schemas import MetricInput
from app.services.conflict.service import ConflictService
from app.services.conflict.similarity import (
    ConflictDetection,
    detect_conflict,
    is_borderline_match,
)


def _cand(defn: str, code: str = "metric_alpha", src: str = "src_x") -> dict:
    return {"metric_code": code, "domain": "d", "definition": defn, "source_tables": [src]}


def _ext(defn: str, code: str = "metric_beta", src: str = "src_y") -> dict:
    return {"metric_code": code, "domain": "d", "definition": defn, "source_tables": [src]}


# 语义接近但词法未达软冲突阈值（borderline，composite < 0.6）
_BORDERLINE = (
    "用户活跃天数，统计当日登录的去重用户数",
    "日活用户数，按天统计登录的去重用户",
)
# 明显不同（非 borderline）
_DISTINCT = ("订单总数", "本月新增注册用户数")


def test_is_borderline_true_for_similar_defs() -> None:
    assert is_borderline_match(_cand(_BORDERLINE[0]), _ext(_BORDERLINE[1])) is True


def test_is_borderline_false_for_distinct_defs() -> None:
    assert is_borderline_match(_cand(_DISTINCT[0]), _ext(_DISTINCT[1])) is False


def test_detect_conflict_llm_confirms_same_semantics() -> None:
    det = detect_conflict(
        _cand(_BORDERLINE[0]), _ext(_BORDERLINE[1]), llm_judge=lambda a, b: True
    )
    assert isinstance(det, ConflictDetection)
    assert det.conflict_type.value == "same_def_diff_name"
    assert det.severity == "soft"
    assert det.block_publish is False
    assert det.llm_confirmed is True


def test_detect_conflict_no_llm_returns_none_on_borderline() -> None:
    assert detect_conflict(_cand(_BORDERLINE[0]), _ext(_BORDERLINE[1])) is None


def test_detect_conflict_llm_abstain_keeps_no_conflict() -> None:
    assert (
        detect_conflict(
            _cand(_BORDERLINE[0]), _ext(_BORDERLINE[1]), llm_judge=lambda a, b: None
        )
        is None
    )


def test_detect_conflict_llm_false_keeps_no_conflict() -> None:
    assert (
        detect_conflict(
            _cand(_BORDERLINE[0]), _ext(_BORDERLINE[1]), llm_judge=lambda a, b: False
        )
        is None
    )


def test_detect_conflict_llm_not_called_when_distinct() -> None:
    calls: list[tuple[str, str]] = []

    def _judge(a: str, b: str) -> bool | None:
        calls.append((a, b))
        return True

    # 明显不同的口径即便 LLM 判定同义也不应升级（borderline 为 False）
    assert detect_conflict(_cand(_DISTINCT[0]), _ext(_DISTINCT[1]), llm_judge=_judge) is None
    assert calls == []


class _FakeLlm:
    """测试用异步 LLM 客户端：始终判定为同义。"""

    def __init__(self, result: bool | None) -> None:
        self._result = result
        self.calls = 0

    async def judge_same_semantics(self, candidate_def: str, existing_def: str) -> bool | None:
        self.calls += 1
        return self._result


def _make_service(llm: _FakeLlm) -> ConflictService:
    svc = ConflictService(db=MagicMock(), events=MagicMock(), llm=llm)
    svc._repo = MagicMock()
    svc._repo.create = AsyncMock(return_value=MagicMock())
    return svc


def _cand_input() -> MetricInput:
    return MetricInput(
        metric_code="metric_alpha",
        domain="d",
        definition=_BORDERLINE[0],
        source_tables=["src_x"],
    )


def _ext_input() -> MetricInput:
    return MetricInput(
        metric_code="metric_beta",
        domain="d",
        definition=_BORDERLINE[1],
        source_tables=["src_y"],
    )


@pytest.mark.asyncio
async def test_service_check_llm_supplements_soft_conflict() -> None:
    svc = _make_service(_FakeLlm(True))
    result = await svc.check(_cand_input(), [_ext_input()])
    assert result.blocked is False
    assert len(result.detections) == 1
    det = result.detections[0]
    assert det.llm_confirmed is True
    assert det.conflict_type.value == "same_def_diff_name"


@pytest.mark.asyncio
async def test_service_check_llm_abstain_no_extra_conflict() -> None:
    svc = _make_service(_FakeLlm(None))
    result = await svc.check(_cand_input(), [_ext_input()])
    assert result.detections == []
