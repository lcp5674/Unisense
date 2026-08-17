"""敏感分级规则引擎测试（PII 合规增强：类别化 + 字段级明细 + 兼容 classify）。"""

from __future__ import annotations

from app.services.collector.classifier import (
    PiiCategory,
    PiiFieldHit,
    SensitivityClassifier,
)


def _schema(*cols: dict) -> dict:
    return {"columns": list(cols)}


def test_classify_basic_pii_phone() -> None:
    clf = SensitivityClassifier()
    assert clf.classify("users", _schema({"name": "phone"})) == "PII"


def test_classify_comment_trigger() -> None:
    """列注释含敏感词同样触发 PII（P0 修复保留）。"""
    clf = SensitivityClassifier()
    assert clf.classify("orders", _schema({"name": "c1", "comment": "用户手机号"})) == "PII"


def test_classify_internal() -> None:
    clf = SensitivityClassifier()
    assert clf.classify("gmv", _schema({"name": "amount"})) == "INTERNAL"


def test_classify_confidential_credential() -> None:
    clf = SensitivityClassifier()
    assert clf.classify("config", _schema({"name": "api_password"})) == "CONFIDENTIAL"


def test_classify_health_category_upgrades_to_pii() -> None:
    """新增健康类别：病历/诊断字段判 PII。"""
    clf = SensitivityClassifier()
    assert clf.classify("patient", _schema({"name": "diagnosis"})) == "PII"
    assert clf.classify("patient", _schema({"name": "blood_pressure"})) == "PII"


def test_classify_biometric_category_upgrades_to_pii() -> None:
    """新增生物特征类别：指纹/人脸判 PII。"""
    clf = SensitivityClassifier()
    assert clf.classify("auth", _schema({"name": "fingerprint"})) == "PII"
    assert clf.classify("auth", _schema({"name": "face_id"})) == "PII"


def test_classify_financial_category_upgrades_to_pii() -> None:
    """新增金融敏感类别：账户余额判 PII。"""
    clf = SensitivityClassifier()
    assert clf.classify("wealth", _schema({"name": "account_balance"})) == "PII"


def test_detect_pii_fields_structure() -> None:
    clf = SensitivityClassifier()
    hits = clf.detect_pii_fields(
        "users", _schema({"name": "id_card"}, {"name": "phone"}, {"name": "amount"})
    )
    assert isinstance(hits[0], PiiFieldHit)
    assert len(hits) == 2
    by_col = {h.column: h for h in hits}
    assert by_col["id_card"].category == PiiCategory.ID_CARD
    assert by_col["id_card"].rule == "id_card"
    assert by_col["id_card"].confidence >= 0.9
    assert by_col["phone"].category == PiiCategory.PHONE
    # 排序：置信度高在前
    assert hits[0].column == "id_card"


def test_detect_pii_fields_sample_boosts_confidence() -> None:
    clf = SensitivityClassifier()
    hits = clf.detect_pii_fields("t", _schema({"name": "phone", "sample": "13800000000"}))
    assert hits[0].matched_by == "name+sample"
    assert hits[0].confidence >= 0.95


def test_detect_pii_fields_comment_match() -> None:
    clf = SensitivityClassifier()
    hits = clf.detect_pii_fields("t", _schema({"name": "c1", "comment": "客户邮箱"}))
    assert hits and hits[0].matched_by == "comment"
    assert hits[0].category == PiiCategory.EMAIL


def test_detect_pii_fields_empty() -> None:
    assert SensitivityClassifier().detect_pii_fields("t", {"columns": [{"name": "id"}]}) == []


def test_detect_pii_fields_legacy_string_columns() -> None:
    """纯字符串列（mysql 连接器形态）兼容检测。"""
    hits = SensitivityClassifier().detect_pii_fields("t", {"columns": ["user_name"]})
    assert len(hits) == 1
    assert hits[0].category == PiiCategory.NAME


def test_classify_accepts_precomputed_hits() -> None:
    """classify 可复用 detect 结果避免重复检测。"""
    clf = SensitivityClassifier()
    schema = _schema({"name": "id_card"})
    hits = clf.detect_pii_fields("t", schema)
    assert clf.classify("t", schema, hits=hits) == "PII"


def test_injectable_rules() -> None:
    """自定义规则集注入（DB 可配置覆盖场景）。"""
    from app.services.collector.classifier import PiiRule

    custom = (
        PiiRule(
            PiiCategory.HEALTH, "health",
            r"(hospital|clinic)", None, 0.95,
        ),
    )
    clf = SensitivityClassifier(rules=custom)
    assert clf.classify("t", _schema({"name": "clinic"})) == "PII"
    # 注入规则后不再命中内置规则（如 phone）
    assert clf.classify("t", _schema({"name": "phone"})) == "INTERNAL"
