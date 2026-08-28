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


def test_aggregate_suffix_health_cnt_not_pii() -> None:
    """*_cnt/*_rate 聚合统计字段不因健康词命中判 PII（用户误报回归）。"""
    clf = SensitivityClassifier()
    for col in (
        "health_exam_cnt",
        "bmi_check_cnt",
        "blood_pressure_compliance_rate",
        "gxy_manage_rate",
        "chronic_disease_cnt",
        "disease_population_cnt1",
        "disease_cnt2",
        "oper_background_disease_cnt3",
        "bmi_check_cnt_1d",
        "d_accpayph_cnt_zjwz_180d",
    ):
        assert clf.detect_pii_fields("t", _schema({"name": col})) == [], col


def test_aggregate_suffix_comment_trigger_not_pii() -> None:
    """统计字段即使注释含敏感词（电话/血压）也不判 PII。"""
    clf = SensitivityClassifier()
    assert clf.detect_pii_fields(
        "t", _schema({"name": "call_connected_cnt", "comment": "电话接通数"})
    ) == []
    assert clf.detect_pii_fields(
        "t", _schema({"name": "plan_cnt", "comment": "健康管理计划数"})
    ) == []


def test_heart_rate_value_exempt_from_aggregate_exclusion() -> None:
    """heart_rate 是个人测量值（值型豁免），即便 *_rate 后缀仍判 PII。"""
    clf = SensitivityClassifier()
    hits = clf.detect_pii_fields("t", _schema({"name": "heart_rate"}))
    assert hits and hits[0].rule == "health"
    # 心率状态/ID 不以统计后缀结尾，同样保留
    assert clf.detect_pii_fields("t", _schema({"name": "heart_rate_state"}))[0].rule == "health"
    # heart_rate 前缀的派生字段（状态/ID）同样豁免统计量词排除
    assert clf.detect_pii_fields("t", _schema({"name": "heart_rate_status_id"}))[0].rule == "health"


def test_aggregate_with_sample_hit_keeps_pii() -> None:
    """统计字段若样本值命中（实际存个体值，如异常手机号）仍保留 PII。"""
    clf = SensitivityClassifier()
    hits = clf.detect_pii_fields(
        "t", _schema({"name": "phone_cnt", "sample": "13800000000"})
    )
    assert hits and hits[0].matched_by == "name+sample"


def test_gps_no_substring_false_positive() -> None:
    """lat 子串不误判 population（词边界修复）。"""
    clf = SensitivityClassifier()
    assert clf.detect_pii_fields("t", _schema({"name": "population"})) == []
    assert clf.detect_pii_fields("t", _schema({"name": "position"})) != []
    assert clf.detect_pii_fields("t", _schema({"name": "composition"})) == []


def test_id_no_no_substring_false_positive() -> None:
    """id_no 不误判 thyroid_nodules（词边界修复）。"""
    clf = SensitivityClassifier()
    assert clf.detect_pii_fields("t", _schema({"name": "thyroid_nodules"})) == []
    assert clf.detect_pii_fields("t", _schema({"name": "doctor_id_no"}))[0].rule == "id_no"


def test_phone_tel_no_substring_false_positive() -> None:
    """tel 不误判 hotel；真实 tel 字段仍命中。"""
    clf = SensitivityClassifier()
    assert clf.detect_pii_fields("t", _schema({"name": "hotel"})) == []
    assert clf.detect_pii_fields("t", _schema({"name": "apply_tel"}))[0].rule == "phone"


def test_real_name_bare_name_entity_context() -> None:
    """裸 name 列依据表名语义判定：机构/地点/无法判断表不判，人员表判。"""
    clf = SensitivityClassifier()
    # 村/部门计数表 → 村名/机构名，非个人姓名
    assert clf.detect_pii_fields(
        "wedw_tmp.village_department_count_dp_tmp1", _schema({"name": "name", "comment": "村名"})
    ) == []
    assert clf.detect_pii_fields(
        "wedw_tmp.village_department_count_dp_tmp1", _schema({"name": "name"})
    ) == []
    # 患者/用户表 → 个人姓名
    assert (
        clf.detect_pii_fields("ods.patient_info", _schema({"name": "name"}))[0].rule
        == "real_name"
    )
    assert clf.detect_pii_fields("dwd.sys_user", _schema({"name": "name"}))[0].rule == "real_name"
    # 学生/教师表 → 个人姓名（人员语义词表覆盖）
    assert clf.detect_pii_fields("stg.student", _schema({"name": "name"}))[0].rule == "real_name"
    assert clf.detect_pii_fields("stg.teacher", _schema({"name": "name"}))[0].rule == "real_name"
    # 无法判断的表（临时导出表）→ 保守不判
    assert clf.detect_pii_fields("dwd.tmp_export_20260801", _schema({"name": "name"})) == []


def test_real_name_prefixed_person_vs_entity() -> None:
    """带前缀 name 列：人名前缀判 PII，机构/技术前缀不判。"""
    clf = SensitivityClassifier()
    person_names = ["patient_name", "user_name", "doctor_name", "contact_name", "applicant_name"]
    for col in person_names:
        hits = clf.detect_pii_fields("ods.visit", _schema({"name": col}))
        assert hits[0].rule == "real_name", col
    person_names2 = ["student_name", "teacher_name", "member_name", "operator_name"]
    for col in person_names2:
        hits = clf.detect_pii_fields("ods.t", _schema({"name": col}))
        assert hits[0].rule == "real_name", col
    entity_names = [
        "village_name", "hospital_name", "dept_name", "org_name", "table_name", "job_name",
    ]
    for col in entity_names:
        assert clf.detect_pii_fields("ods.t", _schema({"name": col})) == [], col


def test_real_name_comment_hit_keeps_pii() -> None:
    """裸 name 列注释明确写「姓名」时仍判 PII（name+comment，不因上下文拦截）。"""
    clf = SensitivityClassifier()
    hits = clf.detect_pii_fields(
        "wedw_tmp.village_department_count_dp_tmp1",
        _schema({"name": "name", "comment": "姓名"}),
    )
    assert hits and hits[0].rule == "real_name" and hits[0].matched_by == "name+comment"


def test_health_org_field_comment_not_pii() -> None:
    """health 规则：机构/地点字段不因注释含「医疗」等词判 PII；健康字段保留。"""
    clf = SensitivityClassifier()
    # 机构/病区字段 → 不判（注释「医疗机构名称」「病区名称」命中 health 但字段是机构）
    schema = _schema(
        {"name": "org_name", "comment": "医疗机构名称"},
        {"name": "ward_name", "comment": "病区名称"},
        {"name": "org_code", "comment": "医疗机构代码"},
    )
    assert clf.detect_pii_fields("wedw_dwd.emr_t", schema) == []
    # 明确健康字段 → 保留（列名含诊断/病名，name 命中不受影响）
    schema2 = _schema(
        {"name": "diseasename", "comment": "疾病名称"},
        {"name": "diagnosis", "comment": "诊断结果"},
        {"name": "blood_pressure", "comment": "血压值"},
    )
    hits = clf.detect_pii_fields("wedw_dwd.emr_t", schema2)
    assert {h.column for h in hits} == {"diseasename", "diagnosis", "blood_pressure"}
