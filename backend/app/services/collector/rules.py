"""敏感规则 DB 可配置加载（PII 合规增强 C-1）。

规则引擎默认内置 ``DEFAULT_PII_RULES``；本模块从 ``system_dict``（dict_type=
``pii_rule``，description 存 ``{category, name_re, sample_re, confidence}`` JSON）
读取**用户自定义规则**，覆盖内置默认——满足「规则/类别可配置」生产诉求。

读取失败或系统字典中无 ``pii_rule`` 项时返回 ``(None, None)``，调用方回退内置
默认（fail-safe：宁可沿用内置规则也不让采集因配置损坏而中断）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.system_dict import SystemDict
from app.services.collector.classifier import PiiCategory, PiiRule

logger = get_logger("unisense.collector.rules")


def _parse_rule(item: SystemDict) -> PiiRule | None:
    """解析字典项 description JSON 为规则；格式非法返回 None（跳过该条）。"""
    raw = (item.description or "").strip()
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("pii_rule_config_invalid_json: code=%s", item.code)
        return None
    name_re = cfg.get("name_re")
    if not isinstance(name_re, str) or not name_re:
        return None
    sample_re = cfg.get("sample_re")
    confidence = float(cfg.get("confidence") or 0.7)
    category = str(cfg.get("category") or "PII").upper()
    # 合法类别校验（无法识别时归入通用 PII 类别）
    try:
        cat = PiiCategory(category)
    except ValueError:
        cat = PiiCategory.NAME
    return PiiRule(
        category=cat,
        rule_id=str(item.code),
        name_re=name_re,
        sample_re=sample_re if isinstance(sample_re, str) else None,
        confidence=min(max(confidence, 0.0), 1.0),
    )


async def load_pii_rules(
    session: AsyncSession,
) -> tuple[Sequence[PiiRule] | None, Sequence[PiiRule] | None]:
    """从 system_dict 读取 PII 规则配置。

    Returns:
        ``(pii_rules, confidential_rules)``；系统字典无 ``pii_rule`` 项或读取失败
        返回 ``(None, None)``（调用方回退内置默认）。
    """
    try:
        rows = (
            await session.execute(
                select(SystemDict).where(
                    SystemDict.dict_type == "pii_rule",
                    SystemDict.status == "active",
                    SystemDict.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    except Exception as exc:  # noqa: BLE001 - 配置读取失败不阻断采集
        logger.warning("pii_rule_load_failed: %s", exc)
        return None, None
    if not rows:
        return None, None
    parsed: list[PiiRule] = []
    for item in rows:
        rule = _parse_rule(item)
        if rule is not None:
            parsed.append(rule)
    if not parsed:
        return None, None
    return tuple(parsed), None
