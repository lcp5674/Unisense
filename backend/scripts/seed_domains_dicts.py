"""初始化 seed 脚本：预置标准主题域 + 10类字典项。

用法:
    poetry run python -m scripts.seed_domains_dicts

幂等：已存在的域/字典项跳过，不覆盖。
对齐 spec FR-012 / plan.md D5。
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
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.logging import configure_logging  # noqa: E402
from app.db.mysql import async_session_factory  # noqa: E402
from app.models.subject_domain import SubjectDomain  # noqa: E402
from app.models.system_dict import SystemDict  # noqa: E402

logger = structlog.get_logger("unisense.seed")

# ---- 标准主题域（医疗业务：HIS 门诊数据驱动的度量域）----
# 从电商演示（sales/finance/user/product/marketing/logistics）切换到医疗实际场景：
# 平台采集的是 HIS 门诊数据（dp元数据.csv：tj_cf_drug_prescription / tj_cf_diagnosis /
# tj_pharmacy_feebill_master 等），度量目录按门诊业务组织。
STANDARD_DOMAINS: list[dict[str, Any]] = [
    {"code": "outpatient", "name": "门诊", "level": 1, "sort_order": 1},
    {"code": "medication", "name": "药品", "level": 1, "sort_order": 2},
    {"code": "medical_fee", "name": "医疗费用", "level": 1, "sort_order": 3},
    {"code": "medical_insurance", "name": "医保", "level": 1, "sort_order": 4},
    {"code": "diagnosis", "name": "诊断", "level": 1, "sort_order": 5},
    {"code": "quality", "name": "质控", "level": 1, "sort_order": 6},
    {"code": "patient", "name": "患者", "level": 1, "sort_order": 7},
    {"code": "uncategorized", "name": "未分类", "level": 1, "sort_order": 99},
]

# ---- 10 类字典项 ----
DICT_SEEDS: dict[str, list[dict[str, Any]]] = {
    "granularity": [
        # 时间粒度
        {"code": "minute", "label": "分钟", "sort_order": 0},
        {"code": "hour", "label": "小时", "sort_order": 1},
        {"code": "day", "label": "天", "sort_order": 2},
        {"code": "week", "label": "周", "sort_order": 3},
        {"code": "month", "label": "月", "sort_order": 4},
        {"code": "quarter", "label": "季", "sort_order": 5},
        {"code": "year", "label": "年", "sort_order": 6},
        {"code": "realtime", "label": "实时", "sort_order": 7},
        # 业务实体粒度（医疗场景：指标按门诊业务主体统计）
        {"code": "register", "label": "挂号粒度", "sort_order": 20},
        {"code": "visit", "label": "就诊粒度", "sort_order": 21},
        {"code": "patient", "label": "患者粒度", "sort_order": 22},
        {"code": "doctor", "label": "医生粒度", "sort_order": 23},
        {"code": "department", "label": "科室粒度", "sort_order": 24},
        {"code": "disease", "label": "病种粒度", "sort_order": 25},
        {"code": "prescription", "label": "处方粒度", "sort_order": 26},
        {"code": "pharmacy", "label": "药房粒度", "sort_order": 27},
        {"code": "yb_settle", "label": "医保结算粒度", "sort_order": 28},
        # 医院（2026-08-28 组合粒度补：用户示例「按月+医院统计订单金额」——hospital
        # 是医疗最基础实体粒度，推断/注册/消费三侧均可识别）
        {"code": "hospital", "label": "医院粒度", "sort_order": 29},
    ],
    "unit": [
        {"code": "CNY", "label": "人民币元", "sort_order": 1},
        {"code": "USD", "label": "美元", "sort_order": 2},
        {"code": "EUR", "label": "欧元", "sort_order": 3},
        {"code": "CNY_WAN", "label": "万元", "sort_order": 4},
        {"code": "CNY_YI", "label": "亿元", "sort_order": 5},
        {"code": "PERCENT", "label": "百分比", "sort_order": 6},
        {"code": "ORDER", "label": "笔", "sort_order": 7},
        {"code": "TIMES", "label": "次", "sort_order": 8},
        {"code": "PERSON", "label": "人", "sort_order": 9},
        {"code": "DAY", "label": "天", "sort_order": 10},
        {"code": "HOUR", "label": "小时", "sort_order": 11},
        {"code": "MINUTE", "label": "分钟", "sort_order": 12},
    ],
    # 币种独立字典（ISO 4217 常用 + 万元/亿元）：币种字段从自由 Input 改为
    # 受控 Select 的标准依据。unit 字典中的 CNY/USD 等为历史语义重叠，保留不动。
    "currency": [
        {"code": "CNY", "label": "人民币", "sort_order": 1},
        {"code": "USD", "label": "美元", "sort_order": 2},
        {"code": "EUR", "label": "欧元", "sort_order": 3},
        {"code": "JPY", "label": "日元", "sort_order": 4},
        {"code": "HKD", "label": "港币", "sort_order": 5},
        {"code": "GBP", "label": "英镑", "sort_order": 6},
        {"code": "CNY_WAN", "label": "万元", "sort_order": 7},
        {"code": "CNY_YI", "label": "亿元", "sort_order": 8},
    ],
    "aggregation": [
        {"code": "SUM", "label": "求和", "sort_order": 1},
        {"code": "AVG", "label": "平均", "sort_order": 2},
        {"code": "COUNT", "label": "计数", "sort_order": 3},
        {"code": "COUNT_DISTINCT", "label": "去重计数", "sort_order": 4},
        {"code": "LAST_VALUE", "label": "末值", "sort_order": 5},
        {"code": "MAX", "label": "最大值", "sort_order": 6},
        {"code": "MIN", "label": "最小值", "sort_order": 7},
        {"code": "MEDIAN", "label": "中位数", "sort_order": 8},
        {"code": "PERCENTILE", "label": "分位数", "sort_order": 9},
    ],
    "time_semantics": [
        {"code": "PERIOD", "label": "期间", "sort_order": 1},
        {"code": "YTD", "label": "年初至今", "sort_order": 2},
        {"code": "TTM", "label": "滚动12月", "sort_order": 3},
        {"code": "AVG", "label": "均值", "sort_order": 4},
        {"code": "MOM", "label": "环比", "sort_order": 5},
        {"code": "YOY", "label": "同比", "sort_order": 6},
    ],
    "freshness": [
        {"code": "REALTIME", "label": "实时", "sort_order": 1},
        {"code": "T0", "label": "T+0", "sort_order": 2},
        {"code": "T1", "label": "T+1", "sort_order": 3},
        {"code": "HOURLY", "label": "小时级", "sort_order": 4},
    ],
    "dw_layer": [
        {"code": "ODS", "label": "原始层", "sort_order": 1},
        {"code": "DWD", "label": "明细层", "sort_order": 2},
        {"code": "DWS", "label": "汇总层", "sort_order": 3},
        {"code": "ADS", "label": "应用层", "sort_order": 4},
        {"code": "DM", "label": "域模型层", "sort_order": 5},
    ],
    "metric_type": [
        {"code": "atomic", "label": "原子", "sort_order": 1},
        {"code": "derived", "label": "衍生", "sort_order": 2},
        {"code": "composite", "label": "复合", "sort_order": 3},
    ],
    "additivity": [
        {"code": "ADDITIVE", "label": "可加", "sort_order": 1},
        {"code": "SEMI_ADDITIVE", "label": "半可加", "sort_order": 2},
        {"code": "NON_ADDITIVE", "label": "不可加", "sort_order": 3},
    ],
    "serving_mode": [
        {"code": "BATCH_ONLY", "label": "仅批", "sort_order": 1},
        {"code": "REALTIME_ONLY", "label": "仅流", "sort_order": 2},
        {"code": "BATCH_REALTIME_DUAL", "label": "批流双路", "sort_order": 3},
    ],
    "metric_tier": [
        {"code": "T1", "label": "核心", "sort_order": 1},
        {"code": "T2", "label": "重要", "sort_order": 2},
        {"code": "T3", "label": "一般", "sort_order": 3},
    ],
    # PII 复核字段类型（对齐采集规则 DEFAULT_PII_RULES）：治理页复核标注的敏感
    # 字段类型字典化——后端新增 PII 规则时前端下拉无需发版（2026-08-28）。
    "pii_field_type": [
        {"code": "user_phone", "label": "手机号", "sort_order": 1},
        {"code": "id_card", "label": "身份证号", "sort_order": 2},
        {"code": "email", "label": "邮箱", "sort_order": 3},
        {"code": "bank_card", "label": "银行卡号", "sort_order": 4},
        {"code": "real_name", "label": "真实姓名", "sort_order": 5},
        {"code": "address", "label": "住址", "sort_order": 6},
        {"code": "passport", "label": "护照号", "sort_order": 7},
        {"code": "gps", "label": "定位/GPS", "sort_order": 8},
    ],
    # 合规法律依据（受控词表，GDPR 合法基础）：资产保留策略/合规标注的选择依据
    # 字典化——口径调整时前端无需发版（2026-08-28）。
    "legal_basis": [
        {"code": "user_consent", "label": "用户同意", "sort_order": 1},
        {"code": "contract", "label": "合同必需", "sort_order": 2},
        {"code": "law", "label": "法定职责", "sort_order": 3},
        {"code": "legitimate_interest", "label": "正当利益", "sort_order": 4},
        {"code": "public_interest", "label": "公共利益", "sort_order": 5},
    ],
}

# admin 用户 ID（seed_admin.py 创建的默认管理员）
DEFAULT_ADMIN_ID = 1


async def seed_domains(db: AsyncSession, owner_id: int = DEFAULT_ADMIN_ID) -> int:
    """预置标准主题域，返回新增数。

    Args:
        db: 复用调用方会话。
        owner_id: 域责任人。默认 ``DEFAULT_ADMIN_ID``（CLI 行为不变）；部署自举传
            入 ``seed_admin`` 实际返回的 admin id，避免 admin 自增 id 非 1 时落错人。
    """
    created = 0
    for d in STANDARD_DOMAINS:
        stmt = select(SubjectDomain).where(
            SubjectDomain.code == d["code"],
            SubjectDomain.deleted_at.is_(None),
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()
        if existing:
            logger.debug("domain_exists_skip", code=d["code"])
            continue
        domain = SubjectDomain(
            code=d["code"],
            name=d["name"],
            parent_id=None,
            level=d["level"],
            path=None,  # flush 后更新
            sort_order=d["sort_order"],
            status="active",
            defaults_json={},
            description=f"标准主题域: {d['name']}",
            owner_id=owner_id,
        )
        db.add(domain)
        await db.flush()
        # 更新 path
        domain.path = str(domain.id)
        await db.flush()
        created += 1
        logger.info("domain_created", code=d["code"], id=domain.id)
    return created


async def seed_dicts(db: AsyncSession) -> int:
    """预置10类字典项，返回新增数。"""
    created = 0
    for dict_type, items in DICT_SEEDS.items():
        for item in items:
            stmt = select(SystemDict).where(
                SystemDict.dict_type == dict_type,
                SystemDict.code == item["code"],
                SystemDict.deleted_at.is_(None),
            )
            existing = (await db.execute(stmt)).scalar_one_or_none()
            if existing:
                logger.debug("dict_item_exists_skip", dict_type=dict_type, code=item["code"])
                continue
            entry = SystemDict(
                dict_type=dict_type,
                code=item["code"],
                label=item["label"],
                sort_order=item["sort_order"],
                status="active",
                description=None,
            )
            db.add(entry)
            created += 1
            logger.info("dict_item_created", dict_type=dict_type, code=item["code"])
    await db.flush()
    return created


async def migrate_existing_domains(db: AsyncSession) -> int:
    """迁移存量 Metric 的 domain 字段：匹配 SubjectDomain.code，不匹配归入 uncategorized。"""
    from app.models.metric import Metric

    stmt = (
        select(Metric.domain)
        .where(
            Metric.deleted_at.is_(None),
        )
        .distinct()
    )
    result = await db.execute(stmt)
    existing_domains = [row[0] for row in result.all() if row[0]]

    # 获取所有域 code
    domain_stmt = select(SubjectDomain.code).where(
        SubjectDomain.deleted_at.is_(None),
        SubjectDomain.status == "active",
    )
    domain_result = await db.execute(domain_stmt)
    valid_codes = {row[0] for row in domain_result.all()}

    migrated = 0
    for domain_val in existing_domains:
        if domain_val not in valid_codes:
            # 归入 uncategorized
            update_stmt = (
                Metric.__table__.update()  # type: ignore[attr-defined]  # sqlalchemy 桩将 __table__ 标为 FromClause，实为 Table，.update() 合法
                .where(Metric.__table__.c.domain == domain_val)
                .where(Metric.__table__.c.deleted_at.is_(None))
                .values(domain="uncategorized")
            )
            result = await db.execute(update_stmt)
            migrated += result.rowcount
            logger.info("domain_migrated_to_uncategorized", old=domain_val, count=result.rowcount)

    await db.flush()
    return migrated


async def run() -> None:
    """执行 seed。"""
    configure_logging()
    async with async_session_factory() as db:
        try:
            domain_count = await seed_domains(db)
            dict_count = await seed_dicts(db)
            migrated_count = await migrate_existing_domains(db)
            await db.commit()
            logger.info(
                "seed_complete",
                domains_created=domain_count,
                dict_items_created=dict_count,
                metrics_migrated=migrated_count,
            )
        except Exception:
            await db.rollback()
            logger.exception("seed_failed")
            raise


if __name__ == "__main__":
    asyncio.run(run())
