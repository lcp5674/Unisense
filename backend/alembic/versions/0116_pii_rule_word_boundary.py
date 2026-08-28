"""PII 规则词边界升级：杜绝统计/业务字段子串误判

背景（2026-08-28 用户反馈）：
- ``population`` 命中 gps 规则的 ``lat``、``thyroid_nodules`` 命中 id_no 规则的
  ``id_no``、``hotel`` 命中 phone 规则的 ``tel``、``composition`` 命中 gps 的
  ``position``——均为无词边界的子串误判；
- ``*_cnt`` / ``*_rate`` 聚合统计字段（``health_exam_cnt``、``blood_pressure_compliance_rate``、
  ``call_connected_cnt`` 等）被健康/电话规则误标 PII。

统计量词排除属检测引擎逻辑（``SensitivityClassifier.detect_pii_fields`` 内置，
对 DB 规则与内置规则统一生效，无需迁移）；本迁移仅把 DB 中 15 条**内置同 ID**
``pii_rule`` 的 ``name_re`` 升级为带「非字母数字边界」的最终版（与
``classifier.DEFAULT_PII_RULES`` 对齐）。**自定义规则与用户改动不受影响**
（仅按 code 匹配内置项更新，未知 code 跳过）。

幂等：仅 UPDATE 命中 code 的记录，可重复执行。
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0116_pii_rule_word_boundary"
down_revision = "0115_granularity_hospital_dict"
branch_labels = None
depends_on = None


def _tok(pattern: str) -> str:
    """与 classifier._tok 一致：非字母数字边界（_ 视为边界）。"""
    return f"(?<![a-zA-Z0-9]){pattern}(?![a-zA-Z0-9])"


#: 内置规则最终版（与 classifier.DEFAULT_PII_RULES 对齐；仅更新内置同 ID 项）。
_UPDATED_RULES: dict[str, dict] = {
    "id_card": {
        "category": "ID_CARD",
        "name_re": "(" + _tok(r"id_?card") + "|" + _tok(r"identity_?no") + "|shenfen|sfz|身份证)",
        "sample_re": r"^\d{17}[\dXx]$",
        "confidence": 0.95,
    },
    "phone": {
        "category": "PHONE",
        "name_re": "(phone|mobile|" + _tok(r"tel") + "|telephone|手机|电话|手机号|联系电话)",
        "sample_re": r"^1[3-9]\d{9}$",
        "confidence": 0.9,
    },
    "email": {
        "category": "EMAIL",
        "name_re": r"(email|mail_?addr|邮箱|邮件|电子邮箱)",
        "sample_re": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        "confidence": 0.9,
    },
    "real_name": {
        "category": "NAME",
        "name_re": r"(\bname\b|姓名|用户名|user_?name|cust_?name|full_?name|real_?name|昵称)",
        "sample_re": None,
        "confidence": 0.7,
    },
    "address": {
        "category": "ADDRESS",
        "name_re": "(address|" + _tok(r"addr") + "|location_detail|地址|住址|居住地|收货地址)",
        "sample_re": None,
        "confidence": 0.7,
    },
    "bank_card": {
        "category": "BANK_CARD",
        "name_re": "(bank_?card|bankcard|" + _tok(r"card_?no") + "|" + _tok(r"account_?no")
        + "|银行卡|卡号|银行账号)",
        "sample_re": r"^\d{16,19}$",
        "confidence": 0.9,
    },
    "id_no": {
        "category": "DOCUMENT",
        "name_re": "(" + _tok(r"id_no") + "|" + _tok(r"cert_no") + "|证件号|证件|"
        + _tok(r"license_no") + "|驾照)",
        "sample_re": None,
        "confidence": 0.85,
    },
    "passport": {
        "category": "PASSPORT",
        "name_re": r"(passport|护照)",
        "sample_re": None,
        "confidence": 0.85,
    },
    "gps": {
        "category": "GPS",
        "name_re": "(" + _tok(r"lat") + "|" + _tok(r"lng") + "|longitude|latitude|geo_?point|"
        + _tok(r"position") + "|定位|坐标|经纬度)",
        "sample_re": None,
        "confidence": 0.6,
    },
    "health": {
        "category": "HEALTH",
        "name_re": r"(health|medical|disease|illness|diagnos|blood|pressure|sugar|heart_?rate|bmi|病历|健康|体检|血压|血糖|心率|诊疗|医疗)",
        "sample_re": None,
        "confidence": 0.85,
    },
    "biometric": {
        "category": "BIOMETRIC",
        "name_re": r"(biometric|fingerprint|face_?id|iris|voiceprint|dna|基因|指纹|人脸|虹膜|声纹)",
        "sample_re": None,
        "confidence": 0.9,
    },
    "financial": {
        "category": "FINANCIAL",
        "name_re": r"(bank_?balance|account_?balance|余额|金融资产|投资|理财|证券|股票|持仓)",
        "sample_re": None,
        "confidence": 0.85,
    },
    "password": {
        "category": "CREDENTIAL",
        "name_re": r"(password|pwd|secret|token|credential|api_?key|密钥|口令|密码)",
        "sample_re": None,
        "confidence": 0.95,
        "pii": False,
    },
    "tax": {
        "category": "TAX",
        "name_re": r"(tax|vat|invoice|税务|税号|发票)",
        "sample_re": None,
        "confidence": 0.9,
        "pii": False,
    },
    "business": {
        "category": "BUSINESS",
        "name_re": r"(salary|工资|薪酬|income|收入|revenue|营收|profit|利润|cost|成本|price|价格)",
        "sample_re": None,
        "confidence": 0.85,
        "pii": False,
    },
}


def upgrade() -> None:
    conn = op.get_bind()
    for code, cfg in _UPDATED_RULES.items():
        conn.execute(
            sa.text(
                "UPDATE system_dict SET description = :desc, updated_at = NOW() "
                "WHERE dict_type = 'pii_rule' AND code = :code AND deleted_at IS NULL"
            ),
            {"code": code, "desc": json.dumps(cfg, ensure_ascii=False)},
        )


def downgrade() -> None:
    # 不做反向降级：规则升级不可逆（旧正则无边界，属于已知缺陷）；
    # 若确需回退，可通过配置台手工编辑或整体重建种子。
    pass
