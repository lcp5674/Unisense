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


#: PII 类别中文标签（敏感规则配置台/明细列表展示用）。
PII_CATEGORY_LABELS: dict[str, str] = {
    PiiCategory.ID_CARD: "身份证号",
    PiiCategory.PHONE: "手机/电话",
    PiiCategory.EMAIL: "邮箱",
    PiiCategory.NAME: "姓名/用户名",
    PiiCategory.ADDRESS: "地址",
    PiiCategory.BANK_CARD: "银行卡",
    PiiCategory.DOCUMENT: "证件号",
    PiiCategory.PASSPORT: "护照",
    PiiCategory.GPS: "行踪定位",
    PiiCategory.HEALTH: "健康医疗",
    PiiCategory.BIOMETRIC: "生物特征",
    PiiCategory.FINANCIAL: "金融敏感",
}

#: 机密类别中文标签（敏感规则配置台展示用）。
CONFIDENTIAL_CATEGORY_LABELS: dict[str, str] = {
    ConfidentialCategory.CREDENTIAL: "密码/密钥",
    ConfidentialCategory.TAX: "税务/发票",
    ConfidentialCategory.BUSINESS: "商业敏感",
}


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
    # 命中字段的脱敏样本值（仅供治理复核展示，已打码；空串表示未采样）。
    # 与 matched_by == "name+sample" 配合：样本是双重验证的证据来源。
    sample: str = ""


def _tok(pattern: str) -> str:
    """把正则片段包成「非字母数字边界」令牌，防子串误判。

    ``(?<![a-zA-Z0-9])...(?![a-zA-Z0-9])`` 与 ``\\b`` 的区别：下划线视为边界
    （兼容 ``doctor_id_no``、``apply_card_no`` 这类业务命名），同时杜绝
    ``population`` 命中 ``lat``、``thyroid_nodules`` 命中 ``id_no``、
    ``hotel`` 命中 ``tel``、``composition`` 命中 ``position`` 等跨词误判。
    """
    return f"(?<![a-zA-Z0-9]){pattern}(?![a-zA-Z0-9])"


#: 聚合统计量词正则：字段名含此量词 token（可带分桶编号/业务后缀，如 ``_cnt1``、
#: ``_cnt_1d``、``_cnt_zjwz_180d``）视为聚合指标（存群体计数/比率，不指向个体）。
#: 命中此类字段时，名称/注释关键词不再判定 PII（除非样本值命中，说明实际存个体值）。
_AGGREGATE_SRC = (
    r"_(cnt|count|qty|quantity|rate|ratio|pct|percent|avg|sum|total|amount|amt|times)"
    r"(?:_[a-z0-9]+(?:_[a-z0-9]+)*|\d*)$"
)

#: 值型豁免前缀：即便字段名带统计量词，仍是个人测量值/标识（如 heart_rate 心率）。
_VALUE_EXEMPT_PREFIXES: tuple[str, ...] = ("heart_rate", "heartrate", "心率")

#: 人名限定前缀：带此前缀的 ``*_name`` 列是个人姓名（患者/用户/会员/医生…），判 PII。
_PERSON_NAME_SRC = (
    r"(patient|user|cust|customer|member|doctor|physician|applicant|contact|owner|"
    r"receiver|sender|payee|holder|guardian|parent|child|spouse|operator|manager|"
    r"teacher|nurse|pharmacist|leader|handler|clerk|staff|employee|principal|director|"
    r"assistant|secretary|student|pupil|"
    r"家属|患者|用户|会员|医生|申请人|联系人|收款|付款|监护人|家长|子女|配偶|"
    r"操作员|经办人|护士|教师|老师|学生|学员|负责人|员工|主任|助理|秘书)_?name$"
)

#: 机构/地点/技术限定前缀：带此前缀的 ``*_name`` 是机构/地点/对象/技术名
#: （村名/医院名/表名…），非个人姓名。
_ENTITY_NAME_SRC = (
    r"(village|org|dept|department|hospital|company|institution|center|team|group|"
    r"project|region|area|zone|branch|unit|enterprise|brand|store|warehouse|school|"
    r"class|community|city|county|province|town|street|clinic|pharmacy|factory|plant|"
    r"shop|station|building|room|ward|bed|host|server|table|column|schema|db|database|"
    r"file|job|task|rule|template|config|menu|module|function|dict|param|setting|index|"
    r"村|社区|部门|机构|医院|单位|项目|组织|科室|学校|班级|地区|区域|城市|区县|省份|"
    r"街道|药店|诊所|工厂|商店|车站|大楼|房间|病房|床位|菜单|模块|功能|字典|参数|"
    r"设置|模板|任务|作业|文件|表|索引|库|主机|服务)_?name$"
)

