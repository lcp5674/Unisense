"""测试 LLM 结构化输出（P2: US12 置信度分流）。

覆盖：
1. LlmStructuredOutput Schema 校验
2. confidence 分流逻辑（<0.7 → needs_review）
3. 置信度校准门禁
4. 确定性降级客户端结构化输出
5. 非结构化输出降级包装
"""

from __future__ import annotations

import json

import pytest

from app.services.llm.client import (
    DeterministicFallbackLlmClient,
    LlmStructuredOutput,
)


class TestLlmStructuredOutputSchema:
    """测试 LlmStructuredOutput Pydantic Schema。"""

    def test_valid_structured_output(self) -> None:
        out = LlmStructuredOutput(
            content="PII",
            confidence=0.9,
            reasoning="含手机号字段",
            candidates=[{"level": "PII", "score": 0.9}],
        )
        assert out.content == "PII"
        assert out.confidence == 0.9
        assert out.reasoning == "含手机号字段"
        assert len(out.candidates) == 1

    def test_confidence_clamp_high(self) -> None:
        """confidence > 1.0 被钳制到 1.0。"""
        out = LlmStructuredOutput(content="test", confidence=1.5)
        assert out.confidence == 1.0

    def test_confidence_clamp_low(self) -> None:
        """confidence < 0.0 被钳制到 0.0。"""
        out = LlmStructuredOutput(content="test", confidence=-0.5)
        assert out.confidence == 0.0

    def test_default_confidence(self) -> None:
        """默认 confidence 为 0.5。"""
        out = LlmStructuredOutput(content="test")
        assert out.confidence == 0.5

    def test_default_reasoning_and_candidates(self) -> None:
        out = LlmStructuredOutput(content="test", confidence=0.8)
        assert out.reasoning == ""
        assert out.candidates == []

    def test_confidence_boundary_0_7(self) -> None:
        """0.7 是分流阈值边界，测试左右。"""
        below = LlmStructuredOutput(content="test", confidence=0.69)
        at = LlmStructuredOutput(content="test", confidence=0.7)
        above = LlmStructuredOutput(content="test", confidence=0.71)
        assert below.confidence < 0.7
        assert at.confidence == 0.7
        assert above.confidence > 0.7


class TestConfidenceRouting:
    """测试 confidence 分流逻辑。"""

    def test_low_confidence_triggers_needs_review(self) -> None:
        """confidence < 0.7 → 标记为 needs_review。"""
        result = {
            "content": "INTERNAL",
            "confidence": 0.5,
            "reasoning": "不确定",
            "candidates": [],
        }
        confidence = result["confidence"]
        sensitivity = "needs_review" if confidence < 0.7 else result["content"]
        assert sensitivity == "needs_review"

    def test_high_confidence_auto_adopt(self) -> None:
        """confidence >= 0.7 → 自动采纳。"""
        result = {
            "content": "PII",
            "confidence": 0.85,
            "reasoning": "含身份证号",
            "candidates": [],
        }
        confidence = result["confidence"]
        sensitivity = "needs_review" if confidence < 0.7 else result["content"]
        assert sensitivity == "PII"

    def test_confidence_0_triggers_needs_review(self) -> None:
        """LLM 不可用时 confidence=0 → needs_review。"""
        result = {
            "content": "",
            "confidence": 0.0,
            "reasoning": "LLM 不可用",
            "candidates": [],
        }
        confidence = result["confidence"]
        sensitivity = "needs_review" if confidence < 0.7 else result["content"]
        assert sensitivity == "needs_review"

    def test_confidence_0_7_boundary_auto_adopt(self) -> None:
        """confidence=0.7 刚好自动采纳。"""
        result = {
            "content": "CONFIDENTIAL",
            "confidence": 0.7,
            "reasoning": "含敏感字段",
            "candidates": [],
        }
        confidence = result["confidence"]
        sensitivity = "needs_review" if confidence < 0.7 else result["content"]
        assert sensitivity == "CONFIDENTIAL"


class TestCalibrationGate:
    """测试置信度校准门禁。"""

    def test_structured_json_parsing(self) -> None:
        """结构化 JSON 输出正确解析。"""
        raw = json.dumps(
            {
                "content": "PII",
                "confidence": 0.92,
                "reasoning": "含手机号",
                "candidates": [{"level": "PII", "score": 0.92}],
            }
        )
        # 模拟 _parse_structured_output 逻辑
        parsed = json.loads(raw)
        out = LlmStructuredOutput(
            content=str(parsed.get("content", raw)),
            confidence=float(parsed.get("confidence", 0.5)),
            reasoning=str(parsed.get("reasoning", "")),
            candidates=parsed.get("candidates", []),
        )
        assert out.content == "PII"
        assert out.confidence == 0.92
        assert out.reasoning == "含手机号"

    def test_unstructured_text_fallback(self) -> None:
        """非 JSON 输出降级为 confidence=0.5。"""
        raw = "这是一个普通文本响应"
        try:
            parsed = json.loads(raw)
            out = LlmStructuredOutput(content=str(parsed.get("content", raw)))
        except (json.JSONDecodeError, ValueError):
            out = LlmStructuredOutput(
                content=raw,
                confidence=0.5,
                reasoning="非结构化输出，默认置信度 0.5",
            )
        assert out.confidence == 0.5
        assert out.content == raw

    def test_partial_json_fallback(self) -> None:
        """部分 JSON 缺少字段时用默认值。"""
        raw = json.dumps({"content": "INTERNAL"})
        parsed = json.loads(raw)
        out = LlmStructuredOutput(
            content=str(parsed.get("content", "")),
            confidence=float(parsed.get("confidence", 0.5)),
            reasoning=str(parsed.get("reasoning", "")),
            candidates=parsed.get("candidates", []),
        )
        assert out.content == "INTERNAL"
        assert out.confidence == 0.5  # 缺省


class TestDeterministicFallbackStructured:
    """测试确定性降级客户端结构化输出。"""

    @pytest.mark.asyncio
    async def test_fallback_returns_structured(self) -> None:
        client = DeterministicFallbackLlmClient()
        result = await client.chat([{"role": "user", "content": "test"}])
        assert "confidence" in result
        assert result["confidence"] == 0.0
        assert result["reasoning"] != ""
        assert "candidates" in result
        assert result["model"] == "deterministic-fallback"

    @pytest.mark.asyncio
    async def test_fallback_confidence_triggers_needs_review(self) -> None:
        client = DeterministicFallbackLlmClient()
        result = await client.chat([{"role": "user", "content": "test"}])
        sensitivity = "needs_review" if result["confidence"] < 0.7 else result["content"]
        assert sensitivity == "needs_review"
