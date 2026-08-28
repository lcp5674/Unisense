"""敏感规则 DB 可配置加载测试（合并语义：DB 按 rule_id 覆盖内置，其余回退内置）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services.collector.classifier import (
    DEFAULT_CONFIDENTIAL_RULES,
    DEFAULT_PII_RULES,
    PiiCategory,
    SensitivityClassifier,
)
from app.services.collector.rules import load_pii_rules, merge_effective_rules
from app.services.collector.service import CollectorService


def _dict_item(code: str, description: str | None, status: str = "active") -> SimpleNamespace:
    return SimpleNamespace(code=code, description=description, status=status)


def _rows_mock(rows: list[SimpleNamespace]) -> MagicMock:
    all_mock = MagicMock(all=MagicMock(return_value=rows))
    return MagicMock(scalars=MagicMock(return_value=all_mock))


def test_parse_rule_valid_json() -> None:
    from app.services.collector.rules import _parse_rule

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
    from app.services.collector.rules import _parse_rule

    assert _parse_rule(_dict_item("bad", "not-json{{")) is None
    assert _parse_rule(_dict_item("empty", None)) is None
    # 无 name_re 也跳过
    assert _parse_rule(_dict_item("nore", '{"category":"PHONE","confidence":0.8}')) is None


def test_parse_rule_unknown_category_falls_back() -> None:
    from app.services.collector.rules import _parse_rule

    rule = _parse_rule(_dict_item("x", '{"category":"NOPE","name_re":"foo","confidence":0.9}'))
    assert rule is not None
    assert rule.category == PiiCategory.NAME  # 无法识别归入通用类别


def test_parse_rule_confidential_category_and_pii_flag() -> None:
    from app.services.collector.classifier import ConfidentialCategory
    from app.services.collector.rules import _parse_rule

    rule = _parse_rule(
        _dict_item(
            "custom_cred",
            '{"category":"CREDENTIAL","name_re":"(app_?secret)","sample_re":null,'
            '"confidence":0.95,"pii":false}',
        )
    )
    assert rule is not None
    assert rule.pii is False
    assert rule.category == ConfidentialCategory.CREDENTIAL


async def test_load_pii_rules_empty_returns_builtin() -> None:
    s = MagicMock()
    s.execute = AsyncMock(return_value=_rows_mock([]))
    pii_rules, conf_rules = await load_pii_rules(s)
    # 合并语义：DB 空 → 完整内置生效集（非 None）
    assert pii_rules == DEFAULT_PII_RULES
    assert conf_rules == DEFAULT_CONFIDENTIAL_RULES


async def test_load_pii_rules_merge_custom_keeps_builtin() -> None:
    s = MagicMock()
    rows = [
        _dict_item(
            "custom",
            '{"category":"HEALTH","name_re":"(clinic|hospital)","sample_re":null,"confidence":0.95}',
        ),
    ]
    s.execute = AsyncMock(return_value=_rows_mock(rows))
    pii_rules, conf_rules = await load_pii_rules(s)
    # 自定义追加 + 内置完整保留（覆盖语义：不吞内置）
    assert len(pii_rules) == len(DEFAULT_PII_RULES) + 1
    ids = {r.rule_id for r in pii_rules}
    assert "custom" in ids
    assert "id_card" in ids and "phone" in ids
    assert conf_rules == DEFAULT_CONFIDENTIAL_RULES


async def test_load_pii_rules_override_same_id() -> None:
    """DB 项覆盖同 ID 内置规则（改正则后采集即用新正则）。"""
    s = MagicMock()
    rows = [
        _dict_item(
            "phone",
            '{"category":"PHONE","name_re":"(customer_mobile)","sample_re":null,"confidence":0.9}',
        ),
    ]
    s.execute = AsyncMock(return_value=_rows_mock(rows))
    pii_rules, _conf = await load_pii_rules(s)
    assert len(pii_rules) == len(DEFAULT_PII_RULES)
    phone = next(r for r in pii_rules if r.rule_id == "phone")
    assert phone.name_re == "(customer_mobile)"
    # 覆盖后：customer_mobile 命中 PHONE，原 phone 关键字不再命中
    clf = SensitivityClassifier(rules=pii_rules)
    assert clf.classify("t", {"columns": [{"name": "customer_mobile"}]}) == "PII"
    assert clf.classify("t", {"columns": [{"name": "phone"}]}) == "INTERNAL"


async def test_load_pii_rules_disabled_removes_rule() -> None:
    """DB 项 inactive → 该规则整体停用（内置亦不生效）。"""
    s = MagicMock()
    rows = [
        _dict_item(
            "real_name",
            '{"category":"NAME","name_re":"(\\bname\\b|姓名)","sample_re":null,"confidence":0.7}',
            status="inactive",
        ),
    ]
    s.execute = AsyncMock(return_value=_rows_mock(rows))
    pii_rules, _conf = await load_pii_rules(s)
    ids = {r.rule_id for r in pii_rules}
    assert "real_name" not in ids
    assert "phone" in ids  # 其余内置保留
    clf = SensitivityClassifier(rules=pii_rules)
    assert clf.classify("t", {"columns": [{"name": "cust_name"}]}) == "INTERNAL"


async def test_load_pii_rules_confidential_custom() -> None:
    """pii=false 自定义规则归入机密规则集（判 CONFIDENTIAL 不计 PII）。"""
    s = MagicMock()
    rows = [
        _dict_item(
            "bonus",
            '{"category":"BUSINESS","name_re":"(bonus|奖金)","sample_re":null,'
            '"confidence":0.9,"pii":false}',
        ),
    ]
    s.execute = AsyncMock(return_value=_rows_mock(rows))
    pii_rules, conf_rules = await load_pii_rules(s)
    conf_ids = {r.rule_id for r in conf_rules}
    assert "bonus" in conf_ids
    assert len(conf_rules) == len(DEFAULT_CONFIDENTIAL_RULES) + 1
    clf = SensitivityClassifier(rules=pii_rules, confidential_rules=conf_rules)
    assert clf.classify("t", {"columns": [{"name": "bonus"}]}) == "CONFIDENTIAL"


def test_merge_effective_rules_direct() -> None:
    rows = [
        _dict_item("phone", '{"category":"PHONE","name_re":"mobile","confidence":0.9}'),
        _dict_item("password", '{"category":"CREDENTIAL","name_re":"pwd","pii":false}'),
    ]
    pii_rules, conf_rules = merge_effective_rules(rows)
    assert {r.rule_id for r in pii_rules} == {r.rule_id for r in DEFAULT_PII_RULES}
    assert {r.rule_id for r in conf_rules} == {r.rule_id for r in DEFAULT_CONFIDENTIAL_RULES}


async def test_maybe_load_db_rules_injects_classifier() -> None:
    db = MagicMock()
    svc = CollectorService(db=db)
    assert isinstance(svc._classifier, SensitivityClassifier)  # noqa: SLF001
    # 默认无 pii_rule 配置 → classifier 不变（仍内置）
    db.execute = AsyncMock(return_value=_rows_mock([]))
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
    db.execute = AsyncMock(return_value=_rows_mock(rows))
    await svc._maybe_load_db_rules()  # noqa: SLF001
    # 合并语义：自定义 clinic 命中健康类别判 PII；内置 phone 仍生效
    assert svc._classifier.classify("t", {"columns": [{"name": "clinic"}]}) == "PII"  # noqa: SLF001
    assert svc._classifier.classify("t", {"columns": [{"name": "phone"}]}) == "PII"  # noqa: SLF001


# ---- PII 上下文词表（pii_vocab 字典加载）----


def test_merge_vocab_overrides_and_keeps_defaults() -> None:
    """merge_vocab：正则类整体覆盖、词条类分隔、豁免并入、缺省回退内置。"""
    from app.services.collector.classifier import PiiVocab
    from app.services.collector.rules import merge_vocab

    v = merge_vocab(
        {
            "person_name_re": r"(patient|孕妇)_?name$",
            "exempt_field": "phone, village_name",
            "exempt_prefix": "test_",
            "aggregate_re": "",
        }
    )
    # 正则覆盖生效（孕妇_name 判 PII；原内置 patient 保留）
    clf = SensitivityClassifier(vocab=v)
    assert clf.detect_pii_fields("t", {"columns": [{"name": "孕妇_name"}]})
    assert clf.detect_pii_fields("t", {"columns": [{"name": "patient_name"}]})
    # 豁免字段/前缀生效
    assert clf.detect_pii_fields("t", {"columns": [{"name": "phone", "comment": "手机号"}]}) == []
    assert (
        clf.detect_pii_fields("t", {"columns": [{"name": "test_phone", "comment": "手机号"}]})
        == []
    )
    # aggregate_re 空 → 回退内置默认（fail-safe）
    assert v.aggregate_re == PiiVocab().aggregate_re


async def test_load_pii_vocab_from_db() -> None:
    """load_pii_vocab：从 system_dict 读取 pii_vocab 项并合并。"""
    from app.services.collector.classifier import PiiVocab
    from app.services.collector.rules import load_pii_vocab

    db = MagicMock()
    rows = [
        _dict_item("person_name_re", r"(patient|孕妇|教师)_?name$"),
        _dict_item("exempt_field", "phone, village_name"),
        _dict_item("exempt_field", "ward_name"),  # 多行追加
        _dict_item("inactive_vocab", "ignored", status="inactive"),
    ]
    db.execute = AsyncMock(return_value=_rows_mock(rows))
    v = await load_pii_vocab(db)
    assert isinstance(v, PiiVocab)
    clf = SensitivityClassifier(vocab=v)
    assert clf.detect_pii_fields("t", {"columns": [{"name": "孕妇_name"}]})
    assert clf.detect_pii_fields("t", {"columns": [{"name": "phone", "comment": "手机号"}]}) == []
    assert (
        clf.detect_pii_fields("t", {"columns": [{"name": "ward_name", "comment": "病区名称"}]})
        == []
    )
    # 默认词表（heart_rate 值型豁免）保持
    assert clf.detect_pii_fields("t", {"columns": [{"name": "heart_rate"}]})