#: 人员语义表名：裸 ``name`` 列所在实体含人员语义（患者表/用户表/学生表…）时视为姓名。
_PERSON_ENTITY_SRC = (
    r"(patient|user|member|doctor|staff|people|person|customer|client|employee|"
    r"student|teacher|pupil|nurse|pharmacist|parent|child|spouse|leader|manager|"
    r"operator|worker|"
    r"患者|用户|会员|医生|员工|人员|职工|病人|学生|教师|老师|学员|护士|药剂师|"
    r"家长|子女|配偶|负责人|经理|操作员|工人)"
)

#: 机构/地点/技术语义表名：裸 ``name`` 列所在实体含机构语义（村/部门/医院…）时视为机构名。
_ENTITY_ENTITY_SRC = (
    r"(village|org|dept|department|hospital|company|institution|center|team|group|"
    r"project|region|area|zone|branch|unit|enterprise|store|warehouse|school|class|"
    r"community|city|county|province|town|street|clinic|pharmacy|factory|plant|shop|"
    r"station|building|room|ward|bed|host|server|table|column|schema|db|database|file|"
    r"job|task|rule|template|config|menu|module|function|dict|param|setting|index|"
    r"村|社区|部门|机构|医院|单位|项目|组织|科室|学校|班级|地区|区域|城市|区县|省份|"
    r"街道|药店|诊所|工厂|商店|车站|大楼|房间|病房|床位|菜单|模块|功能|字典|参数|"
    r"设置|模板|任务|作业|文件|表|索引|库|主机|服务)"
)

#: health 规则「机构/地点/资源」字段：注释命中健康词但字段本身是机构/位置（如
#: ``org_name`` 注释「医疗机构名称」），不是个人健康数据，应降级不判 PII。
_HEALTH_ORG_SRC = (
    r"(org|organ|hospital|dept|department|clinic|institution|company|unit|branch|"
    r"area|region|zone|ward|room|bed|source|"
    r"机构|医院|科室|部门|单位|病区|病房|房间|床位|来源)"
)

#: health 规则「明确健康字段」：字段名本身是个人健康数据（保留 PII）。
_HEALTH_KEEP_SRC = (
    r"(disease|diagnos|symptom|complaint|blood|pressure|sugar|heart|bmi|"
    r"病名|诊断|症状|主诉|血压|血糖|心率|体检|化验|检查)"
)


@dataclass(frozen=True, slots=True)
class PiiVocab:
    """PII 上下文词表（可 DB 配置覆盖，system_dict ``pii_vocab``）。

    与规则（``pii_rule``）分离：规则定义「什么算敏感」（正则+置信度+类别），
    词表定义「上下文判定」——人名/机构前缀、表语义、健康降级、聚合量词、豁免。
    治理者可在敏感规则配置台调整词表（豁免误报字段、补充人员/机构词），
    无需改代码发版。
    """

    person_name_re: str = _PERSON_NAME_SRC
    entity_name_re: str = _ENTITY_NAME_SRC
    person_entity_re: str = _PERSON_ENTITY_SRC
    entity_entity_re: str = _ENTITY_ENTITY_SRC
    health_org_re: str = _HEALTH_ORG_SRC
    health_keep_re: str = _HEALTH_KEEP_SRC
    aggregate_re: str = _AGGREGATE_SRC
    value_exempt_prefixes: tuple[str, ...] = _VALUE_EXEMPT_PREFIXES
    # 豁免：精确字段名（误报反馈一键写入）与字段名前缀（灵活豁免）
    exempt_fields: frozenset[str] = frozenset()
    exempt_prefixes: tuple[str, ...] = ()


