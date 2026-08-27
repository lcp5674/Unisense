"""初始化 seed 脚本：清除 E2E/测试参照数据，灌入微医业务参照数据。

以微医实际业务七大主线组织参照数据：医疗（诊疗）、医药（药品）、医保、
挂号、健康管理、健共体（数字健共体·区域医疗协作）、医生管理（供给侧核心
资产）。覆盖三类主数据（对齐 TD §12.14/§12.15 / FR-05 / FR-08 / FR-012）：
- 主题域（subject_domain）：保留既有 7 医疗域 + uncategorized 及微医线上业务
  一级域（在线问诊/互联网医院/预约挂号/健康管理），新增「健共体」「医生管理」
  一级域及子域，并为各业务主线补全缺失二级子域（处方流转/支付方式改革/
  企业健康/家庭病床）。
- 术语（term）：清除 8 条 E2E/测试术语及关联（term_version/term_relation/
  glossary_conflict），灌入七大业务主线核心术语（含健共体协作与医生层面术语）。
- 维度（dimension）：清除 8 条 E2E/测试维度及引用（dimension_member/
  metric_dimension/reconciliation/dimension_mapping），灌入七大业务主线
  维度+成员（含健共体转诊/医联体/签约、医生职称/医疗机构等维度）。维度已
  存在但成员集合与脚本不一致时自动刷新（删旧重灌），保证参照数据与脚本同步。

用法:
    poetry run python -m scripts.seed_medical_reference

幂等：清除按 E2E/测试编码前缀匹配（已删则跳过），灌入按唯一编码查存在则跳过。
不动 seed_e2e_data.py（E2E 测试跑时自愈重建自己的维度/术语）。
"""

from __future__ import annotations

import asyncio
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
]


# ---------------------------------------------------------------------------
# 术语（七大业务主线核心术语，61 条，全 PUBLISHED 直灌）
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
]


# ---------------------------------------------------------------------------
# 维度 + 成员（七大业务主线，23 个维度；SCD0/SCD1/SCD2 三型）
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
                        parent_code=None,
                        path=None,
                        attributes=m.get("attributes"),
                        status="PUBLISHED",
                    )
                )
            created += 1
            logger.info("dimension_created", dim_code=dim_code, members=len(spec["members"]))
            continue
        # 已存在：成员集合不一致则刷新（删旧重灌），保证与脚本定义一致
        current = set(
            (
                await db.execute(
                    select(DimensionMember.member_code).where(
                        DimensionMember.dim_code == dim_code,
                        DimensionMember.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        expected = {m["code"] for m in spec["members"]}
        if current == expected:
            continue
        await db.execute(delete(DimensionMember).where(DimensionMember.dim_code == dim_code))
        for m in spec["members"]:
            db.add(
                DimensionMember(
                    dim_code=dim_code,
                    member_code=m["code"],
                    member_name=m["name"],
                    parent_code=None,
                    path=None,
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
