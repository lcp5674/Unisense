"""敏感规则 DB 可配置加载测试（PII 合规增强 C-1：system_dict 覆盖内置规则）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services.collector.classifier import PiiCategory, SensitivityClassifier
from app.services.collector.rules import _parse_rule, load_pii_rules
from app.services.collector.service import CollectorService


def _dict_item(code: str, description: str | None) -> SimpleNamespace:
    return SimpleNamespace(code=code, description=description)


def test_parse_rule_valid_json() -> None:
    item = _dict_item(
        "id_card",
        '{"category":"ID_CARD","name_re":"(id_?card|sfz|身份证)",'
        '"sample_re":"^\\\\d{17}[\\\\dXx]$","confidence":0.95}',
    )
    rule = _parse_rule(item)
    assert rule is not None
    assert rule.rule_id == "id_card"
    assert rule.category == PiiCategory.ID_CARD
    assert rule.confidence == 0.95
    assert rule.sample_re is not None


def test_parse_rule_invalid_json_skipped() -> None:
    assert _parse_rule(_dict_item("bad", "not-json{{")) is None
    assert _parse_rule(_dict_item("empty", None)) is None
    # 无 name_re 也跳过
    assert _parse_rule(_dict_item("nore", '{"category":"PHONE","confidence":0.8}')) is None


def test_parse_rule_unknown_category_falls_back() -> None:
    rule = _parse_rule(_dict_item("x", '{"category":"NOPE","name_re":"foo","confidence":0.9}'))
    assert rule is not None
    assert rule.category == PiiCategory.NAME  # 无法识别归入通用类别


async def test_load_pii_rules_empty_returns_none() -> None:
    s = MagicMock()
    empty = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
    s.execute = AsyncMock(return_value=empty)
    pii_rules, conf_rules = await load_pii_rules(s)
    assert pii_rules is None
    assert conf_rules is None


async def test_load_pii_rules_parses_rows() -> None:
    s = MagicMock()
    rows = [
        _dict_item(
            "phone",
            '{"category":"PHONE","name_re":"(phone|mobile)","sample_re":null,"confidence":0.9}',
        ),
        _dict_item(
            "id_card",
            '{"category":"ID_CARD","name_re":"sfz","sample_re":null,"confidence":0.95}',
        ),
    ]
    all_mock = MagicMock(all=MagicMock(return_value=rows))
    s.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=all_mock)))
    pii_rules, conf_rules = await load_pii_rules(s)
    assert pii_rules is not None
    assert len(pii_rules) == 2
    assert pii_rules[0].rule_id == "phone"
    assert pii_rules[0].category == PiiCategory.PHONE


async def test_maybe_load_db_rules_injects_classifier() -> None:
    db = MagicMock()
    svc = CollectorService(db=db)
    assert isinstance(svc._classifier, SensitivityClassifier)  # noqa: SLF001
    # 默认无 pii_rule 配置 → classifier 不变
    empty = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
    db.execute = AsyncMock(return_value=empty)
    await svc._maybe_load_db_rules()  # noqa: SLF001
    assert isinstance(svc._classifier, SensitivityClassifier)  # noqa: SLF001


async def test_maybe_load_db_rules_uses_db_rules() -> None:
    db = MagicMock()
    svc = CollectorService(db=db)
    rows = [
        _dict_item(
            "custom",
            '{"category":"HEALTH","name_re":"(clinic|hospital)","sample_re":null,"confidence":0.95}',
        ),
    ]
    all_mock = MagicMock(all=MagicMock(return_value=rows))
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=all_mock)))
    await svc._maybe_load_db_rules()  # noqa: SLF001
    # 注入 DB 规则后：clinic 命中健康类别判 PII；内置 phone 不再命中（规则被覆盖）
    assert svc._classifier.classify("t", {"columns": [{"name": "clinic"}]}) == "PII"  # noqa: SLF001
    assert svc._classifier.classify("t", {"columns": [{"name": "phone"}]}) == "INTERNAL"  # noqa: SLF001