def _is_person_name(
    name: str,
    entity_name: str,
    person_name_re: re.Pattern,
    entity_name_re: re.Pattern,
    person_entity_re: re.Pattern,
    entity_entity_re: re.Pattern,
) -> bool:
    """判断 ``*_name`` 列是否为个人姓名（供 real_name 规则上下文判定）。

    带人名限定前缀（``patient_name``/``用户姓名``）→ 姓名；带机构/地点/技术前缀
    （``village_name``/``table_name``/``村名``）→ 非姓名；裸 ``name`` 依据表名语义：
    人员语义表（``patient_info``/``用户表``）→ 姓名；机构语义表（``village_*``/``部门表``）
    → 非姓名；无法判断 → 保守视为非姓名（留人工复核，宁缺勿滥）。
    """
    if person_name_re.search(name):
        return True
    if entity_name_re.search(name):
        return False
    if name.lower() == "name":
        if person_entity_re.search(entity_name):
            return True
        if entity_entity_re.search(entity_name):
            return False
        return False
    return False


def _is_health_pii_field(name: str, keep_re: re.Pattern, org_re: re.Pattern) -> bool:
    """判断 health 规则命中字段是否为个人健康数据（供注释命中上下文判定）。

    字段名含明确健康词（``disease_name``/``blood_pressure``）→ 保留 PII；
    字段名是机构/地点/资源（``org_name``/``ward_name``/``hospital_code``）→ 非健康
    数据（注释里的「医疗」等词来自字段说明），降级；其余保守保留。
    """
    if keep_re.search(name):
        return True
    return not bool(org_re.search(name))


def _compile(rule: PiiRule) -> re.Pattern:
    return re.compile(rule.name_re, re.I)


def _compile_sample(rule: PiiRule) -> re.Pattern | None:
    return re.compile(rule.sample_re) if rule.sample_re else None


