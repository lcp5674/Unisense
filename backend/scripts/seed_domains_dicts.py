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

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import configure_logging
from app.db.mysql import async_session_factory
from app.models.subject_domain import SubjectDomain
from app.models.system_dict import SystemDict

logger = structlog.get_logger("unisense.seed")

# ---- 标准主题域 ----
STANDARD_DOMAINS: list[dict[str, Any]] = [
    {"code": "sales", "name": "销售", "level": 1, "sort_order": 1},
    {"code": "finance", "name": "财务", "level": 1, "sort_order": 2},
    {"code": "user", "name": "用户", "level": 1, "sort_order": 3},
    {"code": "product", "name": "产品", "level": 1, "sort_order": 4},
    {"code": "marketing", "name": "营销", "level": 1, "sort_order": 5},
    {"code": "logistics", "name": "物流", "level": 1, "sort_order": 6},
    {"code": "uncategorized", "name": "未分类", "level": 1, "sort_order": 99},
]

# ---- 10 类字典项 ----
DICT_SEEDS: dict[str, list[dict[str, Any]]] = {
    "granularity": [
        {"code": "hour", "label": "小时", "sort_order": 1},
        {"code": "day", "label": "天", "sort_order": 2},
        {"code": "week", "label": "周", "sort_order": 3},
        {"code": "month", "label": "月", "sort_order": 4},
        {"code": "quarter", "label": "季", "sort_order": 5},
        {"code": "year", "label": "年", "sort_order": 6},
    ],
    "unit": [
        {"code": "CNY", "label": "人民币元", "sort_order": 1},
        {"code": "USD", "label": "美元", "sort_order": 2},
        {"code": "EUR", "label": "欧元", "sort_order": 3},
        {"code": "cnt", "label": "个数", "sort_order": 4},
        {"code": "ratio", "label": "比率", "sort_order": 5},
        {"code": "percent", "label": "百分比", "sort_order": 6},
        {"code": "KWH", "label": "千瓦时", "sort_order": 7},
        {"code": "GB", "label": "吉字节", "sort_order": 8},
        {"code": "TB", "label": "太字节", "sort_order": 9},
        {"code": "MB", "label": "兆字节", "sort_order": 10},
    ],
    "aggregation": [
        {"code": "SUM", "label": "求和", "sort_order": 1},
        {"code": "AVG", "label": "平均", "sort_order": 2},
        {"code": "COUNT", "label": "计数", "sort_order": 3},
        {"code": "COUNT_DISTINCT", "label": "去重计数", "sort_order": 4},
        {"code": "LAST_VALUE", "label": "末值", "sort_order": 5},
    ],
    "time_semantics": [
        {"code": "PERIOD", "label": "期间", "sort_order": 1},
        {"code": "YTD", "label": "年初至今", "sort_order": 2},
        {"code": "TTM", "label": "滚动12月", "sort_order": 3},
        {"code": "AVG", "label": "均值", "sort_order": 4},
    ],
    "freshness": [
        {"code": "REALTIME", "label": "实时", "sort_order": 1},
        {"code": "T1", "label": "T+1", "sort_order": 2},
        {"code": "HOURLY", "label": "小时级", "sort_order": 3},
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
}

# admin 用户 ID（seed_admin.py 创建的默认管理员）
DEFAULT_ADMIN_ID = 1


async def seed_domains(db: AsyncSession) -> int:
    """预置标准主题域，返回新增数。"""
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
            owner_id=DEFAULT_ADMIN_ID,
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

    stmt = select(Metric.domain).where(
        Metric.deleted_at.is_(None),
    ).distinct()
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
