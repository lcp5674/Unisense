"""PII 字段级脱敏二次校验单测（Epic D：落库外补强校验，依赖 governance）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import NotFoundError
from app.services.governance.service import GovernanceService


class _StubMetric:
    def __init__(self, pii_flag: bool, compliance_reviewed: bool, definition_json: dict) -> None:
        self.pii_flag = pii_flag
        self.compliance_reviewed = compliance_reviewed
        self.definition_json = definition_json


def _svc(metric: _StubMetric | None) -> GovernanceService:
    svc = GovernanceService(db=MagicMock())
    svc._repo = MagicMock()
    svc._repo.get_metric_by_code = AsyncMock(return_value=metric)
    return svc


@pytest.mark.asyncio
async def test_non_pii_metric_passes() -> None:
    svc = _svc(_StubMetric(False, False, {}))
    res = await svc.validate_pii_masking("m1")
    assert res.passed is True
    assert res.findings == []


@pytest.mark.asyncio
async def test_pii_metric_without_compliance_review_fails() -> None:
    svc = _svc(_StubMetric(True, False, {"pii_fields": ["phone"]}))
    res = await svc.validate_pii_masking("m1", pii_columns=["phone"])
    assert res.passed is False
    assert any("合规复核" in f for f in res.findings)
    assert "phone" in res.checked_columns


@pytest.mark.asyncio
async def test_pii_metric_plaintext_exposure_fails() -> None:
    # 合规已通过，但口径定义中明文暴露 PII 字段
    svc = _svc(
        _StubMetric(
            True,
            True,
            {"expression": "select phone from user", "pii_fields": ["phone"]},
        )
    )
    res = await svc.validate_pii_masking("m1", pii_columns=["phone"])
    assert res.passed is False
    assert any("明文暴露" in f for f in res.findings)


@pytest.mark.asyncio
async def test_pii_metric_clean_passes() -> None:
    svc = _svc(
        _StubMetric(
            True,
            True,
            {"expression": "select hash(phone) as phone_masked from user"},
        )
    )
    res = await svc.validate_pii_masking("m1", pii_columns=["phone"])
    assert res.passed is True
    assert res.masking_policy == "hash"


@pytest.mark.asyncio
async def test_unknown_metric_raises() -> None:
    svc = _svc(None)
    with pytest.raises(NotFoundError):
        await svc.validate_pii_masking("missing")