#: 默认 PII 规则集（8 类既有 + 健康/生物特征/金融 3 类新增，规则键与
#: governance/policy.py PII_RULES 对齐，policy 委托本模块避免双引擎漂移）。
DEFAULT_PII_RULES: tuple[PiiRule, ...] = (
    PiiRule(
        PiiCategory.ID_CARD, "id_card",
        "(" + _tok(r"id_?card") + "|" + _tok(r"identity_?no") + "|shenfen|sfz|身份证)",
        r"^\d{17}[\dXx]$", 0.95,
    ),
    PiiRule(
        PiiCategory.PHONE, "phone",
        "(phone|mobile|" + _tok(r"tel") + "|telephone|手机|电话|手机号|联系电话)",
        r"^1[3-9]\d{9}$", 0.9,
    ),
    PiiRule(
        PiiCategory.EMAIL, "email",
        r"(email|mail_?addr|邮箱|邮件|电子邮箱)",
        r"^[^@\s]+@[^@\s]+\.[^@\s]+$", 0.9,
    ),
    PiiRule(
        PiiCategory.NAME, "real_name",
        r"(_?name$|姓名|用户名|昵称)",
        None, 0.7,
    ),
    PiiRule(
        PiiCategory.ADDRESS, "address",
        "(address|" + _tok(r"addr") + "|location_detail|地址|住址|居住地|收货地址)", None, 0.7,
    ),
    PiiRule(
        PiiCategory.BANK_CARD, "bank_card",
        "(bank_?card|bankcard|" + _tok(r"card_?no") + "|" + _tok(r"account_?no")
        + "|银行卡|卡号|银行账号)",
        r"^\d{16,19}$", 0.9,
    ),
    PiiRule(
        PiiCategory.DOCUMENT, "id_no",
        "(" + _tok(r"id_no") + "|" + _tok(r"cert_no") + "|证件号|证件|"
        + _tok(r"license_no") + "|驾照)",
        None, 0.85,
    ),
    PiiRule(PiiCategory.PASSPORT, "passport", r"(passport|护照)", None, 0.85),
    PiiRule(
        PiiCategory.GPS, "gps",
        "(" + _tok(r"lat") + "|" + _tok(r"lng") + "|longitude|latitude|geo_?point|"
        + _tok(r"position") + "|定位|坐标|经纬度)",
        None, 0.6,
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


#: 样本打码掩码常量（打码后保留格式特征，用于 name+sample 验证与展示）。
_MASK_STAR = "****"


def _mask_phone(value: str) -> str:
    """手机号打码：138****1234（保留前 3 后 4）。"""
    return f"{value[:3]}{_MASK_STAR}{value[-4:]}"


def _mask_id_card(value: str) -> str:
    """身份证打码：110***********1234（保留前 6 后 4）。"""
    return f"{value[:6]}{'*' * 8}{value[-4:]}"


def _mask_email(value: str) -> str:
    """邮箱打码：ab***@domain（保留本地前 2 字符）。"""
    local, _, domain = value.partition("@")
    head = local[:2] if len(local) > 2 else local[:1] or "x"
    return f"{head}***@{domain}"


def _mask_bank_card(value: str) -> str:
    """银行卡打码：6222****1234（保留前 4 后 4）。"""
    return f"{value[:4]}{_MASK_STAR}{value[-4:]}"


class SensitivityClassifier:
    """敏感分级分类器（无状态，可注入规则集用于测试/DB 配置）。"""

    def __init__(
        self,
        rules: Sequence[PiiRule] | None = None,
        confidential_rules: Sequence[PiiRule] | None = None,
        vocab: PiiVocab | None = None,
    ) -> None:
        self._pii_rules = tuple(rules) if rules is not None else DEFAULT_PII_RULES
        default_conf = DEFAULT_CONFIDENTIAL_RULES
        self._conf_rules = (
            tuple(confidential_rules) if confidential_rules is not None else default_conf
        )
        self._pii_compiled = [(_compile(r), _compile_sample(r), r) for r in self._pii_rules]
        self._conf_compiled = [(_compile(r), _compile_sample(r), r) for r in self._conf_rules]
        # 上下文词表（pii_vocab DB 可配置覆盖；缺省内置默认）
        v = vocab or PiiVocab()
        self._person_name_re = re.compile(v.person_name_re, re.I)
        self._entity_name_re = re.compile(v.entity_name_re, re.I)
        self._person_entity_re = re.compile(v.person_entity_re, re.I)
        self._entity_entity_re = re.compile(v.entity_entity_re, re.I)
        self._health_keep_re = re.compile(v.health_keep_re, re.I)
        self._health_org_re = re.compile(v.health_org_re, re.I)
        self._aggregate_re = re.compile(v.aggregate_re, re.I)
        self._value_exempt_prefixes = v.value_exempt_prefixes
        self._exempt_fields = v.exempt_fields
        self._exempt_prefixes = v.exempt_prefixes

    @staticmethod
    def classify_sample(sample: str) -> str | None:
        """判定原始样本值命中的敏感格式，返回 ``rule_id``（phone/id_card/...）。

        与 ``mask_sample`` 共用同一套格式判定（单一事实来源，避免两处正则漂移）。
        采样时调用本方法把 ``rule_id`` 与打码样本一并落库，供
        ``detect_pii_fields`` **精确**判定类别——掩码会丢失格式特征
        （``138****1234`` 既不匹配手机号也不匹配身份证正则），仅凭「含掩码标记」
        无法区分是哪个类别，会让手机号被排在前的 id_card 规则误判。

        Args:
            sample: 采集到的原始样本值（明文，不落库）。

        Returns:
            命中的 rule_id；非敏感值返回 None。
        """
        if not sample:
            return None
        s = sample.strip()
        if re.fullmatch(r"1[3-9]\d{9}", s):
            return "phone"
        if re.fullmatch(r"\d{17}[\dXx]", s):
            return "id_card"
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s):
            return "email"
        if re.fullmatch(r"\d{16,19}", s):
            return "bank_card"
        return None

    def mask_sample(self, sample: str) -> str:
        """对样本值打码后存储（PII 识别只需要格式特征，不需要明文）。

        命中敏感格式（手机/身份证/邮箱/银行卡）→ 打码保留格式特征；非敏感值
        原样返回（普通业务值无需打码，仍可用于 name+sample 验证）。

        注意：打码结果**不能**反推类别，类别由采样侧调用 ``classify_sample``
        单独落库为 ``columns[].sample_rule``。

        Args:
            sample: 采集到的原始样本值。

        Returns:
            打码后的样本值（非敏感值原样返回）。
        """
        if not sample:
            return sample
        s = sample.strip()
        rule_id = self.classify_sample(s)
        if rule_id == "phone":
            return _mask_phone(s)
        if rule_id == "id_card":
            return _mask_id_card(s)
        if rule_id == "email":
            return _mask_email(s)
        if rule_id == "bank_card":
            return _mask_bank_card(s)
        return s

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
            # 豁免（误报反馈闭环写入 pii_vocab）：精确字段名或前缀命中 → 跳过不判
            if name in self._exempt_fields or name.startswith(self._exempt_prefixes):
                continue
            # 样本值：采样落库为列表（多值脱敏样本）；存量/手填场景可能为单值字符串
            raw_sample = col.get("sample", "") or ""
            if isinstance(raw_sample, list):
                sample_list = [str(x) for x in raw_sample if x]
            else:
                sample_list = [str(raw_sample)] if raw_sample else []
            comment = str(col.get("comment", "") or "").strip()
            # 聚合统计字段（*_cnt/*_rate 等，可带分桶编号）存群体计数/比率，不指向个体；
            # 但 heart_rate（心率）等个人测量值豁免（以值型前缀开头）。
            is_aggregate = bool(self._aggregate_re.search(name)) and not name.startswith(
                self._value_exempt_prefixes
            )
            for name_re, sample_re, rule in self._pii_compiled:
                matched_by: str | None = None
                confidence = rule.confidence
                # 样本命中的三种证据（按可靠性降序）：
                # 1) sample_rule 精确等于本规则的 rule_id —— 采样时对明文跑过
                #    classify_sample，类别确定（掩码会丢失格式特征，故必须记录）；
                # 2) sample_re 匹配未打码的样本值（存量/手填场景）；
                # 3) 打码样本（含掩码标记）证明该列存敏感值，但**无法区分类别**——
                #    仅作佐证不可独立命中，否则手机号会被靠前的 id_card 规则误判。
                sample_rule = str(col.get("sample_rule", "") or "")
                sample_hit = bool(
                    sample_re
                    and sample_list
                    and (
                        (sample_rule and sample_rule == rule.rule_id)
                        or any(sample_re.match(s) for s in sample_list)
                    )
                )
                # 打码样本（存量无 sample_rule）：只知道"敏感"、不知道"哪类"
                masked_sensitive = bool(any(_MASK_STAR in s for s in sample_list))
                if name_re.search(name) or (comment and name_re.search(comment)):
                    name_hit = bool(name_re.search(name))
                    comment_hit = bool(comment and name_re.search(comment))
                    if name_hit and comment_hit:
                        matched_by = "name+comment"
                    elif name_hit:
                        matched_by = "name"
                    else:
                        matched_by = "comment"
                    # 名称/注释已命中 + 样本佐证 → 双重验证，置信度上调。
                    # 打码样本（类别未知）也可作佐证：类别已由名称确定，不引入误判。
                    if sample_hit or masked_sensitive:
                        confidence = min(1.0, rule.confidence + 0.05)
                        matched_by = "name+sample"
                elif sample_hit:
                    # 仅样本命中：字段名无语义（如 col_07 / contact）但实际存敏感值——
                    # 这正是采样的核心价值（名称驱动规则无法发现的隐藏 PII）。
                    # 置信度下调（低于名称命中），高置信规则仍自动判 PII，
                    # 边缘情形落入 NEEDS_REVIEW 交人工复核（宁缺勿滥）。
                    matched_by = "sample"
                    confidence = max(0.0, rule.confidence - 0.15)
                if matched_by is None:
                    continue
                # 统计字段不因名称/注释关键词判 PII；仅当样本命中（实际存个体值）才保留
                if is_aggregate and "sample" not in matched_by:
                    continue
                # 裸 name/机构语义 name 需上下文判定（村名/机构名 ≠ 个人姓名）；
                # 仅对字段名命中特判，注释命中（明确写了「姓名」）不受影响
                if (
                    rule.rule_id == "real_name"
                    and matched_by == "name"
                    and not _is_person_name(
                        name,
                        entity_name,
                        self._person_name_re,
                        self._entity_name_re,
                        self._person_entity_re,
                        self._entity_entity_re,
                    )
                ):
                    continue
                # 机构/地点/资源字段（org_name/ward_name 等）不因注释含「医疗」等
                # 词判健康 PII；明确健康字段（disease_name/blood_pressure）保留
                if (
                    rule.rule_id == "health"
                    and matched_by == "comment"
                    and not _is_health_pii_field(name, self._health_keep_re, self._health_org_re)
                ):
                    continue
                hits.append(
                    PiiFieldHit(
                        column=name,
                        category=rule.category,
                        rule=rule.rule_id,
                        confidence=confidence,
                        matched_by=matched_by,
                        sample=sample_list[0] if sample_list else "",
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
