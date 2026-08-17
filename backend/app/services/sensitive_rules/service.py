"""敏感规则配置台业务逻辑（方案 A：规则引擎可视化配置）。

能力：
- 规则列表：内置 + DB 自定义**按 rule_id 合并**（DB 覆盖同 ID 内置，其余回退内置），
  每行标注来源（builtin/custom）与启用状态——管理员可直观看到生效规则全集；
- 结构化创建 / 更新 / 启停 / 删除（落 ``system_dict.pii_rule``）；
- 正则合法性校验（保存前即时反馈，防写坏正则导致采集误判）；
- 规则测试台：用**当前生效规则**模拟识别，先验证再上生产；
- 类别目录（PII 12 类 + 机密 3 类）供类别下拉。

写操作统一走 ``SystemDictService``（复用其唯一性/软删恢复/停用语义），避免重复实现。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.collector.classifier import (
    CONFIDENTIAL_CATEGORY_LABELS,
    DEFAULT_CONFIDENTIAL_RULES,
    DEFAULT_PII_RULES,
    PII_CATEGORY_LABELS,
    PiiRule,
    SensitivityClassifier,
)
from app.services.collector.rules import _parse_rule, load_pii_rules, rule_label
from app.services.sensitive_rules.schemas import (
    CategoryItem,
    RegexCheckResponse,
    RuleTestHit,
    RuleTestRequest,
    RuleTestResponse,
    SensitiveRuleCreate,
    SensitiveRuleItem,
    SensitiveRuleUpsert,
)
from app.services.system_dict.repository import SystemDictRepository
from app.services.system_dict.schemas import DictItemCreate, DictItemUpdate
from app.services.system_dict.service import SystemDictService


def _desc_json(data: SensitiveRuleUpsert) -> str:
    """规则配置 JSON（description 列，规则引擎解析的存储格式）。"""
    return json.dumps(
        {
            "category": data.category,
            "name_re": data.name_re,
            "sample_re": data.sample_re,
            "confidence": data.confidence,
            "pii": data.pii,
        },
        ensure_ascii=False,
    )


def _desc_json_from_rule(rule: PiiRule) -> str:
    return json.dumps(
        {
            "category": rule.category,
            "name_re": rule.name_re,
            "sample_re": rule.sample_re,
            "confidence": rule.confidence,
            "pii": rule.pii,
        },
        ensure_ascii=False,
    )


def _category_label(category: str, pii: bool) -> str:
    table = PII_CATEGORY_LABELS if pii else CONFIDENTIAL_CATEGORY_LABELS
    return table.get(category, category)


def _item_from_rule(
    rule: PiiRule,
    *,
    label: str | None,
    source: str,
    status: str,
    updated_at: datetime | None = None,
) -> SensitiveRuleItem:
    return SensitiveRuleItem(
        rule_id=rule.rule_id,
        label=label or rule_label(rule),
        category=rule.category,
        category_label=_category_label(rule.category, rule.pii),
        name_re=rule.name_re,
        sample_re=rule.sample_re,
        confidence=rule.confidence,
        pii=rule.pii,
        source=source,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        updated_at=updated_at,
    )


class SensitiveRuleService:
    """敏感规则配置台服务。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repo = SystemDictRepository(db)
        self._dict_svc = SystemDictService(db)

    # ------------------------------------------------------------ 读
    async def list_rules(self) -> list[SensitiveRuleItem]:
        """合并展示规则列表：内置 + DB 覆盖（含 inactive），按来源顺序。"""
        rows = await self._repo.list_by_type("pii_rule", status=None)
        db_by_id: dict[str, Any] = {r.code: r for r in rows}
        items: list[SensitiveRuleItem] = []
        builtins = [*DEFAULT_PII_RULES, *DEFAULT_CONFIDENTIAL_RULES]
        for rule in builtins:
            item = db_by_id.pop(rule.rule_id, None)
            if item is None:
                items.append(
                    _item_from_rule(rule, label=None, source="builtin", status="active")
                )
                continue
            parsed = _parse_rule(item)
            if parsed is not None:
                items.append(
                    _item_from_rule(
                        parsed,
                        label=item.label,
                        source="custom",
                        status=item.status,
                        updated_at=item.updated_at,
                    )
                )
            else:
                # DB 配置损坏（JSON 非法）：字段回退内置，来源仍标 custom（被覆盖过）
                items.append(
                    _item_from_rule(
                        rule,
                        label=item.label,
                        source="custom",
                        status=item.status,
                        updated_at=item.updated_at,
                    )
                )
        for _code, item in db_by_id.items():
            parsed = _parse_rule(item)
            if parsed is None:
                continue
            items.append(
                _item_from_rule(
                    parsed,
                    label=item.label,
                    source="custom",
                    status=item.status,
                    updated_at=item.updated_at,
                )
            )
        return items

    async def get_rule(self, rule_id: str) -> SensitiveRuleItem:
        for item in await self.list_rules():
            if item.rule_id == rule_id:
                return item
        raise NotFoundError(f"敏感规则不存在: {rule_id}")

    def list_categories(self) -> list[CategoryItem]:
        """类别目录（PII 12 类 + 机密 3 类）。"""
        out = [
            CategoryItem(category=cat, label=label, pii=True)
            for cat, label in PII_CATEGORY_LABELS.items()
        ]
        out.extend(
            CategoryItem(category=cat, label=label, pii=False)
            for cat, label in CONFIDENTIAL_CATEGORY_LABELS.items()
        )
        return out

    def validate_regex(self, pattern: str) -> RegexCheckResponse:
        """正则合法性校验（Python re 语法）。"""
        try:
            re.compile(pattern)
            return RegexCheckResponse(valid=True)
        except re.error as exc:
            return RegexCheckResponse(valid=False, error=str(exc))

    async def test_rule(self, payload: RuleTestRequest) -> RuleTestResponse:
        """规则测试台：用当前生效规则（DB 合并内置）模拟识别一条字段。"""
        pii_rules, conf_rules = await load_pii_rules(self._db)
        classifier = SensitivityClassifier(rules=pii_rules, confidential_rules=conf_rules)
        schema = {
            "columns": [
                {
                    "name": payload.column_name,
                    "sample": payload.sample_value or "",
                    "comment": payload.comment or "",
                }
            ]
        }
        hits = classifier.detect_pii_fields(payload.entity_name, schema)
        level = classifier.classify(payload.entity_name, schema, hits=hits)
        return RuleTestResponse(
            sensitivity_level=level,
            hits=[
                RuleTestHit(
                    column=h.column,
                    category=h.category,
                    category_label=_category_label(h.category, h.pii),
                    rule=h.rule,
                    confidence=h.confidence,
                    matched_by=h.matched_by,
                    pii=h.pii,
                )
                for h in hits
            ],
        )

    # ------------------------------------------------------------ 写
    async def create_rule(self, data: SensitiveRuleCreate) -> SensitiveRuleItem:
        """新增自定义规则（rule_id 缺省由 label 自动生成英文编码）。"""
        self._validate_category(data.category, data.pii)
        if data.rule_id:
            existing = await self._repo.get_item("pii_rule", data.rule_id)
            if existing is not None:
                raise ConflictError(
                    f"规则标识已存在: {data.rule_id}",
                    error_code="DUPLICATE_RULE_ID",
                )
        item = await self._dict_svc.create_item(
            "pii_rule",
            DictItemCreate(
                code=data.rule_id,
                label=data.label,
                sort_order=await self._next_sort_order(),
                description=_desc_json(data),
            ),
        )
        return _item_from_rule(
            _parse_rule(item) or self._fallback_rule(item.code, data),
            label=item.label,
            source="custom",
            status="active",
            updated_at=item.updated_at,
        )

    async def update_rule(self, rule_id: str, data: SensitiveRuleUpsert) -> SensitiveRuleItem:
        """更新规则：有 DB 项则更新；无则创建（内置规则首次被覆盖时落库）。"""
        self._validate_category(data.category, data.pii)
        existing = await self._repo.get_item("pii_rule", rule_id)
        if existing is None:
            item = await self._dict_svc.create_item(
                "pii_rule",
                DictItemCreate(
                    code=rule_id,
                    label=data.label,
                    sort_order=await self._next_sort_order(),
                    description=_desc_json(data),
                ),
            )
        else:
            item = await self._dict_svc.update_item(
                "pii_rule",
                rule_id,
                DictItemUpdate(label=data.label, description=_desc_json(data)),
            )
        return _item_from_rule(
            _parse_rule(item) or self._fallback_rule(rule_id, data),
            label=item.label,
            source="custom",
            status=item.status,
            updated_at=item.updated_at,
        )

    async def set_status(self, rule_id: str, action: str) -> SensitiveRuleItem:
        """启用 / 停用规则。内置规则无 DB 项时先落库（保留当前内置配置）再改状态。"""
        existing = await self._repo.get_item("pii_rule", rule_id)
        if existing is None:
            builtin = self._find_builtin(rule_id)
            if builtin is None:
                raise NotFoundError(f"敏感规则不存在: {rule_id}")
            await self._dict_svc.create_item(
                "pii_rule",
                DictItemCreate(
                    code=rule_id,
                    label=rule_label(builtin),
                    sort_order=await self._next_sort_order(),
                    description=_desc_json_from_rule(builtin),
                ),
            )
        if action == "activate":
            item = await self._dict_svc.activate_item("pii_rule", rule_id)
        else:
            item = await self._dict_svc.deactivate_item("pii_rule", rule_id)
        parsed = _parse_rule(item)
        if parsed is not None:
            return _item_from_rule(
                parsed,
                label=item.label,
                source="custom",
                status=item.status,
                updated_at=item.updated_at,
            )
        builtin = self._find_builtin(rule_id)
        return _item_from_rule(
            builtin or self._fallback_rule(rule_id, None),
            label=item.label,
            source="custom",
            status=item.status,
            updated_at=item.updated_at,
        )

    async def delete_rule(self, rule_id: str) -> None:
        """删除规则配置（回退内置默认）；无 DB 项时 404。"""
        await self._dict_svc.delete_item("pii_rule", rule_id)

    # ------------------------------------------------------------ 内部
    def _validate_category(self, category: str, pii: bool) -> None:
        table = PII_CATEGORY_LABELS if pii else CONFIDENTIAL_CATEGORY_LABELS
        if category not in table:
            raise ValidationError(
                f"非法类别: {category}（pii={pii} 允许: {', '.join(table)}）",
                error_code="INVALID_CATEGORY",
            )

    def _find_builtin(self, rule_id: str) -> PiiRule | None:
        for rule in [*DEFAULT_PII_RULES, *DEFAULT_CONFIDENTIAL_RULES]:
            if rule.rule_id == rule_id:
                return rule
        return None

    def _fallback_rule(self, rule_id: str, data: SensitiveRuleUpsert | None) -> PiiRule:
        if data is not None:
            return PiiRule(
                category=data.category,
                rule_id=rule_id,
                name_re=data.name_re,
                sample_re=data.sample_re,
                confidence=data.confidence,
                pii=data.pii,
            )
        return PiiRule(
            category="NAME", rule_id=rule_id, name_re="", sample_re=None, confidence=0.7
        )

    async def _next_sort_order(self) -> int:
        rows = await self._repo.list_by_type("pii_rule", status=None)
        return (max((r.sort_order for r in rows), default=-1) + 1) * 10
