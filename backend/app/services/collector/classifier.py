"""敏感分级规则引擎（对齐 TD §12.1「敏感分级（PII/机密）」）。

纯函数、确定性、可单测：依据实体名、字段名（schema_json.columns）以及字段注释
（column.comment）匹配 PII / 机密 关键字与令牌正则，给出 sensitivity_level 与
**字段级命中明细**（列名/类别/规则/置信度）。

规则结构为「类别化」：每条规则归属一个 ``PiiCategory``（身份证/手机/邮箱/姓名/
地址/银行卡/证件/护照/GPS/健康/生物特征/金融），便于按类别统计、行业分级模板映射
与前端按类别筛选。默认规则内置（``DEFAULT_PII_RULES``），亦可由 DB 配置
（system_dict ``pii_rule``）覆盖——``SensitivityClassifier`` 接受 ``rules`` 注入，
无 DB 依赖，保持纯函数可单测。

P0 修复（保留）：分类器的 haystack 从仅含列名扩展为「列名 + 列注释」双重匹配——
修复前列注释从未被采集，即使注释含「手机号」「客户邮箱」也无法识别 PII。
"""

from __future__ import annotations

import enum
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

#: PII 命中判定阈值：低于该置信度仅记录不升级敏感级（与 governance/policy.py 对齐）。
PII_CONFIDENCE_THRESHOLD = 0.7


class PiiCategory(enum.StrEnum):
    """PII 类别（字段级分类统计 / 行业分级模板的映射键）。"""

    ID_CARD = "ID_CARD"  # 身份证号
    PHONE = "PHONE"  # 手机/电话
    EMAIL = "EMAIL"  # 邮箱
    NAME = "NAME"  # 姓名/用户名
    ADDRESS = "ADDRESS"  # 地址/住址
    BANK_CARD = "BANK_CARD"  # 银行卡/账户
    DOCUMENT = "DOCUMENT"  # 其他证件号
    PASSPORT = "PASSPORT"  # 护照
    GPS = "GPS"  # 行踪定位
    HEALTH = "HEALTH"  # 健康医疗
    BIOMETRIC = "BIOMETRIC"  # 生物识别
    FINANCIAL = "FINANCIAL"  # 金融敏感


class ConfidentialCategory(enum.StrEnum):
    """机密类别（商业敏感 / 凭据），不参与 PII 统计。"""

    CREDENTIAL = "CREDENTIAL"  # 密码/密钥/令牌
    TAX = "TAX"  # 税务/发票
    BUSINESS = "BUSINESS"  # 工资/成本/价格等商业敏感


@dataclass(frozen=True, slots=True)
class PiiRule:
    """一条敏感识别规则。

    Attributes:
        category: 命中类别（PiiCategory 或 ConfidentialCategory）。
        rule_id: 规则标识（落库 classification.pii_columns.rule 字段，如 ``id_card``）。
        name_re: 字段名/表名/注释关键字正则。
        sample_re: 取值样本正则（可选；样本命中提升置信度）。
        confidence: 基础置信度（0-1）。
        pii: 是否计入 PII（False 为机密规则）。
    """

    category: str
    rule_id: str
    name_re: str
    sample_re: str | None
    confidence: float
    pii: bool = True


@dataclass(frozen=True, slots=True)
class PiiFieldHit:
    """一次字段级敏感命中（供 pii_columns 明细与前端展示）。"""

    column: str
    category: str
    rule: str
    confidence: float
    matched_by: str  # name | name+sample | comment
    pii: bool = True


def _compile(rule: PiiRule) -> re.Pattern:
    return re.compile(rule.name_re, re.I)


def _compile_sample(rule: PiiRule) -> re.Pattern | None:
    return re.compile(rule.sample_re) if rule.sample_re else None


