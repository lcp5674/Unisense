"""初始化 seed 脚本：清除 E2E/测试参照数据，灌入微医业务参照数据。

以微医实际业务全版图组织参照数据，覆盖十四大业务主线：医疗（诊疗）、医药
（药品）、医保、挂号、健康管理、健共体（数字健共体·区域医疗协作）、医生
管理（供给侧核心资产）、保险（商保直付/经纪）、会员权益、医疗AI与数据、
临床科研、医药供应链、康复护理养老、专科运营、患者服务运营。覆盖三类主数据
（对齐 TD §12.14/§12.15 / FR-05 / FR-08 / FR-012）：
- 主题域（subject_domain）：保留既有 7 医疗域 + uncategorized 及微医线上业务
  一级域，补齐健共体/医生管理/保险/会员/AI/科研/供应链/养老/专科/患者服务
  一级域及子域，覆盖企业全部业务线。
- 术语（term）：清除 E2E/测试术语及关联（term_version/term_relation/
  glossary_conflict），灌入全业务线核心术语。
- 维度（dimension）：清除 E2E/测试维度及引用（dimension_member/
  metric_dimension/reconciliation/dimension_mapping），灌入全业务线
  维度+成员。维度已存在但成员集合与脚本不一致时自动刷新（删旧重灌），保证
  参照数据与脚本同步。

用法:
    poetry run python -m scripts.seed_medical_reference

幂等：清除按 E2E/测试编码前缀匹配（已删则跳过），灌入按唯一编码查存在则跳过。
不动 seed_e2e_data.py（E2E 测试跑时自愈重建自己的维度/术语）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# 将 backend/ 加入 sys.path，确保能 import app
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import structlog  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.logging import configure_logging  # noqa: E402
from app.db.mysql import async_session_factory  # noqa: E402
from app.models.dimension import (  # noqa: E402
    Dimension,
    DimensionMember,
    DimensionMapping,
    MetricDimension,
    Reconciliation,
)
from app.models.glossary import (  # noqa: E402
    GlossaryConflict,
    TermRelation,
    TermVersion,
)
from app.models.subject_domain import SubjectDomain  # noqa: E402
from app.models.term import Term  # noqa: E402

logger = structlog.get_logger("unisense.seed")

# ---- 主数据 owner：与既有数据保持一致（现有 term/dimension owner_id=3 admin；
# 主题域沿用 seed_domains_dicts 的 owner_id=1）----
TERM_OWNER_ID = 3
DIM_OWNER_ID = 3
DOMAIN_OWNER_ID = 1

# E2E/测试参照数据编码前缀（清除匹配，覆盖 E2E 重建后再次执行场景）
_MOCK_DIM_PREFIXES = (
    "sales_e2e_",
    "e2e_",
    "test_",
    "batch_",
    "uncategorized_time",
    "outpatient_e2e_",
)
_MOCK_TERM_PREFIXES = (
    "user_e2e_",
    "sales_e2e_",
    "outpatient_e2e_",
    "medical_fee_e2e_",
    "test_",
    "batch_",
)


# ---------------------------------------------------------------------------
# 主题域（六大业务主线：医疗/医药/医保/挂号/健康管理/健共体，均含二级子域）
# ---------------------------------------------------------------------------
# 仅声明需「确保存在」的域节点：一级域已存在则跳过（保留 seed_domains_dicts 创建结果），
# 二级子域按 code 查存在则跳过。parent 用一级域 code 引用，灌入时解析为 id。
DOMAIN_SEEDS: list[dict[str, Any]] = [
    # ---- 线下 HIS 医疗域（一级已存在，补二级）----
    {"code": "registration", "name": "挂号", "parent": "outpatient", "sort_order": 1},
    {"code": "visit", "name": "就诊", "parent": "outpatient", "sort_order": 2},
    {"code": "triage", "name": "分诊导诊", "parent": "outpatient", "sort_order": 3},
    {"code": "prescription", "name": "处方", "parent": "medication", "sort_order": 1},
    {"code": "drug_category", "name": "药品分类", "parent": "medication", "sort_order": 2},
    {"code": "pharmacy", "name": "药房", "parent": "medication", "sort_order": 3},
    {"code": "fee_item", "name": "收费项目", "parent": "medical_fee", "sort_order": 1},
    {"code": "settlement", "name": "费用结算", "parent": "medical_fee", "sort_order": 2},
    {"code": "refund", "name": "退费", "parent": "medical_fee", "sort_order": 3},
    {"code": "insurance_type", "name": "险种", "parent": "medical_insurance", "sort_order": 1},
    {"code": "yb_settle", "name": "医保结算", "parent": "medical_insurance", "sort_order": 2},
    {"code": "disease", "name": "病种", "parent": "diagnosis", "sort_order": 1},
    {"code": "icd", "name": "诊断编码", "parent": "diagnosis", "sort_order": 2},
    {"code": "medical_record", "name": "病历质控", "parent": "quality", "sort_order": 1},
    {"code": "adverse_event", "name": "不良事件", "parent": "quality", "sort_order": 2},
    {"code": "benchmark", "name": "质量基准", "parent": "quality", "sort_order": 3},
    {"code": "profile", "name": "患者档案", "parent": "patient", "sort_order": 1},
    {"code": "health_record", "name": "健康档案", "parent": "patient", "sort_order": 2},
    # ---- 微医线上业务一级域 ----
    {"code": "online_consultation", "name": "在线问诊", "parent": None, "sort_order": 8},
    {"code": "text_consult", "name": "图文问诊", "parent": "online_consultation", "sort_order": 1},
    {"code": "phone_consult", "name": "电话问诊", "parent": "online_consultation", "sort_order": 2},
    {"code": "video_consult", "name": "视频问诊", "parent": "online_consultation", "sort_order": 3},
    {"code": "internet_hospital", "name": "互联网医院", "parent": None, "sort_order": 9},
    {"code": "online_followup", "name": "在线复诊", "parent": "internet_hospital", "sort_order": 1},
    {"code": "e_prescription", "name": "电子处方", "parent": "internet_hospital", "sort_order": 2},
    {"code": "drug_delivery", "name": "药品配送", "parent": "internet_hospital", "sort_order": 3},
    {"code": "appointment", "name": "预约挂号", "parent": None, "sort_order": 10},
    {"code": "register_source", "name": "号源", "parent": "appointment", "sort_order": 1},
    {"code": "reservation", "name": "预约", "parent": "appointment", "sort_order": 2},
    {"code": "wait_queue", "name": "候诊队列", "parent": "appointment", "sort_order": 3},
    {"code": "health_management", "name": "健康管理", "parent": None, "sort_order": 11},
    {"code": "checkup", "name": "体检", "parent": "health_management", "sort_order": 1},
    {"code": "chronic_disease", "name": "慢病管理", "parent": "health_management", "sort_order": 2},
    {"code": "family_doctor", "name": "家庭医生", "parent": "health_management", "sort_order": 3},
    # ---- 健共体（微医数字健共体：区域医疗协作共同体，一级域）----
    {"code": "health_community", "name": "健共体", "parent": None, "sort_order": 12},
    {"code": "medical_alliance", "name": "医联体", "parent": "health_community", "sort_order": 1},
    {"code": "medical_community", "name": "医共体", "parent": "health_community", "sort_order": 2},
    {"code": "referral", "name": "双向转诊", "parent": "health_community", "sort_order": 3},
    {"code": "remote_consult", "name": "远程会诊", "parent": "health_community", "sort_order": 4},
    {"code": "family_doctor_sign", "name": "家医签约", "parent": "health_community", "sort_order": 5},
    {"code": "shared_resource", "name": "资源共享", "parent": "health_community", "sort_order": 6},
    {"code": "public_health", "name": "公共卫生", "parent": "health_community", "sort_order": 7},
    {"code": "family_bed", "name": "家庭病床", "parent": "health_community", "sort_order": 8},
    # ---- 医生管理（供给侧核心资产，一级域：资质/排班/绩效/多点执业/团队）----
    {"code": "doctor_management", "name": "医生管理", "parent": None, "sort_order": 13},
    {"code": "doctor_qualification", "name": "医生资质", "parent": "doctor_management", "sort_order": 1},
    {"code": "doctor_schedule", "name": "医生排班", "parent": "doctor_management", "sort_order": 2},
    {"code": "doctor_performance", "name": "医生绩效", "parent": "doctor_management", "sort_order": 3},
    {"code": "multi_site_practice", "name": "多点执业", "parent": "doctor_management", "sort_order": 4},
    {"code": "doctor_team", "name": "医生团队", "parent": "doctor_management", "sort_order": 5},
    # ---- 各业务主线补充子域 ----
    {"code": "exam_lab", "name": "检查检验", "parent": "outpatient", "sort_order": 4},
    {"code": "surgery", "name": "手术", "parent": "outpatient", "sort_order": 5},
    {"code": "inpatient", "name": "住院", "parent": "outpatient", "sort_order": 6},
    {"code": "drug_inventory", "name": "药品库存", "parent": "medication", "sort_order": 4},
    {"code": "drug_procurement", "name": "药品采购", "parent": "medication", "sort_order": 5},
    {"code": "prescription_flow", "name": "处方流转", "parent": "medication", "sort_order": 6},
    {"code": "yb_directory", "name": "医保目录", "parent": "medical_insurance", "sort_order": 3},
    {"code": "commercial_insurance", "name": "商业保险", "parent": "medical_insurance", "sort_order": 4},
    {"code": "drg_dip", "name": "支付方式改革", "parent": "medical_insurance", "sort_order": 5},
    {"code": "sign_in", "name": "签到取号", "parent": "appointment", "sort_order": 4},
    {"code": "health_education", "name": "健康教育", "parent": "health_management", "sort_order": 4},
    {"code": "vaccination", "name": "疫苗接种", "parent": "health_management", "sort_order": 5},
    {"code": "corporate_health", "name": "企业健康", "parent": "health_management", "sort_order": 6},
    # ---- 保险（商保直付/理赔/经纪，一级域）----
    {"code": "insurance", "name": "保险", "parent": None, "sort_order": 14},
    {"code": "insurance_policy", "name": "保单", "parent": "insurance", "sort_order": 1},
    {"code": "insurance_underwriting", "name": "核保", "parent": "insurance", "sort_order": 2},
    {"code": "insurance_claim", "name": "理赔", "parent": "insurance", "sort_order": 3},
    {"code": "insurance_direct_settle", "name": "商保直付", "parent": "insurance", "sort_order": 4},
    {"code": "insurance_broker", "name": "保险经纪", "parent": "insurance", "sort_order": 5},
    # ---- 会员权益（家庭会员/健康权益/微医通，一级域）----
    {"code": "membership", "name": "会员权益", "parent": None, "sort_order": 15},
    {"code": "family_member", "name": "家庭会员", "parent": "membership", "sort_order": 1},
    {"code": "health_rights", "name": "健康权益", "parent": "membership", "sort_order": 2},
    {"code": "member_benefit", "name": "会员权益", "parent": "membership", "sort_order": 3},
    {"code": "member_channel", "name": "会员渠道", "parent": "membership", "sort_order": 4},
    # ---- 医疗AI与数据（智能导诊/AI辅诊/医疗大模型/大数据，一级域）----
    {"code": "health_ai", "name": "医疗AI与数据", "parent": None, "sort_order": 16},
    {"code": "ai_triage", "name": "智能导诊", "parent": "health_ai", "sort_order": 1},
    {"code": "ai_diagnosis_assist", "name": "AI辅诊", "parent": "health_ai", "sort_order": 2},
    {"code": "medical_llm", "name": "医疗大模型", "parent": "health_ai", "sort_order": 3},
    {"code": "health_bigdata", "name": "健康大数据", "parent": "health_ai", "sort_order": 4},
    {"code": "ai_report", "name": "智能报告", "parent": "health_ai", "sort_order": 5},
    # ---- 临床科研（真实世界研究/临床试验/科研协作，一级域）----
    {"code": "clinical_research", "name": "临床科研", "parent": None, "sort_order": 17},
    {"code": "rws", "name": "真实世界研究", "parent": "clinical_research", "sort_order": 1},
    {"code": "clinical_trial", "name": "临床试验", "parent": "clinical_research", "sort_order": 2},
    {"code": "research_collab", "name": "科研协作", "parent": "clinical_research", "sort_order": 3},
    {"code": "research_data", "name": "科研数据", "parent": "clinical_research", "sort_order": 4},
    # ---- 医药供应链（药械集采/SPD/器械耗材/DTP，一级域）----
    {"code": "supply_chain", "name": "医药供应链", "parent": None, "sort_order": 18},
    {"code": "procurement_center", "name": "药械集采", "parent": "supply_chain", "sort_order": 1},
    {"code": "spd_logistics", "name": "SPD院内物流", "parent": "supply_chain", "sort_order": 2},
    {"code": "device_consumable", "name": "器械耗材", "parent": "supply_chain", "sort_order": 3},
    {"code": "dtp_pharmacy", "name": "DTP药房", "parent": "supply_chain", "sort_order": 4},
    # ---- 康复护理养老（居家护理/康复/长护险/养老，一级域）----
    {"code": "care_elderly", "name": "康复护理养老", "parent": None, "sort_order": 19},
    {"code": "home_care", "name": "居家护理", "parent": "care_elderly", "sort_order": 1},
    {"code": "rehab", "name": "康复", "parent": "care_elderly", "sort_order": 2},
    {"code": "long_term_care_ins", "name": "长期护理保险", "parent": "care_elderly", "sort_order": 3},
    {"code": "elderly_service", "name": "养老服务", "parent": "care_elderly", "sort_order": 4},
    # ---- 专科运营（肿瘤/妇儿/心理/口腔/中医等跨域专科专病，一级域）----
    {"code": "specialty_center", "name": "专科运营", "parent": None, "sort_order": 20},
    {"code": "oncology_center", "name": "肿瘤中心", "parent": "specialty_center", "sort_order": 1},
    {"code": "women_children_center", "name": "妇儿中心", "parent": "specialty_center", "sort_order": 2},
    {"code": "mental_health_center", "name": "心理精神", "parent": "specialty_center", "sort_order": 3},
    {"code": "stomatology_center", "name": "口腔中心", "parent": "specialty_center", "sort_order": 4},
    {"code": "tcm_center", "name": "中医中心", "parent": "specialty_center", "sort_order": 5},
    {"code": "chronic_specialty", "name": "慢病专病", "parent": "specialty_center", "sort_order": 6},
    # ---- 患者服务运营（教育/随访/满意度/投诉，一级域）----
    {"code": "patient_service", "name": "患者服务运营", "parent": None, "sort_order": 21},
    {"code": "patient_education", "name": "患者教育", "parent": "patient_service", "sort_order": 1},
    {"code": "followup_service", "name": "随访服务", "parent": "patient_service", "sort_order": 2},
    {"code": "satisfaction", "name": "满意度", "parent": "patient_service", "sort_order": 3},
    {"code": "complaint_service", "name": "投诉服务", "parent": "patient_service", "sort_order": 4},
    {"code": "patient_care", "name": "患者关怀", "parent": "patient_service", "sort_order": 5},
    # ---- 公共（横切基础参照：日期/地区/时间/组织/币种/客户，不归属业务线，一级域）----
    {"code": "common", "name": "公共", "parent": None, "sort_order": 22},
    {"code": "common_date", "name": "公共日期", "parent": "common", "sort_order": 1},
    {"code": "common_region", "name": "公共地区", "parent": "common", "sort_order": 2},
    {"code": "common_time", "name": "公共时间", "parent": "common", "sort_order": 3},
    {"code": "common_org", "name": "公共组织", "parent": "common", "sort_order": 4},
    {"code": "common_currency", "name": "公共币种", "parent": "common", "sort_order": 5},
    {"code": "common_customer", "name": "公共客户", "parent": "common", "sort_order": 6},
    {"code": "common_campaign", "name": "公共活动", "parent": "common", "sort_order": 7},
    {"code": "common_age_group", "name": "公共年龄段", "parent": "common", "sort_order": 8},
    {"code": "common_gender", "name": "公共性别", "parent": "common", "sort_order": 9},
    {"code": "common_education", "name": "公共学历", "parent": "common", "sort_order": 10},
    {"code": "common_ethnicity", "name": "公共民族", "parent": "common", "sort_order": 11},
]


# ---------------------------------------------------------------------------
# 术语（全业务线核心术语，95 条，全 PUBLISHED 直灌）
# ---------------------------------------------------------------------------
TERM_SEEDS: list[dict[str, Any]] = [
    # 在线问诊
    {"term_code": "text_consult", "name": "图文问诊", "definition": "患者通过图文消息向医生发起在线咨询的诊疗服务。", "domain": "online_consultation", "synonyms": ["图文咨询", "在线图文"], "boundary": "不含电话/视频问诊"},
    {"term_code": "phone_consult", "name": "电话问诊", "definition": "医患通过电话进行语音问诊的线上诊疗服务。", "domain": "online_consultation", "synonyms": ["电话咨询"], "boundary": None},
    {"term_code": "video_consult", "name": "视频问诊", "definition": "医患通过视频进行面对面问诊的线上诊疗服务。", "domain": "online_consultation", "synonyms": ["视频咨询"], "boundary": None},
    {"term_code": "consult_response_time", "name": "问诊响应时长", "definition": "患者发起问诊到医生首次回复的时间间隔，衡量线上服务响应效率。", "domain": "online_consultation", "synonyms": ["首响时长"], "boundary": "按自然时间计算"},
    # 预约挂号
    {"term_code": "appointment_register", "name": "预约挂号", "definition": "患者通过平台预约医院号源并到院就诊的就医流程。", "domain": "appointment", "synonyms": ["挂号", "在线挂号"], "boundary": None},
    {"term_code": "register_source", "name": "号源", "definition": "医院排班放出的可预约就诊名额，按科室/医生/时段组织。", "domain": "appointment", "synonyms": ["号池"], "boundary": None},
    {"term_code": "wait_time", "name": "候诊时长", "definition": "患者到院取号至医生接诊的等待时间，衡量门诊服务效率。", "domain": "appointment", "synonyms": ["候诊时间"], "boundary": None},
    # 药品 / 互联网医院
    {"term_code": "e_prescription", "name": "电子处方", "definition": "医生在线开具并经药师审核的数字化处方，是互联网医院药品流转的起点。", "domain": "internet_hospital", "synonyms": ["在线处方"], "boundary": None},
    {"term_code": "prescription_review", "name": "处方审核", "definition": "药师对处方用药合理性、安全性、规范性进行的审核。", "domain": "medication", "synonyms": ["审方"], "boundary": None},
    {"term_code": "drug_delivery", "name": "药品配送", "definition": "处方药品从药房配送到患者手中的物流服务。", "domain": "internet_hospital", "synonyms": ["送药上门", "配药"], "boundary": None},
    {"term_code": "online_followup", "name": "在线复诊", "definition": "患者在互联网医院对同一病种进行的线上复诊。", "domain": "internet_hospital", "synonyms": ["线上复诊"], "boundary": "仅限复诊，初诊须线下"},
    {"term_code": "drug_category", "name": "药品分类", "definition": "按药理作用/剂型对药品进行的标准化分类。", "domain": "medication", "synonyms": ["药品类别"], "boundary": None},
    # 医疗费用 / 医保
    {"term_code": "drug_fee", "name": "药品费", "definition": "患者就诊产生的药品相关费用。", "domain": "medical_fee", "synonyms": ["药费"], "boundary": None},
    {"term_code": "register_fee", "name": "挂号费", "definition": "患者挂号就诊时支付的医疗服务费用。", "domain": "medical_fee", "synonyms": ["诊疗挂号费"], "boundary": None},
    {"term_code": "insurance_direct_settle", "name": "商保直付", "definition": "商业保险按约定直接赔付医疗费用的结算方式。", "domain": "medical_fee", "synonyms": ["保险直付"], "boundary": None},
    {"term_code": "yb_settle", "name": "医保结算", "definition": "医保基金按政策对医疗费用进行的结算。", "domain": "medical_insurance", "synonyms": ["医保支付"], "boundary": None},
    {"term_code": "yb_directory", "name": "医保目录", "definition": "医保基金支付范围内的药品、诊疗项目、医疗服务设施目录。", "domain": "medical_insurance", "synonyms": ["医保三大目录"], "boundary": None},
    # 诊断 / 患者
    {"term_code": "icd_code", "name": "诊断编码", "definition": "疾病诊断的标准化编码（ICD-10），用于病种统计与医保结算。", "domain": "diagnosis", "synonyms": ["ICD编码"], "boundary": None},
    {"term_code": "chronic_disease", "name": "慢病", "definition": "需要长期治疗和健康管理的慢性疾病，如高血压、糖尿病。", "domain": "diagnosis", "synonyms": ["慢性病"], "boundary": None},
    {"term_code": "patient_profile", "name": "就诊人", "definition": "在平台上发起就医服务的用户主体。", "domain": "patient", "synonyms": ["患者"], "boundary": None},
    {"term_code": "health_record", "name": "健康档案", "definition": "患者历次就诊、体检、用药等健康信息的集合。", "domain": "patient", "synonyms": ["电子健康档案", "EHR"], "boundary": None},
    {"term_code": "followup_patient", "name": "复诊患者", "definition": "同一病种在互联网医院再次就诊的患者。", "domain": "patient", "synonyms": ["复诊人群"], "boundary": None},
    # 质控
    {"term_code": "medical_record_review", "name": "病历质控", "definition": "对病历书写规范性与完整性的质量控制与评价。", "domain": "quality", "synonyms": ["病案质控"], "boundary": None},
    {"term_code": "rational_drug_use", "name": "合理用药", "definition": "对处方用药安全性、有效性、经济性的审核评价。", "domain": "quality", "synonyms": ["合理用药评价"], "boundary": None},
    # 健共体（区域医疗协作，微医数字健共体核心）
    {"term_code": "two_way_referral", "name": "双向转诊", "definition": "上级医院与基层医疗机构之间依病情需要进行的患者上转与下转协作机制。", "domain": "health_community", "synonyms": ["分级转诊"], "boundary": "含上转/下转两个方向"},
    {"term_code": "upward_referral", "name": "上转", "definition": "基层医疗机构将超出其诊治能力的患者转往上级医院。", "domain": "health_community", "synonyms": ["向上转诊"], "boundary": None},
    {"term_code": "downward_referral", "name": "下转", "definition": "上级医院将病情稳定进入康复期的患者转往基层或康复机构。", "domain": "health_community", "synonyms": ["向下转诊"], "boundary": None},
    {"term_code": "medical_alliance", "name": "医联体", "definition": "城市医疗集团、专科联盟等医疗机构间以协同服务为目标的联合体。", "domain": "health_community", "synonyms": ["城市医疗集团", "专科联盟"], "boundary": None},
    {"term_code": "medical_community", "name": "医共体", "definition": "县域内县、乡、村三级医疗机构一体化管理的医疗共同体。", "domain": "health_community", "synonyms": ["县域医共体"], "boundary": None},
    {"term_code": "remote_consultation", "name": "远程会诊", "definition": "上级专家通过远程医疗平台为基层患者参与的会诊服务。", "domain": "health_community", "synonyms": ["远程医疗"], "boundary": None},
    {"term_code": "family_doctor_contract", "name": "家医签约", "definition": "居民与家庭医生服务团队签订基本医疗与健康管理服务协议。", "domain": "health_community", "synonyms": ["家庭医生签约"], "boundary": None},
    {"term_code": "check_share", "name": "检查检验互认", "definition": "区域内医疗机构间对符合条件的检查检验结果予以互认。", "domain": "health_community", "synonyms": ["结果互认"], "boundary": None},
    {"term_code": "chronic_followup", "name": "慢病随访", "definition": "对高血压、糖尿病等慢病患者进行的定期随访与健康管理。", "domain": "health_community", "synonyms": ["随访管理"], "boundary": None},
    # 医药/医保/挂号/健康管理补充
    {"term_code": "drug_price_comparison", "name": "药品比价", "definition": "对同种药品在不同药房或渠道的销售价格进行比较。", "domain": "medication", "synonyms": ["药价对比"], "boundary": None},
    {"term_code": "insurance_claim", "name": "商保理赔", "definition": "商业保险公司按保险条款对医疗费用进行的赔付。", "domain": "medical_insurance", "synonyms": ["保险理赔"], "boundary": None},
    {"term_code": "physical_exam", "name": "体检", "definition": "以健康评估为目的的系统性身体检查服务。", "domain": "health_management", "synonyms": ["健康体检"], "boundary": None},
    {"term_code": "vaccination", "name": "疫苗接种", "definition": "为预防传染病而进行的疫苗预防接种服务。", "domain": "health_management", "synonyms": ["预防接种"], "boundary": None},
    {"term_code": "health_education", "name": "健康教育", "definition": "面向居民开展的疾病预防与健康促进宣教服务。", "domain": "health_management", "synonyms": ["健康宣教"], "boundary": None},
    {"term_code": "register_sign_in", "name": "签到取号", "definition": "患者到院后凭预约信息签到并取得就诊序号。", "domain": "appointment", "synonyms": ["到院签到"], "boundary": None},
    # 医生管理（供给侧核心资产：资质/职称/执业/排班/绩效/团队）
    {"term_code": "licensed_doctor", "name": "执业医师", "definition": "依法取得执业医师资格并在医疗机构注册执业的医生。", "domain": "doctor_management", "synonyms": ["执业医生"], "boundary": None},
    {"term_code": "doctor_title", "name": "医师职称", "definition": "卫生专业技术人员的医师系列职称等级：住院医师（初级）、主治医师（中级）、副主任医师（副高）、主任医师（正高）。", "domain": "doctor_management", "synonyms": ["职称等级"], "boundary": None},
    {"term_code": "chief_physician", "name": "主任医师", "definition": "正高级医师职称，具有丰富临床经验与疑难病诊治能力。", "domain": "doctor_management", "synonyms": ["正高"], "boundary": None},
    {"term_code": "associate_chief_physician", "name": "副主任医师", "definition": "副高级医师职称。", "domain": "doctor_management", "synonyms": ["副高"], "boundary": None},
    {"term_code": "attending_physician", "name": "主治医师", "definition": "中级医师职称，可独立承担门诊与病房诊治工作。", "domain": "doctor_management", "synonyms": ["中级"], "boundary": None},
    {"term_code": "resident_physician", "name": "住院医师", "definition": "初级医师职称，在上级医师指导下从事临床工作。", "domain": "doctor_management", "synonyms": ["初级"], "boundary": None},
    {"term_code": "multi_site_practice", "name": "多点执业", "definition": "执业医师在注册主执业机构以外机构执业的制度安排。", "domain": "doctor_management", "synonyms": ["多点执业备案"], "boundary": None},
    {"term_code": "primary_practice_org", "name": "主执业点", "definition": "执业医师注册的主要执业医疗机构。", "domain": "doctor_management", "synonyms": ["主要执业机构"], "boundary": None},
    {"term_code": "doctor_schedule", "name": "医生排班", "definition": "医生出诊时间、科室、号源类型的安排计划。", "domain": "doctor_management", "synonyms": ["排班表"], "boundary": None},
    {"term_code": "expert_clinic", "name": "专家门诊", "definition": "由主任/副主任医师出诊的门诊类型。", "domain": "doctor_management", "synonyms": ["专家号"], "boundary": None},
    {"term_code": "attending_doctor", "name": "责任医生", "definition": "对患者诊疗全过程负责的医生。", "domain": "doctor_management", "synonyms": ["主管医生"], "boundary": None},
    {"term_code": "doctor_team", "name": "医生团队", "definition": "由多名医生组成的协作诊疗团队（如专家团队）。", "domain": "doctor_management", "synonyms": ["专家团队"], "boundary": None},
    {"term_code": "doctor_service_duration", "name": "医生服务时长", "definition": "医生单次问诊/接诊的服务时间，衡量医生服务效率。", "domain": "doctor_management", "synonyms": ["接诊时长"], "boundary": None},
    # 医药/医保/挂号/健共体补充
    {"term_code": "prescription_flow", "name": "处方流转", "definition": "医院HIS开具的处方经平台流转至药房配药的业务（处方外流）。", "domain": "medication", "synonyms": ["处方外流"], "boundary": None},
    {"term_code": "drug_traceability", "name": "药品追溯", "definition": "通过药品追溯码对药品生产、流通、使用全链路追踪。", "domain": "medication", "synonyms": ["药品追溯码"], "boundary": None},
    {"term_code": "drg", "name": "DRG付费", "definition": "按疾病诊断相关分组对住院费用打包付费的医保支付方式。", "domain": "medical_insurance", "synonyms": ["疾病诊断相关分组"], "boundary": "按病组打包付费"},
    {"term_code": "dip", "name": "DIP付费", "definition": "按病种分值付费的医保支付方式，依据病种分值计算费用。", "domain": "medical_insurance", "synonyms": ["按病种分值付费"], "boundary": None},
    {"term_code": "outpatient_pooling", "name": "门诊统筹", "definition": "将职工医保普通门诊费用纳入统筹基金支付的保障制度。", "domain": "medical_insurance", "synonyms": ["门诊共济"], "boundary": None},
    {"term_code": "cancel_register", "name": "退号", "definition": "患者取消已预约或已挂号的号源。", "domain": "appointment", "synonyms": ["取消挂号"], "boundary": None},
    {"term_code": "no_show", "name": "爽约", "definition": "患者预约成功但未按约到院就诊且未取消。", "domain": "appointment", "synonyms": ["失约"], "boundary": None},
    {"term_code": "corporate_health", "name": "企业健康管理", "definition": "面向企业员工提供的体检、健康档案与健康干预一体化服务。", "domain": "health_management", "synonyms": ["员工健康"], "boundary": None},
    {"term_code": "family_bed", "name": "家庭病床", "definition": "在患者家中设立病床，由基层医生定期上门巡诊的服务形式。", "domain": "health_community", "synonyms": ["家庭病床服务"], "boundary": None},
    # 保险（商保直付/经纪/理赔/惠民保）
    {"term_code": "insurance_broker", "name": "保险经纪", "definition": "为投保人与保险公司提供保险产品咨询、投保与理赔协助的居间服务。", "domain": "insurance", "synonyms": ["保险中介"], "boundary": None},
    {"term_code": "insurance_policy", "name": "保单", "definition": "投保人与保险人订立保险合同的书面凭证，载明保险责任与保额。", "domain": "insurance", "synonyms": ["保险合同"], "boundary": None},
    {"term_code": "insurance_underwriting", "name": "核保", "definition": "保险公司对投保申请进行风险评估并决定是否承保及承保条件的环节。", "domain": "insurance", "synonyms": ["承保审核"], "boundary": None},
    {"term_code": "huimin_insurance", "name": "惠民保", "definition": "地方政府指导、商业保险公司承办的普惠型补充医疗保险。", "domain": "insurance", "synonyms": ["城市定制型商业医疗险"], "boundary": None},
    {"term_code": "deductible", "name": "免赔额", "definition": "保险合同中约定的由被保险人自行承担、保险人不予赔付的金额。", "domain": "insurance", "synonyms": ["起付线"], "boundary": None},
    # 会员权益（家庭会员/健康权益/会员等级/微医通）
    {"term_code": "family_member", "name": "家庭会员", "definition": "以家庭为单位的会员产品，成员共享健康权益与家庭医生服务。", "domain": "membership", "synonyms": ["家庭健康会员"], "boundary": None},
    {"term_code": "health_rights", "name": "健康权益", "definition": "会员可享有的体检、问诊、药品、健康管理等权益组合。", "domain": "membership", "synonyms": ["权益包"], "boundary": None},
    {"term_code": "member_level", "name": "会员等级", "definition": "按消费与活跃度划分的会员成长等级（银卡/金卡/铂金等）。", "domain": "membership", "synonyms": ["会员成长值"], "boundary": None},
    {"term_code": "weiyitong", "name": "微医通", "definition": "微医面向家庭推出的智能健康终端，集成在线问诊、慢病管理等服务。", "domain": "membership", "synonyms": ["健康终端"], "boundary": None},
    # 医疗AI与数据（智能导诊/AI辅诊/医疗大模型/大数据）
    {"term_code": "ai_triage", "name": "智能导诊", "definition": "基于症状与病史由AI为患者推荐就诊科室与医生的导诊服务。", "domain": "health_ai", "synonyms": ["AI导诊"], "boundary": None},
    {"term_code": "ai_diagnosis_assist", "name": "AI辅助诊断", "definition": "AI基于医学影像/病历/检验数据为医生提供诊断建议的辅助工具。", "domain": "health_ai", "synonyms": ["AI辅诊"], "boundary": None},
    {"term_code": "medical_llm", "name": "医疗大模型", "definition": "面向医疗场景训练的行业大语言模型，支持病历生成、问答与决策支持。", "domain": "health_ai", "synonyms": ["医疗AI大模型"], "boundary": None},
    {"term_code": "health_bigdata", "name": "健康大数据", "definition": "汇聚诊疗、体检、用药、可穿戴等多源健康数据的分析资产。", "domain": "health_ai", "synonyms": ["医疗大数据"], "boundary": None},
    {"term_code": "ai_report", "name": "智能报告", "definition": "AI自动生成的影像/检验/体检报告，辅助医生审阅与质控。", "domain": "health_ai", "synonyms": ["AI报告"], "boundary": None},
    # 临床科研（真实世界研究/临床试验/科研协作）
    {"term_code": "rws", "name": "真实世界研究", "definition": "在真实诊疗环境中利用常规诊疗数据开展的临床研究。", "domain": "clinical_research", "synonyms": ["RWS", "真实世界证据"], "boundary": None},
    {"term_code": "clinical_trial", "name": "临床试验", "definition": "在人体中验证药物/器械安全性有效性的系统性研究。", "domain": "clinical_research", "synonyms": ["GCP试验"], "boundary": None},
    {"term_code": "research_collab", "name": "科研协作", "definition": "医院、药企、科研机构间围绕科研课题的协同合作。", "domain": "clinical_research", "synonyms": ["科研合作"], "boundary": None},
    {"term_code": "informed_consent", "name": "知情同意", "definition": "受试者在充分了解研究内容与风险后自愿签署同意参加研究。", "domain": "clinical_research", "synonyms": ["知情同意书"], "boundary": None},
    {"term_code": "research_data", "name": "科研数据", "definition": "用于临床科研的脱敏诊疗数据与随访数据集合。", "domain": "clinical_research", "synonyms": ["科研数据集"], "boundary": None},
    # 医药供应链（药械集采/SPD/器械耗材/DTP）
    {"term_code": "drug_centralized_procurement", "name": "药械集采", "definition": "以量换价的药品/耗材集中带量采购模式。", "domain": "supply_chain", "synonyms": ["带量采购"], "boundary": None},
    {"term_code": "spd", "name": "SPD院内物流", "definition": "医院药品耗材的院内供应链管理（采购-库存-配送-消耗一体化）。", "domain": "supply_chain", "synonyms": ["院内物流"], "boundary": None},
    {"term_code": "medical_device", "name": "器械耗材", "definition": "医疗机构使用的医疗器械与高值/低值耗材。", "domain": "supply_chain", "synonyms": ["医用耗材"], "boundary": None},
    {"term_code": "dtp_pharmacy", "name": "DTP药房", "definition": "直接面向患者提供高值/新特药与专业药事服务的院外药房。", "domain": "supply_chain", "synonyms": ["院外药房"], "boundary": None},
    # 康复护理养老（居家护理/康复/长护险/养老）
    {"term_code": "home_care", "name": "居家护理", "definition": "由护士/护理员上门为居家人群提供的基础护理与专项护理服务。", "domain": "care_elderly", "synonyms": ["上门护理"], "boundary": None},
    {"term_code": "rehabilitation", "name": "康复治疗", "definition": "针对功能障碍者开展的物理/作业/言语等康复训练与治疗。", "domain": "care_elderly", "synonyms": ["康复"], "boundary": None},
    {"term_code": "long_term_care_insurance", "name": "长期护理保险", "definition": "为失能人群长期护理需求提供保障的社会保险/商业保险制度。", "domain": "care_elderly", "synonyms": ["长护险"], "boundary": None},
    {"term_code": "elderly_service", "name": "养老服务", "definition": "面向老年人的生活照料、健康管理、精神慰藉等综合服务。", "domain": "care_elderly", "synonyms": ["老年服务"], "boundary": None},
    # 专科运营（肿瘤/妇儿/心理/口腔/中医等跨域专科专病）
    {"term_code": "oncology_center", "name": "肿瘤中心", "definition": "聚焦肿瘤预防、筛查、诊疗与康复的一体化专科中心。", "domain": "specialty_center", "synonyms": ["肿瘤专科"], "boundary": None},
    {"term_code": "women_children_center", "name": "妇儿中心", "definition": "覆盖妇女与儿童保健、诊疗的跨科室专科中心。", "domain": "specialty_center", "synonyms": ["妇儿专科"], "boundary": None},
    {"term_code": "mental_health_clinic", "name": "心理门诊", "definition": "提供心理评估、心理咨询与精神心理疾病诊疗的门诊服务。", "domain": "specialty_center", "synonyms": ["精神心理"], "boundary": None},
    {"term_code": "chronic_specialty", "name": "专病管理", "definition": "围绕单病种（如高血压/糖尿病/哮喘）的全流程规范化管理。", "domain": "specialty_center", "synonyms": ["单病种管理"], "boundary": None},
    {"term_code": "tcm_center", "name": "中医中心", "definition": "以中医药诊疗与治未病为特色的专科中心。", "domain": "specialty_center", "synonyms": ["中医专科"], "boundary": None},
    # 患者服务运营（教育/随访/满意度/投诉）
    {"term_code": "patient_followup", "name": "患者随访", "definition": "对出院/术后/慢病患者进行的定期跟踪回访与康复指导。", "domain": "patient_service", "synonyms": ["随访管理"], "boundary": None},
    {"term_code": "patient_satisfaction", "name": "患者满意度", "definition": "患者对医疗服务过程与结果的主观评价水平。", "domain": "patient_service", "synonyms": ["就医满意度"], "boundary": None},
    {"term_code": "nps", "name": "净推荐值", "definition": "衡量用户推荐意愿的指标（NPS），反映服务口碑。", "domain": "patient_service", "synonyms": ["NPS"], "boundary": None},
    {"term_code": "patient_education", "name": "患者教育", "definition": "面向患者的疾病预防、用药与康复知识科普宣教。", "domain": "patient_service", "synonyms": ["健康科普"], "boundary": None},
    {"term_code": "complaint_handling", "name": "投诉处理", "definition": "对患者投诉的受理、调查、反馈与改进闭环。", "domain": "patient_service", "synonyms": ["客诉处理"], "boundary": None},
    # 公共（横切基础参照：日期/地区）
    {"term_code": "date_dimension", "name": "日期维度", "definition": "以日期为主键的公共维度，提供年/季/月/周/自然日/工作日/节假日等时间属性，供各业务指标按时间切片与钻取分析。", "domain": "common", "synonyms": ["时间维度", "dim_date"], "boundary": "与指标口径的时间粒度（granularity）不同：粒度描述统计单位，日期维度描述分析切片"},
    {"term_code": "region_dimension", "name": "地区维度", "definition": "按行政区划（省/市/区县）组织的公共维度，用于按地域分析业务量（问诊/挂号/药品/健共体协作等）。", "domain": "common", "synonyms": ["地理维度", "dim_region"], "boundary": "不承载具体机构与地址，机构归属由医疗机构维度表达"},
    {"term_code": "administrative_division", "name": "行政区划", "definition": "国家为分级管理划分的省、市、区县等行政区域体系。", "domain": "common", "synonyms": ["行政区"], "boundary": None},
    {"term_code": "natural_day", "name": "自然日", "definition": "以自然 24 小时为单位的日历日，是日期维度按日分析的最小切片。", "domain": "common", "synonyms": ["日历日"], "boundary": None},
    {"term_code": "workday", "name": "工作日", "definition": "一周中安排正常工作的日期（通常周一至周五），区分自然日以支持排班/就诊/配送等运营分析。", "domain": "common", "synonyms": ["上班日"], "boundary": "节假日调休由日期维度属性承载，不单独建维度"},
    {"term_code": "time_dimension", "name": "日内时间维度", "definition": "以日内时刻（时/分）为分析切片的公共维度，提供时段/班次/高峰属性，用于分时就诊、预约高峰、夜间急诊等日内分析；与日期维度（跨天）互补，日+时组合即完整时间戳。", "domain": "common", "synonyms": ["时间维度", "日内时段"], "boundary": "与日期维度不同：日期维度描述跨天日期切片，日内时间维度描述一天内的时刻切片"},
    {"term_code": "org_dimension", "name": "组织维度", "definition": "按企业管理层级（集团/分公司/部门/团队）组织的公共维度，用于人效、成本、预算归口等内部经营分析；区别于临床科室（医疗业务视角）。", "domain": "common", "synonyms": ["行政组织", "dim_org"], "boundary": "不承载临床科室/医生，科室由科室维度表达"},
    {"term_code": "currency_dimension", "name": "币种维度", "definition": "以币种为成员的公共维度，提供币种代码/符号/汇率基准等属性，用于金额类指标的多币种结算（对外结算/保险理赔/跨境合作）。", "domain": "common", "synonyms": ["币种", "dim_currency"], "boundary": "不承载汇率明细，汇率由财务系统实时提供"},
    {"term_code": "customer_dimension", "name": "客户维度", "definition": "以商业合作主体（企业健管/保险/医院/政府/渠道）为成员的公共维度，区别于患者（临床个体）；用于商业客户经营分析（合同、营收、合作模式）。", "domain": "common", "synonyms": ["商业客户", "B端客户"], "boundary": "不承载患者个体，患者由患者维度表达"},
    {"term_code": "campaign_dimension", "name": "活动维度", "definition": "以营销/运营活动为成员的公共维度，用于活动→业务量（问诊/挂号/下单/获客）的归因分析，衡量活动 ROI。", "domain": "common", "synonyms": ["营销活动", "活动"], "boundary": "不承载活动预算/执行明细，活动主数据由活动系统承载"},
    {"term_code": "marketing_campaign", "name": "营销活动", "definition": "平台为获客/促活/转化组织的运营活动（义诊/健康日/保险促销/拉新/会员活动等），可关联指标做活动效果分析。", "domain": "common", "synonyms": ["运营活动", "促销活动"], "boundary": "区分日常运营动作：活动是阶段性、有明确目标与时间窗的运营项目"},
    {"term_code": "free_clinic", "name": "义诊", "definition": "平台联合医院/医生提供的免费问诊或检查活动，常用于获客与品牌建设。", "domain": "common", "synonyms": ["公益义诊"], "boundary": None},
    {"term_code": "health_day", "name": "健康日", "definition": "围绕健康主题（如世界高血压日、全国爱牙日）发起的科普/筛查/问诊活动。", "domain": "common", "synonyms": ["主题健康日"], "boundary": None},
    # ---- 第二轮公共维度（属性类横切维度：年龄段/性别/学历/民族）----
    {"term_code": "age_group", "name": "年龄段", "definition": "按年龄区间划分的分析分桶（婴幼儿/学龄前/儿童/青少年/青年/中年/老年/高龄），供任何业务按年龄切片分析（门诊量年龄分布、慢病年龄特征等）。", "domain": "common", "synonyms": ["年龄分桶", "年龄组"], "boundary": "患者维度含年龄段属性，公共年龄段维度提供统一分桶口径供各业务引用"},
    {"term_code": "gender_dimension", "name": "性别维度", "definition": "以性别为成员的公共维度（男/女/未知），供各业务按性别切片分析（问诊性别比、病种性别特征等）。", "domain": "common", "synonyms": ["性别"], "boundary": None},
    {"term_code": "education", "name": "学历", "definition": "个人最高学历（小学及以下至博士），用于人群画像/健康教育触达/患者服务分析。", "domain": "common", "synonyms": ["教育程度", "学历层次"], "boundary": None},
    {"term_code": "ethnicity", "name": "民族", "definition": "个人民族归属（汉族/壮族/回族等），用于多民族地区医疗健康服务分析。", "domain": "common", "synonyms": ["民族"], "boundary": None},
    # ---- 第二轮业务维度术语 ----
    {"term_code": "inpatient_ward", "name": "病区", "definition": "住院部按诊疗需要划分的护理单元（内科/外科/ICU/康复病区等），用于住院运营分析（床位使用、平均住院日、护理负荷）。", "domain": "outpatient", "synonyms": ["住院病区", "护理单元"], "boundary": "区别于临床科室：科室是医生执业组织，病区是护理/住院管理单元"},
    {"term_code": "surgery_level", "name": "手术等级", "definition": "按手术难度、风险与资源消耗划分的手术级别（一级~四级），用于手术运营与绩效分析。", "domain": "outpatient", "synonyms": ["手术分级"], "boundary": "与切口分类（一类~四类）不同：等级按手术本身，切口按污染风险"},
    {"term_code": "exam_item", "name": "检查检验项目", "definition": "医技科室开展的检查（影像/超声/内镜）与检验（血/尿/生化）项目，用于医技工作量与开单分析。", "domain": "outpatient", "synonyms": ["医技项目"], "boundary": "区别于收费项目：检查检验项目是医疗行为，收费项目是费用归集"},
    {"term_code": "insurance_catalog", "name": "医保目录", "definition": "基本医疗保险药品/诊疗项目/医疗服务设施目录（甲类/乙类/丙类/自费），决定报销比例。", "domain": "medical_insurance", "synonyms": ["医保三目录"], "boundary": "目录随国家/省政策动态调整，维度记录分类口径而非逐条目录"},
    {"term_code": "drg_group", "name": "DRG分组", "definition": "按疾病诊断相关分组（DRG/DIP）的疾病系统分组（循环/呼吸/消化系统等），用于医保支付改革与病组费用分析。", "domain": "medical_insurance", "synonyms": ["疾病诊断相关分组", "病组"], "boundary": "与病种（ICD-10 章）不同：DRG 是医保支付分组，含资源消耗与住院特征"},
    {"term_code": "drug_route", "name": "给药途径", "definition": "药品进入人体的途径（口服/静脉/外用/吸入等），用于合理用药与用药安全分析。", "domain": "medication", "synonyms": ["用药途径", "给药方式"], "boundary": None},
    {"term_code": "drug_delivery_method", "name": "药品配送方式", "definition": "互联网医院处方药品送达患者的方式（门店自提/快递/药房配送/冷链等），用于配送履约与服务体验分析。", "domain": "internet_hospital", "synonyms": ["送药方式"], "boundary": "药品配送（服务）与配送方式（分类维度）不同：前者是业务，后者是切片"},
    {"term_code": "online_followup_type", "name": "复诊类型", "definition": "在线复诊的服务形式（图文/视频/电话/处方续方复诊），用于复诊业务结构与体验分析。", "domain": "internet_hospital", "synonyms": ["线上复诊形式"], "boundary": "复诊类型是服务形式维度，复诊业务是行为"},
    {"term_code": "chronic_disease_type", "name": "慢病类型", "definition": "慢性病病种分类（高血压/糖尿病/冠心病/慢阻肺等），用于慢病管理人群分层、随访与干预效果分析。", "domain": "health_management", "synonyms": ["慢病病种"], "boundary": "区别于慢病管理（业务）：慢病类型是被管理的病种分类维度"},
    {"term_code": "remote_consult_type", "name": "远程会诊类型", "definition": "远程会诊的服务形式（同步实时/异步资料/疑难病例/急会诊），用于健共体远程协作分析。", "domain": "health_community", "synonyms": ["会诊形式"], "boundary": None},
    {"term_code": "vaccine", "name": "疫苗", "definition": "预防接种疫苗品种（一类免疫规划/二类自愿接种），用于疫苗接种覆盖与预防保健分析。", "domain": "health_management", "synonyms": ["疫苗品种"], "boundary": None},
    {"term_code": "checkup_package", "name": "体检套餐", "definition": "健康体检的组合产品（入职/基础/全面/专项/老年套餐等），用于体检业务量与套餐转化分析。", "domain": "health_management", "synonyms": ["体检组合"], "boundary": "体检套餐是组合产品，体检项目是单项检查内容"},
]


# ---------------------------------------------------------------------------
# 维度 + 成员（全业务线，29 个维度；SCD0/SCD1/SCD2 三型）
# ---------------------------------------------------------------------------
def _slug(name: str) -> str:
    """中文名 → 拼音风格 slug（科室/病种成员编码）。"""
    table = {
        "内科": "neike", "外科": "waike", "儿科": "erke", "妇产科": "fuchanke",
        "急诊科": "jizhenke", "骨科": "guke", "眼科": "yanke", "耳鼻喉科": "erbihouke",
        "口腔科": "kouqiangke", "皮肤科": "pifuke", "中医科": "zhongyike",
        "肿瘤科": "zhongliuke", "神经内科": "shenjingneike", "心血管内科": "xinxueguanneike",
        "呼吸内科": "huxineike", "消化内科": "xiaohuaneike", "泌尿外科": "miniaowaike",
        "康复科": "kangfuke", "影像科": "yingxiangke", "检验科": "jianyanke",
        "呼吸系统疾病": "respiratory", "消化系统疾病": "digestive", "循环系统疾病": "circulatory",
        "神经系统疾病": "nervous", "内分泌代谢疾病": "endocrine", "肿瘤": "tumor",
        "泌尿生殖系统疾病": "urogenital", "肌肉骨骼系统疾病": "musculoskeletal",
        "传染病": "infectious", "损伤与中毒": "injury_poisoning",
    }
    return table.get(name, name)


def _dept_members() -> list[dict[str, Any]]:
    names = [
        "内科", "外科", "儿科", "妇产科", "急诊科", "骨科", "眼科", "耳鼻喉科",
        "口腔科", "皮肤科", "中医科", "肿瘤科", "神经内科", "心血管内科",
        "呼吸内科", "消化内科", "泌尿外科", "康复科", "影像科", "检验科",
    ]
    return [
        {"code": _slug(n), "name": n, "attributes": {"dept_type": "clinical"}}
        for n in names
    ]


def _doctor_members() -> list[dict[str, Any]]:
    """医生维度成员：覆盖主要科室的代表医生，attributes 含科室/职称/执业类型/专家标识（SCD2 跟踪职称变化）。"""
    doctors = [
        # (姓名, 科室, 职称, 执业类型 full=全职/multi=多点, 是否专家)
        ("张伟", "neike", "主任医师", "full", True),
        ("李娜", "neike", "副主任医师", "full", True),
        ("王强", "waike", "主任医师", "full", True),
        ("赵敏", "fuchanke", "副主任医师", "full", True),
        ("刘洋", "erke", "主治医师", "full", False),
        ("陈静", "zhongyike", "副主任医师", "full", True),
        ("杨光", "xinxueguanneike", "主治医师", "full", False),
        ("周婷", "huxineike", "住院医师", "full", False),
        ("孙磊", "guke", "副主任医师", "full", True),
        ("吴芳", "yanke", "主治医师", "full", False),
        ("郑华", "shenjingneike", "主任医师", "full", True),
        ("钱进", "pifuke", "主治医师", "multi", False),
        ("何静", "kouqiangke", "副主任医师", "full", True),
        ("罗强", "yingxiangke", "主治医师", "full", False),
        ("梁敏", "jianyanke", "副主任医师", "full", True),
        ("谢涛", "miniaowaike", "主治医师", "full", False),
    ]
    return [
        {
            "code": f"doc_{i + 1:02d}",
            "name": name,
            "attributes": {
                "dept_code": dept,
                "title": title,
                "practice_type": "全职" if practice == "full" else "多点",
                "doctor_level": "专家" if is_expert else "普通",
                "is_expert": is_expert,
            },
        }
        for i, (name, dept, title, practice, is_expert) in enumerate(doctors)
    ]


def _patient_members() -> list[dict[str, Any]]:
    """患者维度成员：年龄段 × 性别组合（SCD2 跟踪年龄段变化）。"""
    groups = [
        ("0-17", "男"), ("0-17", "女"),
        ("18-44", "男"), ("18-44", "女"),
        ("45-59", "男"), ("45-59", "女"),
        ("60+", "男"), ("60+", "女"),
    ]
    return [
        {
            "code": f"p_{age.replace('+', '_plus').replace('-', '_')}_{'m' if sex == '男' else 'f'}",
            "name": f"{age}岁·{sex}",
            "attributes": {"age_band": age, "gender": sex},
        }
        for age, sex in groups
    ]


def _disease_members() -> list[dict[str, Any]]:
    names = [
        "呼吸系统疾病", "消化系统疾病", "循环系统疾病", "神经系统疾病",
        "内分泌代谢疾病", "肿瘤", "泌尿生殖系统疾病", "肌肉骨骼系统疾病",
        "传染病", "损伤与中毒",
    ]
    return [{"code": _slug(n), "name": n, "attributes": {"icd_chapter": f"{i + 1:02d}"}} for i, n in enumerate(names)]


# 月份 → (季节, 财季, 主要法定节假日, 主要节气)
_MONTH_META: dict[int, tuple[str, int, str, str]] = {
    1: ("冬季", 1, "元旦、春节（农历正月初一）", "小寒、大寒"),
    2: ("冬季", 1, "春节（农历正月初一）", "立春、雨水"),
    3: ("春季", 1, "妇女节（部分）", "惊蛰、春分"),
    4: ("春季", 2, "清明节", "清明、谷雨"),
    5: ("春季", 2, "劳动节", "立夏、小满"),
    6: ("夏季", 2, "端午节", "芒种、夏至"),
    7: ("夏季", 3, "建党节（部分）", "小暑、大暑"),
    8: ("夏季", 3, "建军节（部分）", "立秋、处暑"),
    9: ("秋季", 3, "中秋节", "白露、秋分"),
    10: ("秋季", 4, "国庆节", "寒露、霜降"),
    11: ("秋季", 4, "无", "立冬、小雪"),
    12: ("冬季", 4, "无", "大雪、冬至"),
}


def _date_members() -> list[dict[str, Any]]:
    """日期维度成员：年 → 季 → 月 三级层级节点（2024-2026）。

    设计取舍：维度管理是参照/口径层，只灌层级节点不灌每日明细
    （每日明细是海量主数据 dim_date 物理表承载，365 行/年）。
    按日分析由物理 date_id 字段承载，维度描述中已注明。

    属性说明（层级节点提供可计算属性，日级属性由 dim_date 物理表承载）：
    - 年节点：财年（=自然年）、是否闰年
    - 季节点：财季（=自然季）、季节
    - 月节点：季节、财季、当月主要法定节假日、当月主要节气
    - 日级属性（工作日/周末、节假日调休、自然周/ISO 周、节气精确日、农历日）
      由 dim_date 物理表按日承载，不在维度层级节点重复
    """
    members: list[dict[str, Any]] = []
    quarters = ["Q1", "Q2", "Q3", "Q4"]
    for year in (2024, 2025, 2026):
        y = str(year)
        members.append(
            {
                "code": f"y{y}",
                "name": f"{y}年",
                "attributes": {"level": "year", "fiscal_year": year, "is_leap": year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)},
            }
        )
        for q_idx, q in enumerate(quarters, start=1):
            q_code = f"{y}q{q_idx}"
            season = {1: "冬季", 2: "春季", 3: "夏季", 4: "秋季"}[q_idx]
            members.append(
                {
                    "code": q_code,
                    "name": f"{y}年第{q_idx}季度",
                    "parent_code": f"y{y}",
                    "attributes": {"level": "quarter", "quarter": q_idx, "fiscal_quarter": q_idx, "season": season},
                }
            )
            for m in range((q_idx - 1) * 3 + 1, q_idx * 3 + 1):
                season, fq, festivals, solar_terms = _MONTH_META[m]
                members.append(
                    {
                        "code": f"{y}{m:02d}",
                        "name": f"{y}年{m}月",
                        "parent_code": q_code,
                        "attributes": {
                            "level": "month",
                            "month": m,
                            "season": season,
                            "fiscal_quarter": fq,
                            "festivals": festivals,
                            "solar_terms": solar_terms,
                        },
                    }
                )
    return members


_REGION_SEEDS: dict[str, dict[str, list[str]]] = {
    # 省/直辖市 → {市/区 → 区县示例}
    "北京市": {"北京市": ["朝阳区", "海淀区", "东城区"]},
    "天津市": {"天津市": ["和平区", "滨海新区", "河西区"]},
    "上海市": {"上海市": ["浦东新区", "徐汇区", "静安区"]},
    "浙江省": {"杭州市": ["西湖区", "余杭区", "滨江区"], "宁波市": ["海曙区", "鄞州区"], "温州市": ["鹿城区", "瓯海区"], "嘉兴市": ["南湖区", "秀洲区"]},
    "山东省": {"济南市": ["历下区", "市中区"], "青岛市": ["市南区", "崂山区"]},
    "广东省": {"广州市": ["天河区", "越秀区"], "深圳市": ["南山区", "福田区"]},
    "江苏省": {"南京市": ["鼓楼区", "玄武区"], "苏州市": ["姑苏区", "工业园区"]},
    "福建省": {"福州市": ["鼓楼区", "台江区"], "厦门市": ["思明区", "湖里区"]},
    "四川省": {"成都市": ["武侯区", "锦江区"]},
    "湖北省": {"武汉市": ["江汉区", "武昌区"]},
    "湖南省": {"长沙市": ["岳麓区", "天心区"]},
    "安徽省": {"合肥市": ["蜀山区", "庐阳区"]},
    "河北省": {"石家庄市": ["长安区", "桥西区"]},
    "河南省": {"郑州市": ["金水区", "中原区"]},
    "陕西省": {"西安市": ["雁塔区", "碑林区"]},
    "重庆市": {"重庆市": ["渝中区", "江北区"]},
}


# 省/直辖市 → 大区
_PROVINCE_MACRO_REGION: dict[str, str] = {
    "北京市": "华北", "天津市": "华北", "河北省": "华北",
    "上海市": "华东", "江苏省": "华东", "浙江省": "华东", "安徽省": "华东", "福建省": "华东", "山东省": "华东",
    "广东省": "华南",
    "四川省": "西南", "重庆市": "西南",
    "湖北省": "华中", "湖南省": "华中", "河南省": "华中",
    "陕西省": "西北",
}

# 健共体试点省（微医数字健共体核心落地区域）
_HC_DEMO_PROVINCES = {"浙江省", "天津市", "山东省"}
# 互联网医院落地省（已取得互联网医院牌照并实际运营的省份）
_IOH_PROVINCES = {"浙江省", "天津市", "山东省", "北京市", "上海市", "广东省", "江苏省"}

# 城市 → 城市等级（一线/新一线/二线；未列出的城市默认二线）
_CITY_TIER: dict[str, str] = {
    "北京市": "一线", "上海市": "一线", "广州市": "一线", "深圳市": "一线",
    "杭州市": "新一线", "南京市": "新一线", "苏州市": "新一线", "天津市": "新一线",
    "成都市": "新一线", "武汉市": "新一线", "长沙市": "新一线", "郑州市": "新一线",
    "西安市": "新一线", "重庆市": "新一线", "青岛市": "新一线",
    "宁波市": "二线", "温州市": "二线", "嘉兴市": "二线", "济南市": "二线",
    "福州市": "二线", "厦门市": "二线", "合肥市": "二线", "石家庄市": "二线",
}


def _region_members() -> list[dict[str, Any]]:
    """地区维度成员：省（直辖市）→ 市 → 区县 三级层级，覆盖微医核心业务区域。

    设计约束：
    - 直辖市省=市（如北京市），只生成一个市级根节点，区县挂其下，避免同名撞码；
    - 区县编码带「城市前缀」（如 hangzhou_xihu），避免不同城市同名区县（南京/福州鼓楼区）撞唯一索引；
    - 属性：大区/城市等级/是否健共体试点省/是否互联网医院落地省（省、市、区县逐级继承）。
    """
    members: list[dict[str, Any]] = []
    for prov, cities in _REGION_SEEDS.items():
        p_code = _region_code(prov)
        macro_region = _PROVINCE_MACRO_REGION.get(prov, "其他")
        is_hc = prov in _HC_DEMO_PROVINCES
        is_ioh = prov in _IOH_PROVINCES
        # 直辖市：prov 自身即城市，根节点 + 区县
        if prov in cities:
            members.append(
                {
                    "code": p_code,
                    "name": prov,
                    "attributes": {
                        "level": "municipality",
                        "macro_region": macro_region,
                        "city_tier": _CITY_TIER.get(prov, "二线"),
                        "is_hc_demo": is_hc,
                        "is_ioh_province": is_ioh,
                    },
                }
            )
            for dist in cities[prov]:
                members.append(
                    {
                        "code": f"{p_code}_{_region_code(dist)}",
                        "name": dist,
                        "parent_code": p_code,
                        "attributes": {
                            "level": "district",
                            "macro_region": macro_region,
                            "city_tier": _CITY_TIER.get(prov, "二线"),
                            "is_hc_demo": is_hc,
                            "is_ioh_province": is_ioh,
                        },
                    }
                )
            continue
        members.append(
            {
                "code": p_code,
                "name": prov,
                "attributes": {
                    "level": "province",
                    "macro_region": macro_region,
                    "is_hc_demo": is_hc,
                    "is_ioh_province": is_ioh,
                },
            }
        )
        for city, districts in cities.items():
            c_code = _region_code(city)
            city_tier = _CITY_TIER.get(city, "二线")
            members.append(
                {
                    "code": c_code,
                    "name": city,
                    "parent_code": p_code,
                    "attributes": {
                        "level": "city",
                        "macro_region": macro_region,
                        "city_tier": city_tier,
                        "is_hc_demo": is_hc,
                        "is_ioh_province": is_ioh,
                    },
                }
            )
            for dist in districts:
                members.append(
                    {
                        "code": f"{c_code}_{_region_code(dist)}",
                        "name": dist,
                        "parent_code": c_code,
                        "attributes": {
                            "level": "district",
                            "macro_region": macro_region,
                            "city_tier": city_tier,
                            "is_hc_demo": is_hc,
                            "is_ioh_province": is_ioh,
                        },
                    }
                )
    return members


def _region_code(name: str) -> str:
    """地区名 → 拼音风格编码（与 _slug 共用映射，行政区固定清单）。"""
    table = {
        "北京市": "beijing", "天津市": "tianjin", "上海市": "shanghai", "重庆市": "chongqing",
        "浙江省": "zhejiang", "山东省": "shandong", "广东省": "guangdong", "江苏省": "jiangsu",
        "福建省": "fujian", "四川省": "sichuan", "湖北省": "hubei", "湖南省": "hunan",
        "安徽省": "anhui", "河北省": "hebei", "河南省": "henan", "陕西省": "shaanxi",
        "杭州市": "hangzhou", "宁波市": "ningbo", "温州市": "wenzhou", "嘉兴市": "jiaxing",
        "济南市": "jinan", "青岛市": "qingdao", "广州市": "guangzhou", "深圳市": "shenzhen",
        "南京市": "nanjing", "苏州市": "suzhou", "福州市": "fuzhou", "厦门市": "xiamen",
        "成都市": "chengdu", "武汉市": "wuhan", "长沙市": "changsha", "合肥市": "hefei",
        "石家庄市": "shijiazhuang", "郑州市": "zhengzhou", "西安市": "xian",
        "朝阳区": "chaoyang", "海淀区": "haidian", "东城区": "dongcheng",
        "和平区": "heping", "滨海新区": "binhai", "河西区": "hexi",
        "浦东新区": "pudong", "徐汇区": "xuhui", "静安区": "jingan",
        "西湖区": "xihu", "余杭区": "yuhang", "滨江区": "binjiang",
        "海曙区": "haishu", "鄞州区": "yinzhou", "鹿城区": "lucheng", "瓯海区": "ouhai",
        "南湖区": "nanhu", "秀洲区": "xiuzhou",
        "历下区": "lixia", "市中区": "shizhong", "市南区": "shinan", "崂山区": "laoshan",
        "天河区": "tianhe", "越秀区": "yuexiu", "南山区": "nanshan", "福田区": "futian",
        "鼓楼区": "gulou", "玄武区": "xuanwu", "姑苏区": "gusu", "工业园区": "gongyeyuanqu",
        "台江区": "taijiang", "思明区": "siming", "湖里区": "huli",
        "武侯区": "wuhou", "锦江区": "jinjiang", "江汉区": "jianghan", "武昌区": "wuchang",
        "岳麓区": "yuelu", "天心区": "tianxin", "蜀山区": "shushan", "庐阳区": "luyang",
        "长安区": "changan", "桥西区": "qiaoxi", "金水区": "jinshui", "中原区": "zhongyuan",
        "雁塔区": "yanta", "碑林区": "beilin", "渝中区": "yuzhong", "江北区": "jiangbei",
    }
    return table.get(name, name)


# ---------------------------------------------------------------------------
# 公共时间/组织/币种/客户 维度成员
# ---------------------------------------------------------------------------
_TIME_PERIODS: list[tuple[str, str, int, int, str, bool]] = [
    # (code, 名称, 起始小时, 结束小时, 班次, 是否就诊高峰)
    ("lingchen", "凌晨", 0, 5, "夜班", False),
    ("qingchen", "清晨", 6, 7, "早班", False),
    ("shangwu", "上午", 8, 11, "早班", True),
    ("zhongwu", "中午", 12, 13, "白班", False),
    ("xiawu", "下午", 14, 17, "白班", True),
    ("bangwan", "傍晚", 18, 19, "中班", False),
    ("yejian", "夜间", 20, 22, "中班", False),
    ("shenye", "深夜", 23, 23, "夜班", False),
]


def _time_members() -> list[dict[str, Any]]:
    """时间维度成员：时段 → 小时 两级层级（8 时段 + 24 小时）。

    设计取舍：分钟/秒为物理时间戳承载（dim_time 明细 1440 行/天），
    维度管理只灌时段/小时层级节点，属性含班次/高峰标识。
    """
    members: list[dict[str, Any]] = []
    for p_code, p_name, start, end, shift, peak in _TIME_PERIODS:
        members.append(
            {
                "code": p_code,
                "name": p_name,
                "attributes": {"level": "period", "start_hour": start, "end_hour": end, "shift": shift, "is_peak": peak},
            }
        )
        for h in range(start, end + 1):
            members.append(
                {
                    "code": f"hour_{h:02d}",
                    "name": f"{h:02d}时",
                    "parent_code": p_code,
                    "attributes": {"level": "hour", "hour": h, "shift": shift, "is_peak": peak},
                }
            )
    return members


_ORG_SEEDS: dict[str, dict[str, Any]] = {
    # code -> 组织节点（level=group 集团 / branch 分公司）
    "weiyi_group": {"name": "微医集团", "level": "group", "region": "杭州"},
    "hangzhou_hq": {"name": "杭州总部", "level": "branch", "parent": "weiyi_group", "region": "杭州"},
    "tianjin_branch": {"name": "天津分公司", "level": "branch", "parent": "weiyi_group", "region": "天津"},
    "jinan_branch": {"name": "济南分公司", "level": "branch", "parent": "weiyi_group", "region": "济南"},
    "beijing_branch": {"name": "北京分公司", "level": "branch", "parent": "weiyi_group", "region": "北京"},
    "shanghai_branch": {"name": "上海分公司", "level": "branch", "parent": "weiyi_group", "region": "上海"},
    "shenzhen_branch": {"name": "深圳分公司", "level": "branch", "parent": "weiyi_group", "region": "深圳"},
}

_ORG_DEPARTMENTS: dict[str, list[tuple[str, str]]] = {
    # branch_code -> [(dept_code, 部门名)]
    "weiyi_group": [
        ("medical_ops", "医疗运营部"), ("tech_rd", "技术研发部"), ("product_design", "产品设计部"),
        ("data_intel", "数据智能部"), ("marketing", "市场品牌部"), ("business_coop", "商务合作部"),
        ("finance", "财务部"), ("hr", "人力资源部"), ("compliance", "合规法务部"),
    ],
    "hangzhou_hq": [
        ("medical_ops", "医疗运营部"), ("tech_rd", "技术研发部"), ("product_design", "产品设计部"),
        ("data_intel", "数据智能部"), ("finance", "财务部"),
    ],
    "tianjin_branch": [("hc_ops", "健共体运营部"), ("medical_ops", "医疗运营部"), ("business_coop", "商务合作部"), ("admin", "综合管理部")],
    "jinan_branch": [("hc_ops", "健共体运营部"), ("public_health", "公共卫生部"), ("medical_ops", "医疗运营部"), ("admin", "综合管理部")],
    "beijing_branch": [("ioh_ops", "互联网医院运营部"), ("business_coop", "商务合作部"), ("admin", "综合管理部")],
    "shanghai_branch": [("insurance_coop", "保险合作部"), ("business_coop", "商务合作部"), ("admin", "综合管理部")],
    "shenzhen_branch": [("smart_med", "智慧医疗部"), ("business_coop", "商务合作部"), ("admin", "综合管理部")],
}

_ORG_TEAMS: dict[str, list[tuple[str, str]]] = {
    # dept_code -> [(team_code, 团队名)]
    "weiyi_group_tech_rd": [("platform_arch", "平台架构组"), ("biz_dev", "业务研发组"), ("data_platform", "数据平台组")],
    "weiyi_group_data_intel": [("dw", "数据仓库组"), ("algo", "算法组")],
}


# 部门名 → 组织类型（研发/运营/医疗/职能）
_DEPT_ORG_CATEGORY: dict[str, str] = {
    "技术研发部": "研发", "数据智能部": "研发", "产品设计部": "研发", "智慧医疗部": "研发",
    "医疗运营部": "医疗", "健共体运营部": "医疗", "互联网医院运营部": "医疗", "公共卫生部": "医疗",
    "市场品牌部": "运营", "商务合作部": "运营", "保险合作部": "运营",
    "财务部": "职能", "人力资源部": "职能", "合规法务部": "职能", "综合管理部": "职能",
}


def _org_members() -> list[dict[str, Any]]:
    """组织维度成员：集团 → 分公司 → 部门 → 团队 四级层级（SCD1 跟踪组织调整）。

    属性：
    - 集团/分公司：持有类型（直营/控股/参股，微医集团及分子公司均为直营）
    - 部门：组织类型（研发/运营/医疗/职能）
    """
    members: list[dict[str, Any]] = []
    for code, spec in _ORG_SEEDS.items():
        members.append(
            {
                "code": code,
                "name": spec["name"],
                "parent_code": spec.get("parent"),
                "attributes": {
                    "level": spec["level"],
                    "region": spec["region"],
                    "org_type": "总部" if spec["level"] == "group" else "分公司",
                    "holding_type": "直营",
                },
            }
        )
    for branch_code, depts in _ORG_DEPARTMENTS.items():
        for dept_code, dept_name in depts:
            full = f"{branch_code}_{dept_code}"
            members.append(
                {
                    "code": full,
                    "name": dept_name,
                    "parent_code": branch_code,
                    "attributes": {
                        "level": "department",
                        "org_type": "部门",
                        "holding_type": "直营",
                        "org_category": _DEPT_ORG_CATEGORY.get(dept_name, "职能"),
                    },
                }
            )
            for team_code, team_name in _ORG_TEAMS.get(full, []):
                members.append(
                    {
                        "code": f"{full}_{team_code}",
                        "name": team_name,
                        "parent_code": full,
                        "attributes": {
                            "level": "team",
                            "org_type": "团队",
                            "holding_type": "直营",
                            "org_category": _DEPT_ORG_CATEGORY.get(dept_name, "职能"),
                        },
                    }
                )
    return members


_CURRENCY_SEEDS: list[tuple[str, str, str, bool]] = [
    # (code, 名称, 符号, 是否本位币)
    ("cny", "人民币", "¥", True),
    ("usd", "美元", "$", False),
    ("hkd", "港币", "HK$", False),
    ("eur", "欧元", "€", False),
    ("gbp", "英镑", "£", False),
    ("jpy", "日元", "JP¥", False),
    ("sgd", "新加坡元", "S$", False),
    ("krw", "韩元", "₩", False),
    ("aud", "澳元", "A$", False),
    ("cad", "加元", "C$", False),
    ("chf", "瑞士法郎", "Fr", False),
    ("twd", "新台币", "NT$", False),
    ("thb", "泰铢", "฿", False),
    ("myr", "林吉特", "RM", False),
    ("aed", "迪拉姆", "AED", False),
]


def _currency_members() -> list[dict[str, Any]]:
    """币种维度成员：平铺 15 种常用币种，属性含符号/本位币标识（SCD0）。"""
    return [
        {
            "code": code,
            "name": name,
            "attributes": {"level": "currency", "symbol": symbol, "is_base": is_base},
        }
        for code, name, symbol, is_base in _CURRENCY_SEEDS
    ]


_CUSTOMER_SEEDS: dict[str, dict[str, Any]] = {
    # type_code -> {name: 类型名, customers: [(customer_code, 客户名, 区域, 合作模式)]}
    "corporate_health": {
        "name": "企业健康管理",
        "customers": [
            ("east_manufacturing", "华东先进制造集团", "华东", "年度健管服务"),
            ("south_retail", "华南连锁零售集团", "华南", "员工体检套餐"),
            ("north_energy", "华北能源集团", "华北", "职业健康监护"),
            ("west_mining", "西部矿业集团", "西北", "年度健管服务"),
            ("southeast_tech", "东南互联网科技集团", "华东", "弹性福利平台"),
        ],
    },
    "insurance_coop": {
        "name": "保险合作",
        "customers": [
            ("life_coop", "寿险合作机构", "全国", "商保直付网络"),
            ("property_coop", "财险合作机构", "全国", "健康险理赔"),
            ("health_coop", "健康险合作机构", "全国", "带病体保险"),
            ("huiminbao_ops", "惠民保运营机构", "多地", "惠民保运营服务"),
        ],
    },
    "hospital_client": {
        "name": "医院客户",
        "customers": [
            ("tertiary_alliance", "三甲医院联盟", "全国", "互联网医院共建"),
            ("private_group", "民营医院集团", "华东", "HIS/运营服务"),
            ("ioh_co_build", "互联网医院共建方", "多地", "平台共建运营"),
            ("chc_alliance", "社区卫生服务中心联盟", "浙江", "健共体协作"),
        ],
    },
    "gov_agency": {
        "name": "政府机构",
        "customers": [
            ("provincial_hc", "省级卫健委", "浙江", "数字健共体"),
            ("municipal_mi", "市级医保局", "多地", "医保智能审核"),
            ("district_cdc", "区级疾控中心", "天津", "公共卫生监测"),
            ("hc_committee", "健共体管理委员会", "山东", "区域医疗协作"),
        ],
    },
    "channel_partner": {
        "name": "渠道伙伴",
        "customers": [
            ("pharma_channel", "药企渠道伙伴", "全国", "处方流转合作"),
            ("hm_platform", "健康管理平台伙伴", "全国", "API 供数"),
            ("smart_hw", "智能硬件伙伴", "全国", "健康数据接入"),
            ("broker_partner", "保险经纪伙伴", "全国", "产品代销"),
        ],
    },
}


def _customer_members() -> list[dict[str, Any]]:
    """客户维度成员：客户类型 → 具体客户 两级层级（SCD1 跟踪合作状态）。

    设计取舍：客户为商业合作主体（B 端），与患者（临床个体）区分；
    客户明细主数据（合同/联系人）由客户主数据表承载，维度管理只承载分析层级。
    """
    members: list[dict[str, Any]] = []
    for type_code, spec in _CUSTOMER_SEEDS.items():
        members.append(
            {
                "code": type_code,
                "name": spec["name"],
                "attributes": {"level": "type"},
            }
        )
        for cust_code, cust_name, region, mode in spec["customers"]:
            members.append(
                {
                    "code": f"{type_code}_{cust_code}",
                    "name": cust_name,
                    "parent_code": type_code,
                    "attributes": {"level": "customer", "region": region, "cooperation_mode": mode},
                }
            )
    return members


_CAMPAIGN_SEEDS: dict[str, dict[str, Any]] = {
    # type_code -> {name: 活动类型名, campaigns: [(campaign_code, 活动名, 活动对象, 区域, 目标指标)]}
    "free_clinic": {
        "name": "义诊活动",
        "campaigns": [
            ("spring_free_clinic", "春季大型义诊", "全科患者", "杭州", "问诊量/挂号量"),
            ("mountain_village_clinic", "山区健康义诊行", "农村居民", "山东", "问诊量/筛查量"),
            ("eye_care_clinic", "爱眼日义诊", "眼疾患者", "天津", "挂号量/检查量"),
        ],
    },
    "health_day": {
        "name": "健康日活动",
        "campaigns": [
            ("hypertension_day", "世界高血压日", "慢病患者", "全国", "筛查量/复诊量"),
            ("tooth_day", "全国爱牙日", "口腔患者", "全国", "挂号量"),
            ("diabetes_day", "联合国糖尿病日", "糖友", "全国", "筛查量/健管签约"),
            ("lung_day", "世界慢阻肺日", "呼吸疾病患者", "浙江", "筛查量"),
        ],
    },
    "insurance_promo": {
        "name": "保险促销",
        "campaigns": [
            ("huiminbao_renewal", "惠民保续保季", "参保人", "浙江", "续保率/保费"),
            ("commercial_insurance_gift", "商保购险赠健管", "新投保人", "全国", "保费/健管激活"),
        ],
    },
    "acquisition": {
        "name": "拉新活动",
        "campaigns": [
            ("new_user_benefit", "新用户问诊立减", "新用户", "全国", "新客数/首问转化"),
            ("referral_bonus", "老带新奖励", "存量用户", "全国", "拉新数"),
            ("campus_health", "校园健康卡推广", "大学生", "杭州", "开卡数"),
        ],
    },
    "member_activity": {
        "name": "会员活动",
        "campaigns": [
            ("family_card_week", "家庭会员周", "会员", "全国", "会员开卡/续费"),
            ("point_mall_sale", "积分商城大促", "会员", "全国", "积分消耗/GMV"),
            ("health_lecture", "名医健康讲堂", "会员", "全国", "参与人次"),
        ],
    },
    "enterprise_health": {
        "name": "企业健管活动",
        "campaigns": [
            ("corp_annual_check", "企业年度体检季", "企业员工", "华东", "体检人数/企业签约"),
            ("corp_mental_week", "企业心理健康周", "企业员工", "华北", "咨询人次"),
            ("occupational_health_day", "职业健康宣传日", "企业员工", "西北", "监护人次"),
        ],
    },
}


def _campaign_members() -> list[dict[str, Any]]:
    """活动维度成员：活动类型 → 具体活动 两级层级（SCD0 活动类型稳定，活动实例滚动新增）。

    设计取舍：活动为主数据（活动系统承载预算/执行明细），维度管理承载
    「活动类型 → 活动」分析层级，供业务量（问诊/挂号/下单/获客）归因分析活动 ROI。
    """
    members: list[dict[str, Any]] = []
    for type_code, spec in _CAMPAIGN_SEEDS.items():
        members.append(
            {
                "code": type_code,
                "name": spec["name"],
                "attributes": {"level": "type"},
            }
        )
        for camp_code, camp_name, target, region, goal in spec["campaigns"]:
            members.append(
                {
                    "code": f"{type_code}_{camp_code}",
                    "name": camp_name,
                    "parent_code": type_code,
                    "attributes": {"level": "campaign", "target_audience": target, "region": region, "goal_metrics": goal},
                }
            )
    return members


DIMENSION_SEEDS: list[dict[str, Any]] = [
    {
        "dim_code": "outpatient_department", "name": "科室", "domain": "outpatient", "type": "SCD1",
        "description": "医院临床科室，SCD1 覆盖科室更名/合并。", "members": _dept_members(),
    },
    {
        "dim_code": "outpatient_doctor", "name": "医生", "domain": "outpatient", "type": "SCD2",
        "description": "出诊医生，attributes 含科室/职称，SCD2 跟踪职称变迁。", "members": _doctor_members(),
    },
    {
        "dim_code": "outpatient_visit_type", "name": "就诊类型", "domain": "outpatient", "type": "SCD0",
        "description": "门诊就诊类型划分。", "members": [
            {"code": "normal", "name": "普通门诊"},
            {"code": "expert", "name": "专家门诊"},
            {"code": "emergency", "name": "急诊"},
            {"code": "special", "name": "特需门诊"},
            {"code": "checkup", "name": "体检门诊"},
        ],
    },
    {
        "dim_code": "patient_profile", "name": "患者", "domain": "patient", "type": "SCD2",
        "description": "患者人群画像维度（年龄段×性别），SCD2 跟踪年龄段变化。", "members": _patient_members(),
    },
    {
        "dim_code": "diagnosis_disease", "name": "病种", "domain": "diagnosis", "type": "SCD1",
        "description": "疾病诊断分类（ICD-10 章），attributes 含章编码。", "members": _disease_members(),
    },
    {
        "dim_code": "medical_fee_item", "name": "收费项目", "domain": "medical_fee", "type": "SCD0",
        "description": "医疗收费项目类别。", "members": [
            {"code": "register", "name": "挂号费"}, {"code": "diag", "name": "诊疗费"},
            {"code": "exam", "name": "检查费"}, {"code": "lab", "name": "检验费"},
            {"code": "drug", "name": "药品费"}, {"code": "treat", "name": "治疗费"},
            {"code": "material", "name": "材料费"}, {"code": "surgery", "name": "手术费"},
        ],
    },
    {
        "dim_code": "medical_insurance_type", "name": "医保类型", "domain": "medical_insurance", "type": "SCD0",
        "description": "患者医保/支付类型。", "members": [
            {"code": "urban_worker", "name": "城镇职工医保"},
            {"code": "urban_rural", "name": "城乡居民医保"},
            {"code": "self_pay", "name": "自费"},
            {"code": "public", "name": "公费医疗"},
            {"code": "commercial", "name": "商业保险"},
        ],
    },
    {
        "dim_code": "medication_prescription_type", "name": "处方类型", "domain": "medication", "type": "SCD0",
        "description": "电子处方类型划分。", "members": [
            {"code": "western", "name": "西药处方"}, {"code": "patent", "name": "中成药处方"},
            {"code": "herbal", "name": "中药饮片处方"}, {"code": "antibiotic", "name": "抗菌药物处方"},
        ],
    },
    {
        "dim_code": "medication_drug_category", "name": "药品分类", "domain": "medication", "type": "SCD1",
        "description": "药品药理作用大类。", "members": [
            {"code": "anti_infective", "name": "抗感染药"}, {"code": "cardiovascular", "name": "心血管系统药"},
            {"code": "digestive", "name": "消化系统药"}, {"code": "respiratory", "name": "呼吸系统药"},
            {"code": "nervous", "name": "神经系统药"}, {"code": "hormone", "name": "激素及内分泌药"},
            {"code": "chinese_patent", "name": "中成药"}, {"code": "herbal", "name": "中药饮片"},
        ],
    },
    {
        "dim_code": "medical_insurance_settle_channel", "name": "结算渠道", "domain": "medical_insurance", "type": "SCD0",
        "description": "医疗费用结算渠道。", "members": [
            {"code": "window", "name": "窗口结算"}, {"code": "self_service", "name": "自助机结算"},
            {"code": "mobile", "name": "移动支付"}, {"code": "direct", "name": "医保直结"},
        ],
    },
    {
        "dim_code": "online_consult_channel", "name": "问诊渠道", "domain": "online_consultation", "type": "SCD0",
        "description": "在线问诊服务渠道（微医核心线上业务）。", "members": [
            {"code": "text", "name": "图文问诊"}, {"code": "phone", "name": "电话问诊"},
            {"code": "video", "name": "视频问诊"}, {"code": "team", "name": "专家团队问诊"},
        ],
    },
    {
        "dim_code": "appointment_register_type", "name": "预约类型", "domain": "appointment", "type": "SCD0",
        "description": "预约挂号类型。", "members": [
            {"code": "normal", "name": "普通号"}, {"code": "expert", "name": "专家号"},
            {"code": "special", "name": "特需号"}, {"code": "emergency", "name": "急诊号"},
            {"code": "followup", "name": "复诊号"},
        ],
    },
    {
        "dim_code": "register_shift", "name": "出诊时段", "domain": "appointment", "type": "SCD0",
        "description": "医生出诊班次时段，衡量号源与候诊负荷。", "members": [
            {"code": "morning", "name": "上午"}, {"code": "afternoon", "name": "下午"},
            {"code": "night", "name": "夜间"}, {"code": "weekend", "name": "周末"},
        ],
    },
    {
        "dim_code": "health_community_referral_type", "name": "转诊类型", "domain": "health_community", "type": "SCD0",
        "description": "健共体内双向转诊方向类型。", "members": [
            {"code": "upward", "name": "上转"}, {"code": "downward", "name": "下转"},
            {"code": "horizontal", "name": "平转"}, {"code": "consult", "name": "院间会诊转诊"},
        ],
    },
    {
        "dim_code": "health_community_alliance_type", "name": "医联体类型", "domain": "health_community", "type": "SCD0",
        "description": "医联体/医共体组织形态。", "members": [
            {"code": "city_group", "name": "城市医疗集团"}, {"code": "county_community", "name": "县域医共体"},
            {"code": "specialty_alliance", "name": "专科联盟"}, {"code": "remote_network", "name": "远程医疗协作网"},
        ],
    },
    {
        "dim_code": "health_community_sign_status", "name": "家医签约状态", "domain": "health_community", "type": "SCD0",
        "description": "居民家庭医生签约服务状态。", "members": [
            {"code": "signed", "name": "已签约"}, {"code": "renew", "name": "待续签"},
            {"code": "terminated", "name": "已解约"}, {"code": "unsigned", "name": "未签约"},
        ],
    },
    {
        "dim_code": "checkup_project", "name": "体检项目", "domain": "health_management", "type": "SCD0",
        "description": "健康体检检查项目类别。", "members": [
            {"code": "general", "name": "一般检查"}, {"code": "blood_routine", "name": "血常规"},
            {"code": "urine_routine", "name": "尿常规"}, {"code": "liver_func", "name": "肝功能"},
            {"code": "renal_func", "name": "肾功能"}, {"code": "blood_lipid", "name": "血脂"},
            {"code": "blood_glucose", "name": "血糖"}, {"code": "ecg", "name": "心电图"},
            {"code": "chest_xray", "name": "胸部X线"}, {"code": "abdominal_us", "name": "腹部超声"},
            {"code": "tumor_marker", "name": "肿瘤标志物"}, {"code": "hp_test", "name": "幽门螺杆菌检测"},
        ],
    },
    {
        "dim_code": "medication_drug_form", "name": "药品剂型", "domain": "medication", "type": "SCD0",
        "description": "药品制剂形态。", "members": [
            {"code": "tablet", "name": "片剂"}, {"code": "capsule", "name": "胶囊剂"},
            {"code": "injection", "name": "注射剂"}, {"code": "granule", "name": "颗粒剂"},
            {"code": "oral_liquid", "name": "口服液"}, {"code": "topical", "name": "外用制剂"},
            {"code": "aerosol", "name": "气雾剂"}, {"code": "eye_drop", "name": "滴眼剂"},
        ],
    },
    {
        "dim_code": "doctor_title", "name": "医生职称", "domain": "doctor_management", "type": "SCD0",
        "description": "医师职称等级（参照数据，用于医生服务与绩效分析）。", "members": [
            {"code": "chief", "name": "主任医师"},
            {"code": "associate_chief", "name": "副主任医师"},
            {"code": "attending", "name": "主治医师"},
            {"code": "resident", "name": "住院医师"},
            {"code": "physician", "name": "医师"},
        ],
    },
    {
        "dim_code": "medical_org", "name": "医疗机构", "domain": "health_community", "type": "SCD1",
        "description": "健共体内医疗机构等级与类型（分级诊疗分析地基）。", "members": [
            {"code": "tertiary", "name": "三级医院", "attributes": {"org_level": "三级", "org_type": "综合/专科"}},
            {"code": "secondary", "name": "二级医院", "attributes": {"org_level": "二级", "org_type": "综合"}},
            {"code": "primary", "name": "一级医院", "attributes": {"org_level": "一级", "org_type": "综合"}},
            {"code": "community", "name": "社区卫生服务中心", "attributes": {"org_level": "基层", "org_type": "社区"}},
            {"code": "township", "name": "乡镇卫生院", "attributes": {"org_level": "基层", "org_type": "乡镇"}},
            {"code": "village_clinic", "name": "村卫生室", "attributes": {"org_level": "基层", "org_type": "村级"}},
            {"code": "internet_hospital", "name": "互联网医院", "attributes": {"org_level": "线上", "org_type": "互联网"}},
        ],
    },
    {
        "dim_code": "patient_source_channel", "name": "患者来源渠道", "domain": "online_consultation", "type": "SCD0",
        "description": "患者触达平台的服务渠道（线上获客分析维度）。", "members": [
            {"code": "app", "name": "微医APP"},
            {"code": "wechat_mini", "name": "微信小程序"},
            {"code": "wechat_oa", "name": "微信公众号"},
            {"code": "h5", "name": "H5页面"},
            {"code": "offline_qr", "name": "线下扫码"},
            {"code": "referral", "name": "转介绍"},
        ],
    },
    {
        "dim_code": "visit_status", "name": "就诊状态", "domain": "outpatient", "type": "SCD0",
        "description": "患者就诊流程状态（预约-候诊-就诊-完成漏斗分析维度）。", "members": [
            {"code": "pending", "name": "待就诊"},
            {"code": "waiting", "name": "候诊中"},
            {"code": "visiting", "name": "就诊中"},
            {"code": "completed", "name": "已完成"},
            {"code": "cancelled", "name": "已取消"},
            {"code": "no_show", "name": "爽约"},
        ],
    },
    {
        "dim_code": "payment_method", "name": "支付方式", "domain": "medical_fee", "type": "SCD0",
        "description": "患者医疗费用支付工具（交易分析维度）。", "members": [
            {"code": "wechat", "name": "微信支付"},
            {"code": "alipay", "name": "支付宝"},
            {"code": "unionpay", "name": "银联"},
            {"code": "medical_account", "name": "医保个账"},
            {"code": "insurance_direct", "name": "商保直付"},
        ],
    },
    {
        "dim_code": "insurance_product_type", "name": "保险产品类型", "domain": "insurance", "type": "SCD0",
        "description": "保险业务产品线（商保直付/惠民保/经纪业务分析维度）。", "members": [
            {"code": "medical", "name": "医疗险"},
            {"code": "critical_illness", "name": "重疾险"},
            {"code": "huimin", "name": "惠民保"},
            {"code": "accident", "name": "意外险"},
            {"code": "life", "name": "寿险"},
            {"code": "annuity", "name": "年金险"},
        ],
    },
    {
        "dim_code": "member_level", "name": "会员等级", "domain": "membership", "type": "SCD0",
        "description": "会员成长等级（会员运营与权益成本分析维度）。", "members": [
            {"code": "normal", "name": "普通会员"},
            {"code": "silver", "name": "银卡会员"},
            {"code": "gold", "name": "金卡会员"},
            {"code": "platinum", "name": "铂金会员"},
            {"code": "family", "name": "家庭会员"},
        ],
    },
    {
        "dim_code": "specialty_center_type", "name": "专科中心", "domain": "specialty_center", "type": "SCD0",
        "description": "跨域专科专病中心（专科运营与专病管理分析维度）。", "members": [
            {"code": "oncology", "name": "肿瘤中心"},
            {"code": "women_children", "name": "妇儿中心"},
            {"code": "mental_health", "name": "心理精神"},
            {"code": "stomatology", "name": "口腔中心"},
            {"code": "tcm", "name": "中医中心"},
            {"code": "chronic", "name": "慢病专病"},
        ],
    },
    {
        "dim_code": "care_service_type", "name": "照护类型", "domain": "care_elderly", "type": "SCD0",
        "description": "康复护理养老服务类型（养老与长护业务分析维度）。", "members": [
            {"code": "home_care", "name": "居家护理"},
            {"code": "rehab", "name": "康复治疗"},
            {"code": "ltc", "name": "长护险护理"},
            {"code": "elderly", "name": "养老照护"},
        ],
    },
    {
        "dim_code": "ai_scene", "name": "AI应用场景", "domain": "health_ai", "type": "SCD0",
        "description": "医疗AI产品应用场景（AI能力运营与效果分析维度）。", "members": [
            {"code": "triage", "name": "智能导诊"},
            {"code": "diag_assist", "name": "AI辅诊"},
            {"code": "ai_report", "name": "智能报告"},
            {"code": "record_qc", "name": "病历质控"},
            {"code": "health_qa", "name": "健康问答"},
        ],
    },
    {
        "dim_code": "research_type", "name": "科研类型", "domain": "clinical_research", "type": "SCD0",
        "description": "临床科研项目类型（科研业务与成果分析维度）。", "members": [
            {"code": "rws", "name": "真实世界研究"},
            {"code": "clinical_trial", "name": "临床试验"},
            {"code": "research_collab", "name": "科研协作"},
            {"code": "data_extraction", "name": "科研数据提取"},
        ],
    },
    # ---- 公共维度（横切基础参照，不归属业务线）----
    {
        "dim_code": "common_date", "name": "日期", "domain": "common", "type": "SCD0",
        "description": "公共日期维度（dim_date 参照）：年→季→月三级层级，层级节点属性含财年/闰年、财季/季节、当月主要法定节假日与节气；日级属性（工作日/周末、节假日调休、自然周/ISO 周、节气精确日、农历日）由物理 dim_date 表按日承载（365 行/年），维度层级不重复灌每日明细。指标按时间切片/钻取分析引用此维度。",
        "members": _date_members(),
    },
    {
        "dim_code": "common_region", "name": "地区", "domain": "common", "type": "SCD1",
        "description": "公共地区维度（dim_region 参照）：省→市→区县三级层级（parent_code/path），覆盖微医核心业务区域；属性含大区（华东/华北/华南…）、城市等级（一线/新一线/二线）、是否健共体试点省（浙江/天津/山东）、是否互联网医院落地省，用于按地域分析问诊/挂号/药品/健共体协作等业务量。SCD1 覆盖行政区划调整更名。",
        "members": _region_members(),
    },
    {
        "dim_code": "common_time", "name": "时间", "domain": "common", "type": "SCD0",
        "description": "公共时间维度（日内）：时段→小时两级层级（凌晨/清晨/上午/中午/下午/傍晚/夜间/深夜 8 时段 + 24 小时），属性含班次（早/白/中/夜班）与就诊高峰标识，用于分时就诊/预约高峰/夜间急诊等日内分析；分钟/秒由物理时间戳承载，与日期维度（跨天）互补。",
        "members": _time_members(),
    },
    {
        "dim_code": "common_org", "name": "组织", "domain": "common", "type": "SCD1",
        "description": "公共组织维度（dim_org 参照）：集团→分公司→部门→团队四级层级（parent_code/path），覆盖微医集团及杭州/天津/济南/北京/上海/深圳分公司组织架构；属性含持有类型（直营/控股/参股）与组织类型（研发/运营/医疗/职能），用于人效/成本/预算归口等内部经营分析；区别于临床科室（医疗业务视角）。SCD1 跟踪组织调整。",
        "members": _org_members(),
    },
    {
        "dim_code": "common_currency", "name": "币种", "domain": "common", "type": "SCD0",
        "description": "公共币种维度（dim_currency 参照）：人民币/美元/港币/欧元/英镑/日元等 15 种常用币种，属性含符号与本位币标识（人民币），用于金额类指标的多币种结算（对外结算/保险理赔/跨境合作）。",
        "members": _currency_members(),
    },
    {
        "dim_code": "common_customer", "name": "客户", "domain": "common", "type": "SCD1",
        "description": "公共客户维度（dim_customer 参照）：客户类型→具体客户两级层级（企业健康管理/保险合作/医院客户/政府机构/渠道伙伴），覆盖商业合作主体，区别于患者（临床个体）；用于商业客户经营分析（合同/营收/合作模式）。SCD1 跟踪合作状态变化。",
        "members": _customer_members(),
    },
    {
        "dim_code": "common_campaign", "name": "活动", "domain": "common", "type": "SCD0",
        "description": "公共活动维度（dim_campaign 参照）：活动类型→具体活动两级层级（义诊活动/健康日活动/保险促销/拉新活动/会员活动/企业健管活动），属性含活动对象/区域/目标指标；用于活动→业务量（问诊/挂号/下单/获客）归因分析，衡量活动 ROI。活动实例为滚动主数据，SCD0 类型稳定、活动实例滚动新增。",
        "members": _campaign_members(),
    },
    # ---- 第二轮公共维度（属性类横切维度：年龄段/性别/学历/民族）----
    {
        "dim_code": "common_age_group", "name": "年龄段", "domain": "common", "type": "SCD0",
        "description": "公共年龄段维度（dim_age_group 参照）：年龄区间分桶（婴幼儿/学龄前/儿童/青少年/青年/中年/老年/高龄），attributes 含区间范围；供任何业务按年龄统一切片（门诊量年龄分布、慢病年龄特征、健康教育触达分层）。SCD0 分桶口径稳定。",
        "members": [
            {"code": "infant", "name": "婴幼儿", "attributes": {"range": "0-3岁"}},
            {"code": "preschool", "name": "学龄前", "attributes": {"range": "4-6岁"}},
            {"code": "child", "name": "儿童", "attributes": {"range": "7-12岁"}},
            {"code": "teen", "name": "青少年", "attributes": {"range": "13-17岁"}},
            {"code": "young", "name": "青年", "attributes": {"range": "18-35岁"}},
            {"code": "middle", "name": "中年", "attributes": {"range": "36-59岁"}},
            {"code": "senior", "name": "老年", "attributes": {"range": "60-74岁"}},
            {"code": "elder", "name": "高龄", "attributes": {"range": "75岁及以上"}},
        ],
    },
    {
        "dim_code": "common_gender", "name": "性别", "domain": "common", "type": "SCD0",
        "description": "公共性别维度（dim_gender 参照）：男/女/未知，供各业务按性别统一切片（问诊性别比、病种性别特征、药品消费差异）。",
        "members": [
            {"code": "male", "name": "男"},
            {"code": "female", "name": "女"},
            {"code": "unknown", "name": "未知"},
        ],
    },
    {
        "dim_code": "common_education", "name": "学历", "domain": "common", "type": "SCD0",
        "description": "公共学历维度（dim_education 参照）：小学及以下至博士，用于人群画像、健康教育触达分层与患者服务分析。",
        "members": [
            {"code": "primary", "name": "小学及以下"},
            {"code": "junior", "name": "初中"},
            {"code": "high", "name": "高中"},
            {"code": "secondary", "name": "中专/技校"},
            {"code": "college", "name": "大专"},
            {"code": "bachelor", "name": "本科"},
            {"code": "master", "name": "硕士"},
            {"code": "doctor", "name": "博士"},
        ],
    },
    {
        "dim_code": "common_ethnicity", "name": "民族", "domain": "common", "type": "SCD0",
        "description": "公共民族维度（dim_ethnicity 参照）：汉族及主要少数民族，用于多民族地区医疗健康服务覆盖与语言服务分析。",
        "members": [
            {"code": "han", "name": "汉族"},
            {"code": "zhuang", "name": "壮族"},
            {"code": "hui", "name": "回族"},
            {"code": "manchu", "name": "满族"},
            {"code": "uyghur", "name": "维吾尔族"},
            {"code": "miao", "name": "苗族"},
            {"code": "yi", "name": "彝族"},
            {"code": "tujia", "name": "土家族"},
            {"code": "tibetan", "name": "藏族"},
            {"code": "mongol", "name": "蒙古族"},
            {"code": "other", "name": "其他民族"},
        ],
    },
    # ---- 第二轮业务维度（住院/手术/检查检验/医保目录/DRG/给药/配送/复诊/慢病/会诊/疫苗/体检）----
    {
        "dim_code": "inpatient_ward", "name": "病区", "domain": "outpatient", "type": "SCD1",
        "description": "住院病区维度（dim_ward 参照）：按诊疗需要划分的护理单元（内科/外科/ICU/康复病区等），attributes 含护理等级与隔离标识；用于住院运营分析（床位使用、平均住院日、护理负荷）。SCD1 跟踪病区调整合并。",
        "members": [
            {"code": "internal_medicine", "name": "内科病区", "attributes": {"care_level": "综合", "isolation": False}},
            {"code": "surgery_ward", "name": "外科病区", "attributes": {"care_level": "综合", "isolation": False}},
            {"code": "gyn_ob", "name": "妇产科病区", "attributes": {"care_level": "综合", "isolation": False}},
            {"code": "pediatric", "name": "儿科病区", "attributes": {"care_level": "综合", "isolation": False}},
            {"code": "emergency_ward", "name": "急诊病区", "attributes": {"care_level": "综合", "isolation": False}},
            {"code": "icu", "name": "ICU重症病区", "attributes": {"care_level": "特级", "isolation": False}},
            {"code": "cardio", "name": "心血管病区", "attributes": {"care_level": "综合", "isolation": False}},
            {"code": "neuro", "name": "神经内科病区", "attributes": {"care_level": "综合", "isolation": False}},
            {"code": "oncology", "name": "肿瘤病区", "attributes": {"care_level": "综合", "isolation": False}},
            {"code": "rehab_ward", "name": "康复病区", "attributes": {"care_level": "综合", "isolation": False}},
        ],
    },
    {
        "dim_code": "surgery_type", "name": "手术类型", "domain": "outpatient", "type": "SCD0",
        "description": "手术等级维度（dim_surgery 参照）：按难度/风险/资源消耗划分的一级~四级手术，attributes 含是否微创/是否急诊；用于手术运营与绩效分析（手术量、四级手术占比、微创率）。",
        "members": [
            {"code": "level1", "name": "一级手术", "attributes": {"minimal_invasive": False, "emergency": False}},
            {"code": "level2", "name": "二级手术", "attributes": {"minimal_invasive": False, "emergency": False}},
            {"code": "level3", "name": "三级手术", "attributes": {"minimal_invasive": True, "emergency": False}},
            {"code": "level4", "name": "四级手术", "attributes": {"minimal_invasive": True, "emergency": True}},
        ],
    },
    {
        "dim_code": "exam_item", "name": "检查检验项目", "domain": "outpatient", "type": "SCD0",
        "description": "检查检验项目维度（dim_exam_item 参照）：医技检查（影像/超声/内镜）与检验（血/尿/生化）项目，attributes 含类别（检验/检查）；用于医技工作量、开单与报告时效分析。",
        "members": [
            {"code": "blood_routine", "name": "血常规", "attributes": {"category": "lab"}},
            {"code": "urine_routine", "name": "尿常规", "attributes": {"category": "lab"}},
            {"code": "liver_function", "name": "肝功能", "attributes": {"category": "lab"}},
            {"code": "kidney_function", "name": "肾功能", "attributes": {"category": "lab"}},
            {"code": "blood_glucose", "name": "血糖", "attributes": {"category": "lab"}},
            {"code": "blood_lipid", "name": "血脂", "attributes": {"category": "lab"}},
            {"code": "electrocardiogram", "name": "心电图", "attributes": {"category": "exam"}},
            {"code": "chest_xray", "name": "胸部X线", "attributes": {"category": "exam"}},
            {"code": "ct", "name": "CT检查", "attributes": {"category": "exam"}},
            {"code": "mri", "name": "核磁共振", "attributes": {"category": "exam"}},
            {"code": "ultrasound", "name": "超声检查", "attributes": {"category": "exam"}},
            {"code": "gastroscopy", "name": "胃镜", "attributes": {"category": "exam"}},
            {"code": "colonoscopy", "name": "肠镜", "attributes": {"category": "exam"}},
        ],
    },
    {
        "dim_code": "medical_insurance_catalog", "name": "医保目录类型", "domain": "medical_insurance", "type": "SCD0",
        "description": "医保目录类型维度（dim_yb_catalog 参照）：甲类/乙类/丙类/自费，attributes 含报销特征说明；用于医保目录结构、自费占比与费用负担分析。",
        "members": [
            {"code": "class_a", "name": "甲类", "attributes": {"reimburse_hint": "全额纳入报销范围"}},
            {"code": "class_b", "name": "乙类", "attributes": {"reimburse_hint": "个人先自付部分后按比例报销"}},
            {"code": "class_c", "name": "丙类", "attributes": {"reimburse_hint": "目录外，全额自费"}},
            {"code": "self_pay", "name": "自费", "attributes": {"reimburse_hint": "不在医保目录内"}},
        ],
    },
    {
        "dim_code": "drg_group", "name": "DRG/DIP分组", "domain": "medical_insurance", "type": "SCD1",
        "description": "DRG/DIP 病组维度（dim_drg 参照）：按疾病诊断相关分组的主要系统（循环/呼吸/消化等），attributes 含支付模型（DRG/DIP）；用于医保支付改革下的病组费用、结构变化与超支分析。SCD1 跟踪分组规则调整。",
        "members": [
            {"code": "circulation", "name": "循环系统疾病及功能障碍", "attributes": {"payment_model": "DRG"}},
            {"code": "respiratory", "name": "呼吸系统疾病及功能障碍", "attributes": {"payment_model": "DRG"}},
            {"code": "digestive", "name": "消化系统疾病及功能障碍", "attributes": {"payment_model": "DRG"}},
            {"code": "nervous", "name": "神经系统疾病及功能障碍", "attributes": {"payment_model": "DRG"}},
            {"code": "musculoskeletal", "name": "肌肉骨骼系统疾病及功能障碍", "attributes": {"payment_model": "DRG"}},
            {"code": "endocrine", "name": "内分泌营养代谢疾病及功能障碍", "attributes": {"payment_model": "DRG"}},
            {"code": "urogenital", "name": "泌尿生殖系统疾病及功能障碍", "attributes": {"payment_model": "DRG"}},
            {"code": "ob_gyn", "name": "妇产系统疾病及功能障碍", "attributes": {"payment_model": "DRG"}},
            {"code": "newborn", "name": "新生儿疾病及功能障碍", "attributes": {"payment_model": "DRG"}},
            {"code": "injury", "name": "损伤中毒及外因", "attributes": {"payment_model": "DRG"}},
        ],
    },
    {
        "dim_code": "medication_route", "name": "给药途径", "domain": "medication", "type": "SCD0",
        "description": "给药途径维度（dim_drug_route 参照）：口服/静脉/外用/吸入等，attributes 含给药系统说明；用于合理用药、用药安全与处方结构分析。",
        "members": [
            {"code": "oral", "name": "口服"},
            {"code": "iv_injection", "name": "静脉注射"},
            {"code": "iv_infusion", "name": "静脉滴注"},
            {"code": "im", "name": "肌肉注射"},
            {"code": "sc", "name": "皮下注射"},
            {"code": "topical", "name": "外用"},
            {"code": "inhalation", "name": "吸入"},
            {"code": "sublingual", "name": "舌下含服"},
            {"code": "rectal", "name": "直肠给药"},
            {"code": "ophthalmic", "name": "眼部给药"},
            {"code": "otic", "name": "耳部给药"},
            {"code": "nasal", "name": "鼻部给药"},
        ],
    },
    {
        "dim_code": "drug_delivery_method", "name": "药品配送方式", "domain": "internet_hospital", "type": "SCD0",
        "description": "药品配送方式维度（dim_drug_delivery 参照）：门店自提/快递/药房配送/冷链等，用于配送履约时效、服务体验与成本分析。",
        "members": [
            {"code": "store_pickup", "name": "门店自提"},
            {"code": "express", "name": "快递配送"},
            {"code": "pharmacy_delivery", "name": "合作药房配送"},
            {"code": "hospital_pickup", "name": "医院药房取药"},
            {"code": "cold_chain", "name": "冷链配送"},
        ],
    },
    {
        "dim_code": "online_followup_type", "name": "复诊类型", "domain": "internet_hospital", "type": "SCD0",
        "description": "在线复诊类型维度（dim_followup_type 参照）：图文/视频/电话/处方续方复诊，用于复诊业务结构、服务体验与续方流转分析。",
        "members": [
            {"code": "text_followup", "name": "图文复诊"},
            {"code": "video_followup", "name": "视频复诊"},
            {"code": "phone_followup", "name": "电话复诊"},
            {"code": "prescription_renew", "name": "处方续方复诊"},
        ],
    },
    {
        "dim_code": "chronic_disease_type", "name": "慢病类型", "domain": "health_management", "type": "SCD1",
        "description": "慢病类型维度（dim_chronic_type 参照）：高血压/糖尿病/冠心病/慢阻肺等常见慢病病种，用于慢病管理人群分层、随访计划与干预效果分析。SCD1 跟踪病种管理规范更新。",
        "members": [
            {"code": "hypertension", "name": "高血压"},
            {"code": "diabetes", "name": "糖尿病"},
            {"code": "coronary", "name": "冠心病"},
            {"code": "copd", "name": "慢性阻塞性肺疾病"},
            {"code": "stroke", "name": "脑卒中"},
            {"code": "ckd", "name": "慢性肾病"},
            {"code": "hyperlipidemia", "name": "高血脂"},
            {"code": "asthma", "name": "哮喘"},
            {"code": "rheumatoid", "name": "类风湿性关节炎"},
            {"code": "osteoporosis", "name": "骨质疏松"},
        ],
    },
    {
        "dim_code": "remote_consult_type", "name": "远程会诊类型", "domain": "health_community", "type": "SCD0",
        "description": "远程会诊类型维度（dim_remote_consult 参照）：同步实时/异步资料/疑难病例/急会诊，用于健共体远程协作量、时效与疑难病例转诊分析。",
        "members": [
            {"code": "realtime", "name": "同步会诊（实时视频）"},
            {"code": "async_consult", "name": "异步会诊（影像/资料）"},
            {"code": "difficult_case", "name": "疑难病例会诊"},
            {"code": "emergency_consult", "name": "急会诊"},
        ],
    },
    {
        "dim_code": "vaccine_type", "name": "疫苗类型", "domain": "health_management", "type": "SCD0",
        "description": "疫苗类型维度（dim_vaccine 参照）：一类免疫规划疫苗与二类自愿接种疫苗，attributes 含类别（一类/二类）；用于疫苗接种覆盖、预防保健与异常反应监测分析。",
        "members": [
            {"code": "hepb", "name": "乙肝疫苗", "attributes": {"category": "class1"}},
            {"code": "bcg", "name": "卡介苗", "attributes": {"category": "class1"}},
            {"code": "polio", "name": "脊髓灰质炎疫苗", "attributes": {"category": "class1"}},
            {"code": "dtap", "name": "百白破疫苗", "attributes": {"category": "class1"}},
            {"code": "measles", "name": "麻疹疫苗", "attributes": {"category": "class1"}},
            {"code": "meningitis", "name": "流脑疫苗", "attributes": {"category": "class1"}},
            {"code": "japanese_encephalitis", "name": "乙脑疫苗", "attributes": {"category": "class1"}},
            {"code": "hepa", "name": "甲肝疫苗", "attributes": {"category": "class1"}},
            {"code": "influenza", "name": "流感疫苗", "attributes": {"category": "class2"}},
            {"code": "pneumonia", "name": "肺炎疫苗", "attributes": {"category": "class2"}},
            {"code": "hpv", "name": "HPV疫苗", "attributes": {"category": "class2"}},
            {"code": "rabies", "name": "狂犬疫苗", "attributes": {"category": "class2"}},
            {"code": "varicella", "name": "水痘疫苗", "attributes": {"category": "class2"}},
            {"code": "shingles", "name": "带状疱疹疫苗", "attributes": {"category": "class2"}},
        ],
    },
    {
        "dim_code": "checkup_package", "name": "体检套餐", "domain": "health_management", "type": "SCD0",
        "description": "体检套餐维度（dim_checkup_package 参照）：入职/基础/全面/专项/老年等体检组合产品，attributes 含适用人群；用于体检业务量、套餐转化与客单分析。体检套餐为组合产品，滚动新增（SCD0 类型稳定、套餐实例滚动）。",
        "members": [
            {"code": "entry", "name": "入职体检", "attributes": {"target": "企业入职"}},
            {"code": "basic", "name": "基础体检套餐", "attributes": {"target": "大众基础"}},
            {"code": "standard", "name": "标准体检套餐", "attributes": {"target": "大众标准"}},
            {"code": "comprehensive", "name": "全面体检套餐", "attributes": {"target": "全面深度"}},
            {"code": "female_special", "name": "女性专项套餐", "attributes": {"target": "女性"}},
            {"code": "male_special", "name": "男性专项套餐", "attributes": {"target": "男性"}},
            {"code": "senior", "name": "老年体检套餐", "attributes": {"target": "老年人群"}},
            {"code": "cancer_screening", "name": "肿瘤早筛套餐", "attributes": {"target": "高风险人群"}},
            {"code": "cardiovascular", "name": "心血管专项", "attributes": {"target": "心血管风险人群"}},
            {"code": "diabetes_screening", "name": "糖尿病专项", "attributes": {"target": "血糖异常人群"}},
        ],
    },
]


# ---------------------------------------------------------------------------
# 清除 E2E/测试参照数据
# ---------------------------------------------------------------------------
async def clear_mock_dimensions(db: AsyncSession) -> int:
    """物理删除 E2E/测试维度及其引用（member/mapping/metric_dimension/reconciliation）。"""
    dims = (
        await db.execute(
            select(Dimension.dim_code).where(
                Dimension.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    targets = [c for c in dims if c.startswith(_MOCK_DIM_PREFIXES)]
    if not targets:
        # 无 mock 维度时跳过引用清理，但孤儿对账记录清理仍须执行（见下方）
        logger.info("mock_dimensions_cleared", codes=[])
    else:
        # 先删引用，再删维度本体（SQLAlchemy delete-orphan 不触发，须显式 Core delete）
        await db.execute(delete(DimensionMember).where(DimensionMember.dim_code.in_(targets)))
        await db.execute(delete(MetricDimension).where(MetricDimension.dim_code.in_(targets)))
        await db.execute(
            delete(DimensionMapping).where(
                (DimensionMapping.source_dim_code.in_(targets))
                | (DimensionMapping.target_dim_code.in_(targets))
            )
        )
        await db.execute(delete(Dimension).where(Dimension.dim_code.in_(targets)))
        logger.info("mock_dimensions_cleared", codes=targets)
    # 清除指向已不存在维度的孤儿对账记录（历史残留，dim_code 不在现存维度集合）
    surviving = (
        await db.execute(select(Dimension.dim_code).where(Dimension.deleted_at.is_(None)))
    ).scalars().all()
    if surviving:
        await db.execute(
            delete(Reconciliation).where(
                Reconciliation.dim_code.is_not(None),
                Reconciliation.dim_code.not_in(surviving),
            )
        )
    else:
        await db.execute(delete(Reconciliation).where(Reconciliation.dim_code.is_not(None)))
    return len(targets)


async def clear_mock_terms(db: AsyncSession) -> int:
    """物理删除 E2E/测试术语及其关联（version/relation/conflict）。"""
    terms = (
        await db.execute(select(Term.id, Term.term_code).where(Term.deleted_at.is_(None)))
    ).all()
    targets = [t for t in terms if t.term_code.startswith(_MOCK_TERM_PREFIXES)]
    if not targets:
        return 0
    term_ids = [t.id for t in targets]
    await db.execute(delete(TermVersion).where(TermVersion.term_id.in_(term_ids)))
    await db.execute(
        delete(TermRelation).where(
            (TermRelation.source_term_id.in_(term_ids)) | (TermRelation.target_term_id.in_(term_ids))
        )
    )
    await db.execute(
        delete(GlossaryConflict).where(
            (GlossaryConflict.term_id.in_(term_ids)) | (GlossaryConflict.ref_term_id.in_(term_ids))
        )
    )
    await db.execute(delete(Term).where(Term.id.in_(term_ids)))
    logger.info("mock_terms_cleared", codes=[t.term_code for t in targets])
    return len(targets)


# ---------------------------------------------------------------------------
# 灌入主题域
# ---------------------------------------------------------------------------
async def seed_domains(db: AsyncSession) -> int:
    """确保微医主题域树存在（一级域 + 二级子域），返回新增数。"""
    created = 0
    existing = {
        row.code: row
        for row in (
            await db.execute(
                select(SubjectDomain).where(SubjectDomain.deleted_at.is_(None))
            )
        ).scalars()
    }
    # 先确保一级域（parent=None 的种子）已存在
    for seed in DOMAIN_SEEDS:
        if seed["parent"] is not None:
            continue
        if seed["code"] in existing:
            continue
        node = SubjectDomain(
            code=seed["code"],
            name=seed["name"],
            parent_id=None,
            level=1,
            path=None,
            sort_order=seed["sort_order"],
            status="active",
            defaults_json={},
            description=f"微医业务主题域: {seed['name']}",
            owner_id=DOMAIN_OWNER_ID,
        )
        db.add(node)
        await db.flush()
        node.path = str(node.id)
        existing[seed["code"]] = node
        created += 1
        logger.info("domain_created", code=seed["code"], id=node.id)
    # 再灌二级子域
    for seed in DOMAIN_SEEDS:
        if seed["parent"] is None:
            continue
        if seed["code"] in existing:
            continue
        parent = existing.get(seed["parent"])
        if parent is None:
            logger.warning("domain_parent_missing", code=seed["code"], parent=seed["parent"])
            continue
        node = SubjectDomain(
            code=seed["code"],
            name=seed["name"],
            parent_id=parent.id,
            level=2,
            path=f"{parent.path}.{0}",  # flush 后回填
            sort_order=seed["sort_order"],
            status="active",
            defaults_json={},
            description=f"微医业务主题域: {seed['name']}",
            owner_id=DOMAIN_OWNER_ID,
        )
        db.add(node)
        await db.flush()
        node.path = f"{parent.path}.{node.id}"
        existing[seed["code"]] = node
        created += 1
        logger.info("domain_created", code=seed["code"], id=node.id, parent=parent.code)
    return created


# ---------------------------------------------------------------------------
# 灌入术语
# ---------------------------------------------------------------------------
async def seed_terms(db: AsyncSession) -> int:
    """灌入微医业务术语（PUBLISHED 直灌），返回新增数。"""
    created = 0
    existing = {
        row.term_code
        for row in (
            await db.execute(select(Term.term_code).where(Term.deleted_at.is_(None)))
        ).all()
    }
    for spec in TERM_SEEDS:
        if spec["term_code"] in existing:
            continue
        db.add(
            Term(
                term_code=spec["term_code"],
                name=spec["name"],
                definition=spec["definition"],
                domain=spec["domain"],
                synonyms=spec["synonyms"],
                boundary=spec["boundary"],
                status="PUBLISHED",
                owner_id=TERM_OWNER_ID,
            )
        )
        created += 1
        logger.info("term_created", term_code=spec["term_code"])
    await db.flush()
    return created


# ---------------------------------------------------------------------------
# 灌入维度 + 成员
# ---------------------------------------------------------------------------
def _member_fingerprint(code: str, parent_code: str | None, attributes: dict | None) -> tuple:
    """成员指纹：code + parent_code + 规范化 attributes 的稳定三元组。

    用于判断成员是否与脚本定义一致——仅编码一致但属性/层级变更也会触发刷新。
    """
    return (
        code,
        parent_code,
        json.dumps(attributes or {}, sort_keys=True, ensure_ascii=False),
    )


async def seed_dimensions(db: AsyncSession) -> int:
    """灌入医疗业务维度及成员（PUBLISHED 直灌），返回新增维度数。

    幂等增强：维度已存在时校验成员集合是否与脚本定义一致（如脚本扩充了成员），
    不一致则先删旧成员再重灌，保证参照数据与脚本同步；一致则跳过。
    """
    created = 0
    refreshed = 0
    existing = {
        row.dim_code
        for row in (
            await db.execute(select(Dimension.dim_code).where(Dimension.deleted_at.is_(None)))
        ).all()
    }
    for spec in DIMENSION_SEEDS:
        dim_code = spec["dim_code"]
        if dim_code not in existing:
            dim = Dimension(
                dim_code=dim_code,
                name=spec["name"],
                domain=spec["domain"],
                type=spec["type"],
                description=spec["description"],
                owner_id=DIM_OWNER_ID,
                status="PUBLISHED",
            )
            db.add(dim)
            await db.flush()
            for m in spec["members"]:
                db.add(
                    DimensionMember(
                        dim_code=dim.dim_code,
                        member_code=m["code"],
                        member_name=m["name"],
                        parent_code=m.get("parent_code"),
                        path=m.get("path"),
                        attributes=m.get("attributes"),
                        status="PUBLISHED",
                    )
                )
            created += 1
            logger.info("dimension_created", dim_code=dim_code, members=len(spec["members"]))
            continue
        # 已存在：成员指纹集合（code+parent+attributes）不一致则刷新（删旧重灌）
        # 保证与脚本定义一致（仅编码一致但属性/层级变更也会触发刷新）
        current_rows = (
            await db.execute(
                select(
                    DimensionMember.member_code,
                    DimensionMember.parent_code,
                    DimensionMember.attributes,
                ).where(
                    DimensionMember.dim_code == dim_code,
                    DimensionMember.deleted_at.is_(None),
                )
            )
        ).all()
        current = {_member_fingerprint(r.member_code, r.parent_code, r.attributes) for r in current_rows}
        expected = {
            _member_fingerprint(m["code"], m.get("parent_code"), m.get("attributes"))
            for m in spec["members"]
        }
        if current == expected:
            continue
        await db.execute(delete(DimensionMember).where(DimensionMember.dim_code == dim_code))
        for m in spec["members"]:
            db.add(
                DimensionMember(
                    dim_code=dim_code,
                    member_code=m["code"],
                    member_name=m["name"],
                    parent_code=m.get("parent_code"),
                    path=m.get("path"),
                    attributes=m.get("attributes"),
                    status="PUBLISHED",
                )
            )
        refreshed += 1
        logger.info(
            "dimension_refreshed",
            dim_code=dim_code,
            old_members=len(current),
            new_members=len(spec["members"]),
        )
    await db.flush()
    if refreshed:
        logger.info("dimensions_refreshed_total", count=refreshed)
    return created


async def run() -> None:
    """执行 seed：清除 mock → 灌入参照数据。"""
    configure_logging()
    async with async_session_factory() as db:
        try:
            cleared_dims = await clear_mock_dimensions(db)
            cleared_terms = await clear_mock_terms(db)
            domain_count = await seed_domains(db)
            term_count = await seed_terms(db)
            dim_count = await seed_dimensions(db)
            await db.commit()
            logger.info(
                "seed_medical_reference_complete",
                mock_dimensions_cleared=cleared_dims,
                mock_terms_cleared=cleared_terms,
                domains_created=domain_count,
                terms_created=term_count,
                dimensions_created=dim_count,
            )
        except Exception:
            await db.rollback()
            logger.exception("seed_failed")
            raise


if __name__ == "__main__":
    asyncio.run(run())
