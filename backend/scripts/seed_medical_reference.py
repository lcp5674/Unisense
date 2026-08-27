"""初始化 seed 脚本：清除 E2E/测试参照数据，灌入微医业务参照数据。

覆盖三类主数据（对齐 TD §12.14/§12.15 / FR-05 / FR-08 / FR-012）：
- 主题域（subject_domain）：保留既有 7 医疗域 + uncategorized，补充微医线上业务
  一级域（在线问诊/互联网医院/预约挂号/健康管理）并为全部一级域补二级子域。
- 术语（term）：清除 8 条 E2E/测试术语及关联（term_version/term_relation/
  glossary_conflict），灌入微医核心业务术语。
- 维度（dimension）：清除 8 条 E2E/测试维度及引用（dimension_member/
  metric_dimension/reconciliation/dimension_mapping），灌入医疗业务维度+成员。

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
# 主题域（微医业务：7 医疗域 + 4 线上业务域，均含二级子域）
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
]


# ---------------------------------------------------------------------------
# 术语（微医核心业务，24 条，全 PUBLISHED 直灌）
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
]


# ---------------------------------------------------------------------------
# 维度 + 成员（医疗业务，12 个维度；SCD0/SCD1/SCD2 三型）
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
    """医生维度成员：代表医生，attributes 含科室/职称（SCD2 跟踪职称变化）。"""
    doctors = [
        ("张伟", "neike", "主任医师"),
        ("李娜", "neike", "副主任医师"),
        ("王强", "waike", "主任医师"),
        ("赵敏", "fuchanke", "副主任医师"),
        ("刘洋", "erke", "主治医师"),
        ("陈静", "zhongyike", "副主任医师"),
        ("杨光", "xinxueguanneike", "主治医师"),
        ("周婷", "huxineike", "住院医师"),
    ]
    return [
        {"code": f"doc_{i + 1:02d}", "name": name, "attributes": {"dept_code": dept, "title": title}}
        for i, (name, dept, title) in enumerate(doctors)
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
    """灌入医疗业务维度及成员（PUBLISHED 直灌），返回新增维度数。"""
    created = 0
    existing = {
        row.dim_code
        for row in (
            await db.execute(select(Dimension.dim_code).where(Dimension.deleted_at.is_(None)))
        ).all()
    }
    for spec in DIMENSION_SEEDS:
        if spec["dim_code"] in existing:
            continue
        dim = Dimension(
            dim_code=spec["dim_code"],
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
        logger.info("dimension_created", dim_code=spec["dim_code"], members=len(spec["members"]))
    await db.flush()
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