#: 默认 PII 规则集（8 类既有 + 健康/生物特征/金融 3 类新增，规则键与
#: governance/policy.py PII_RULES 对齐，policy 委托本模块避免双引擎漂移）。
DEFAULT_PII_RULES: tuple[PiiRule, ...] = (
    PiiRule(
        PiiCategory.ID_CARD, "id_card",
        r"(id_?card|identity_?no|shenfen|sfz|身份证)", r"^\d{17}[\dXx]$", 0.95,
    ),
    PiiRule(
        PiiCategory.PHONE, "phone",
        r"(phone|mobile|tel|telephone|手机|电话|手机号|联系电话)", r"^1[3-9]\d{9}$", 0.9,
    ),
    PiiRule(
        PiiCategory.EMAIL, "email",
        r"(email|mail_?addr|邮箱|邮件|电子邮箱)",
        r"^[^@\s]+@[^@\s]+\.[^@\s]+$", 0.9,
    ),
    PiiRule(
        PiiCategory.NAME, "real_name",
        r"(\bname\b|姓名|用户名|user_?name|cust_?name|full_?name|real_?name|昵称)",
        None, 0.7,
    ),
    PiiRule(
        PiiCategory.ADDRESS, "address",
        r"(address|addr|location_detail|地址|住址|居住地|收货地址)", None, 0.7,
    ),
    PiiRule(
        PiiCategory.BANK_CARD, "bank_card",
        r"(bank_?card|bankcard|card_?no|account_?no|银行卡|卡号|银行账号)",
        r"^\d{16,19}$", 0.9,
    ),
    PiiRule(
        PiiCategory.DOCUMENT, "id_no",
        r"(id_no|cert_no|证件号|证件|license_no|驾照)", None, 0.85,
    ),
    PiiRule(PiiCategory.PASSPORT, "passport", r"(passport|护照)", None, 0.85),
    PiiRule(
        PiiCategory.GPS, "gps",
        r"(lat|lng|longitude|latitude|geo_?point|position|定位|坐标|经纬度)", None, 0.6,
    ),
    PiiRule(
        PiiCategory.HEALTH, "health",
        r"(health|medical|disease|illness|diagnos|blood|pressure|sugar|heart_?rate|bmi|病历|健康|体检|血压|血糖|心率|诊疗|医疗)",
        None, 0.85,
    ),
    PiiRule(
        PiiCategory.BIOMETRIC, "biometric",
        r"(biometric|fingerprint|face_?id|iris|voiceprint|dna|基因|指纹|人脸|虹膜|声纹)",
        None, 0.9,
    ),
    PiiRule(
        PiiCategory.FINANCIAL, "financial",
        r"(bank_?balance|account_?balance|余额|金融资产|投资|理财|证券|股票|持仓)",
        None, 0.85,
    ),
)

#: 默认机密规则集（密码/密钥、税务发票、商业敏感——判 CONFIDENTIAL，不计 PII）。
DEFAULT_CONFIDENTIAL_RULES: tuple[PiiRule, ...] = (
    PiiRule(
        ConfidentialCategory.CREDENTIAL, "password",
        r"(password|pwd|secret|token|credential|api_?key|密钥|口令|密码)",
        None, 0.95, pii=False,
    ),
    PiiRule(
        ConfidentialCategory.TAX, "tax",
        r"(tax|vat|invoice|税务|税号|发票)", None, 0.9, pii=False,
    ),
    PiiRule(
        ConfidentialCategory.BUSINESS, "business",
        r"(salary|工资|薪酬|income|收入|revenue|营收|profit|利润|cost|成本|price|价格)",
        None, 0.85, pii=False,
    ),
)


def _column_defs(schema_json: dict[str, Any]) -> list[dict[str, Any]]:
    """归一化列定义为 dict 列表（兼容字符串列与 dict 列两种 schema 格式）。

    postgres/clickhouse/hive/kafka 的 columns 为 ``[{"name": ..., "type": ...}]``
    结构，直接 ``str(col)`` 会引入字面量键名（如 "name"）而命中 PII 规则，
    导致这些连接器采集的每一张表都被误判为 PII（P0-1）。
    """
    out: list[dict[str, Any]] = []
    raw = schema_json.get("fields") or schema_json.get("columns") or []
    if not isinstance(raw, list):
        return out
    for col in raw:
        if isinstance(col, dict):
            out.append(col)
        elif isinstance(col, str):
            out.append({"name": col})
    return out


