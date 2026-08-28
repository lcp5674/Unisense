"""敏感规则 DB 可配置加载（PII 合规增强 C-1 + 可视化配置台 A）。

规则引擎默认内置 ``DEFAULT_PII_RULES`` / ``DEFAULT_CONFIDENTIAL_RULES``；本模块从
``system_dict``（dict_type=``pii_rule``，description 存
``{category, name_re, sample_re, confidence, pii}`` JSON）读取**用户配置**，并按
``rule_id`` **覆盖同 ID 内置规则、其余回退内置**（自定义规则不会吞掉内置规则）：

- active 的 DB 项：同 ``rule_id`` 覆盖内置；内置无此 ID 视为新增自定义规则；
- inactive 的 DB 项：对应规则整体停用（从生效集剔除，内置回退亦不生效）；
- 无 DB 项的内置规则：保持内置默认。

``pii=false`` 的规则归入机密规则集（密码/税务/商业敏感等），与内置机密规则合并，
补齐既有「机密规则不可 DB 配置」的缺口。

读取失败时回退内置默认（fail-safe：宁可沿用内置规则也不让采集因配置损坏而中断）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.system_dict import SystemDict
from app.services.collector import classifier
from app.services.collector.classifier import (
    DEFAULT_CONFIDENTIAL_RULES,
    DEFAULT_PII_RULES,
    ConfidentialCategory,
    PiiCategory,
    PiiRule,
    PiiVocab,
)

logger = get_logger("unisense.collector.rules")


def _resolve_category(category: str, pii: bool) -> str:
    """解析类别（先 PII 类别、后机密类别）；无法识别时按 pii 归默认类别。"""
    try:
        return PiiCategory(category).value
    except ValueError:
        pass
    try:
        return ConfidentialCategory(category).value
    except ValueError:
        return (PiiCategory.NAME if pii else ConfidentialCategory.CREDENTIAL).value


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
    pii = bool(cfg.get("pii", True))
    category = str(cfg.get("category") or ("PII" if pii else "CREDENTIAL")).upper()
    return PiiRule(
        category=_resolve_category(category, pii),
        rule_id=str(item.code),
        name_re=name_re,
        sample_re=sample_re if isinstance(sample_re, str) else None,
        confidence=min(max(confidence, 0.0), 1.0),
        pii=pii,
    )


def _merge_group(
    active: dict[str, PiiRule],
    disabled: set[str],
    builtin: Sequence[PiiRule],
    pii: bool,
) -> tuple[PiiRule, ...]:
    """合并一组规则：内置保留 + 未被停用的 DB 项按 rule_id 覆盖；自定义追加。"""
    merged: list[PiiRule] = []
    for r in builtin:
        if r.rule_id in disabled:
            continue  # 该规则被停用，整体失效
        merged.append(active.pop(r.rule_id, r))
    for _rule_id, r in active.items():
        if r.pii is pii:
            merged.append(r)
    return tuple(merged)


def merge_effective_rules(
    rows: Sequence[SystemDict],
) -> tuple[tuple[PiiRule, ...], tuple[PiiRule, ...]]:
    """按覆盖语义合并 DB 项与内置规则，产出生效的 PII/机密规则集。

    Args:
        rows: ``pii_rule`` 类型的字典项（含 inactive；由调用方负责软删除过滤）。

    Returns:
        ``(pii_rules, confidential_rules)``，两集合均非空（至少含内置默认）。
    """
    active: dict[str, PiiRule] = {}
    disabled: set[str] = set()
    for item in rows:
        if item.status != "active":
            disabled.add(str(item.code))
            continue
        rule = _parse_rule(item)
        if rule is not None:
            active[str(item.code)] = rule
    pii_active = {k: v for k, v in active.items() if v.pii}
    conf_active = {k: v for k, v in active.items() if not v.pii}
    pii_rules = _merge_group(pii_active, disabled, DEFAULT_PII_RULES, pii=True)
    conf_rules = _merge_group(conf_active, disabled, DEFAULT_CONFIDENTIAL_RULES, pii=False)
    return pii_rules, conf_rules


async def load_pii_rules(
    session: AsyncSession,
) -> tuple[Sequence[PiiRule], Sequence[PiiRule]]:
    """从 system_dict 读取敏感规则并合并内置，产出生效规则集。

    Returns:
        ``(pii_rules, confidential_rules)``——合并后**完整生效集**（非 None，
        至少含内置默认）；读取失败时回退内置，保证采集不中断。
    """
    try:
        rows = (
            await session.execute(
                select(SystemDict).where(
                    SystemDict.dict_type == "pii_rule",
                    SystemDict.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    except Exception as exc:  # noqa: BLE001 - 配置读取失败不阻断采集
        logger.warning("pii_rule_load_failed: %s", exc)
        return DEFAULT_PII_RULES, DEFAULT_CONFIDENTIAL_RULES
    if not rows:
        return DEFAULT_PII_RULES, DEFAULT_CONFIDENTIAL_RULES
    return merge_effective_rules(rows)


def rule_label(rule: PiiRule) -> str:
    """规则显示名：内置走标签映射，自定义回退 rule_id。"""
    labels = _LABEL_BY_RULE_ID if rule.pii else _CONF_LABEL_BY_RULE_ID
    return labels.get(rule.rule_id, str(rule.rule_id))


#: 内置规则中文名映射（对齐 0067 种子 / classifier 默认规则集）。
_LABEL_BY_RULE_ID: dict[str, str] = {
    "id_card": "身份证号规则",
    "phone": "手机号规则",
    "email": "邮箱规则",
    "real_name": "姓名规则",
    "address": "地址规则",
    "bank_card": "银行卡规则",
    "id_no": "证件号规则",
    "passport": "护照规则",
    "gps": "定位规则",
    "health": "健康规则",
    "biometric": "生物特征规则",
    "financial": "金融规则",
}
_CONF_LABEL_BY_RULE_ID: dict[str, str] = {
    "password": "密码/密钥规则",
    "tax": "税务/发票规则",
    "business": "商业敏感规则",
}


# ---- PII 上下文词表（pii_vocab 字典，可配置覆盖内置）----


#: pii_vocab 词表键 → 内置正则源（供配置台展示默认值/恢复默认）。
VOCAB_DEFAULT_SRC: dict[str, str] = {
    "person_name_re": classifier._PERSON_NAME_SRC,
    "entity_name_re": classifier._ENTITY_NAME_SRC,
    "person_entity_re": classifier._PERSON_ENTITY_SRC,
    "entity_entity_re": classifier._ENTITY_ENTITY_SRC,
    "health_org_re": classifier._HEALTH_ORG_SRC,
    "health_keep_re": classifier._HEALTH_KEEP_SRC,
    "aggregate_re": classifier._AGGREGATE_SRC,
}

#: pii_vocab 词表键 → 内置词条（逗号分隔列表，供配置台展示/恢复）。
VOCAB_DEFAULT_WORDS: dict[str, str] = {
    "value_exempt_prefix": "heart_rate,heartrate,心率",
}


def merge_vocab(raw: dict[str, str]) -> PiiVocab:
    """按字典项覆盖内置词表，产出生效 PiiVocab。

    Args:
        raw: ``pii_vocab`` 字典项映射（code → description）。
            正则类键整体作正则；词条类键按逗号/换行分隔为词列表。
            空/非法项回退内置默认（fail-safe，配置损坏不阻断采集）。
    """
    base = PiiVocab()

    def _re(key: str, default: str) -> str:
        s = raw.get(key, "").strip()
        return s or default

    def _words(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        s = raw.get(key, "").strip()
        if not s:
            return default
        return tuple(p.strip() for p in s.replace("\n", ",").split(",") if p.strip())

    return PiiVocab(
        person_name_re=_re("person_name_re", base.person_name_re),
        entity_name_re=_re("entity_name_re", base.entity_name_re),
        person_entity_re=_re("person_entity_re", base.person_entity_re),
        entity_entity_re=_re("entity_entity_re", base.entity_entity_re),
        health_org_re=_re("health_org_re", base.health_org_re),
        health_keep_re=_re("health_keep_re", base.health_keep_re),
        aggregate_re=_re("aggregate_re", base.aggregate_re),
        value_exempt_prefixes=_words("value_exempt_prefix", base.value_exempt_prefixes),
        exempt_fields=frozenset(_words("exempt_field", ())),
        exempt_prefixes=_words("exempt_prefix", ()),
    )


async def load_pii_vocab(session: AsyncSession) -> PiiVocab:
    """从 system_dict 读取 PII 上下文词表（dict_type=pii_vocab）并合并内置。

    Returns:
        生效词表（无 DB 项/读取失败时回退内置默认，采集不中断）。
    """
    try:
        rows = (
            await session.execute(
                select(SystemDict).where(
                    SystemDict.dict_type == "pii_vocab",
                    SystemDict.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    except Exception as exc:  # noqa: BLE001 - 词表读取失败不阻断采集
        logger.warning("pii_vocab_load_failed: %s", exc)
        return PiiVocab()
    raw: dict[str, str] = {}
    for item in rows:
        desc = (item.description or "").strip()
        if item.status == "active" and desc:
            code = str(item.code)
            # 同 code 多行合并（如 exempt_field 多条追加，逗号连接）
            raw[code] = f"{raw[code]},{desc}" if code in raw else desc
    if not raw:
        return PiiVocab()
    return merge_vocab(raw)