class SensitivityClassifier:
    """敏感分级分类器（无状态，可注入规则集用于测试/DB 配置）。"""

    def __init__(
        self,
        rules: Sequence[PiiRule] | None = None,
        confidential_rules: Sequence[PiiRule] | None = None,
    ) -> None:
        self._pii_rules = tuple(rules) if rules is not None else DEFAULT_PII_RULES
        default_conf = DEFAULT_CONFIDENTIAL_RULES
        self._conf_rules = (
            tuple(confidential_rules) if confidential_rules is not None else default_conf
        )
        self._pii_compiled = [(_compile(r), _compile_sample(r), r) for r in self._pii_rules]
        self._conf_compiled = [(_compile(r), _compile_sample(r), r) for r in self._conf_rules]

    def detect_pii_fields(
        self, entity_name: str, schema_json: dict[str, Any]
    ) -> list[PiiFieldHit]:
        """识别字段级 PII 命中明细（列名/类别/规则/置信度/匹配途径）。

        Args:
            entity_name: 实体（表/视图）名。
            schema_json: 含 ``columns``/``fields`` 的字典。

        Returns:
            命中列表，按置信度倒序；无命中返回空列表。
        """
        columns = _column_defs(schema_json)
        hits: list[PiiFieldHit] = []
        for col in columns:
            name = str(col.get("name", "")).strip()
            if not name:
                continue
            sample = str(col.get("sample", "") or "")
            comment = str(col.get("comment", "") or "").strip()
            for name_re, sample_re, rule in self._pii_compiled:
                matched_by: str | None = None
                confidence = rule.confidence
                if name_re.search(name) or (comment and name_re.search(comment)):
                    matched_by = "name" if name_re.search(name) else "comment"
                    if sample_re and sample and sample_re.match(sample):
                        confidence = min(1.0, rule.confidence + 0.05)
                        matched_by = "name+sample"
                if matched_by is None:
                    continue
                hits.append(
                    PiiFieldHit(
                        column=name,
                        category=rule.category,
                        rule=rule.rule_id,
                        confidence=confidence,
                        matched_by=matched_by,
                    )
                )
                break
        hits.sort(key=lambda h: (-h.confidence, h.column))
        return hits

    def classify(
        self,
        entity_name: str,
        schema_json: dict[str, Any],
        hits: Sequence[PiiFieldHit] | None = None,
    ) -> str:
        """返回 sensitivity_level：PII / CONFIDENTIAL / INTERNAL。

        PII 命中且置信度达到阈值判 PII；仅有低置信度 PII 命中或机密规则命中
        判 CONFIDENTIAL；均未命中返回 INTERNAL。

        Args:
            entity_name: 实体（表/视图）名。
            schema_json: 含 ``columns``/``fields`` 的字典。
            hits: 可选，``detect_pii_fields`` 的结果（避免同一 schema 重复检测）。
        """
        if hits is None:
            hits = self.detect_pii_fields(entity_name, schema_json)
        if any(h.confidence >= PII_CONFIDENCE_THRESHOLD for h in hits):
            return "PII"
        if hits:
            return "CONFIDENTIAL"
        # 机密规则检测（密码/税务/商业敏感），不计 PII
        names = " ".join(_column_names(schema_json))
        comments = " ".join(_column_comments(schema_json))
        haystack = f"{entity_name} {names} {comments}"
        for name_re, _sample_re, _rule in self._conf_compiled:
            if name_re.search(haystack):
                return "CONFIDENTIAL"
        return "INTERNAL"


def _column_names(schema_json: dict[str, Any]) -> list[str]:
    """提取列名（兼容字符串列与 dict 列），供 classify 机密检测 haystack。"""
    names: list[str] = []
    for col in _column_defs(schema_json):
        name = col.get("name")
        if name:
            names.append(str(name))
    return names


def _column_comments(schema_json: dict[str, Any]) -> list[str]:
    """提取列注释（仅非空），供 classify 机密检测 haystack。"""
    comments: list[str] = []
    for col in _column_defs(schema_json):
        comment = col.get("comment")
        if comment and str(comment).strip():
            comments.append(str(comment).strip())
    return comments
