"""敏感规则配置台服务单测（方案 A：规则引擎可视化配置）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.sensitive_rules.service import SensitiveRuleService


def _dict_item(code: str, description: str | None, status: str = "active") -> SimpleNamespace:
    return SimpleNamespace(
        code=code,
        description=description,
        label=code,
        status=status,
        sort_order=0,
        updated_at=None,
    )


def _make_svc() -> SensitiveRuleService:
    db = MagicMock()
    svc = SensitiveRuleService(db)
    svc._repo = MagicMock()  # type: ignore[assignment]
    svc._dict_svc = MagicMock()  # type: ignore[assignment]
    return svc


async def test_list_rules_merges_builtin_and_db() -> None:
    svc = _make_svc()
    rows = [
        _dict_item(
            "phone",
            '{"category":"PHONE","name_re":"(customer_mobile)","sample_re":null,"confidence":0.9}',
        ),
        _dict_item(
            "password",
            '{"category":"CREDENTIAL","name_re":"(pwd)","sample_re":null,"confidence":0.95,"pii":false}',
            status="inactive",
        ),
    ]
    svc._repo.list_by_type = AsyncMock(return_value=rows)
    items = await svc.list_rules()
    result = list(items)
    # 12 PII 内置 + 3 机密内置（phone 被覆盖、password 停用仍各占一行）
    assert len(result) == 15
    by_id = {i.rule_id: i for i in result}
    # DB 覆盖同 ID 内置 → source=custom
    assert by_id["phone"].source == "custom"
    assert by_id["phone"].name_re == "(customer_mobile)"
    # 停用项保留展示，status=inactive
    assert by_id["password"].source == "custom"
    assert by_id["password"].status == "inactive"
    assert by_id["password"].pii is False
    # 无 DB 项的内置 → builtin + active
    assert by_id["id_card"].source == "builtin"
    assert by_id["id_card"].status == "active"


async def test_list_rules_appends_custom_rule() -> None:
    svc = _make_svc()
    rows = [
        _dict_item(
            "custom_rule",
            '{"category":"HEALTH","name_re":"(clinic)","sample_re":null,"confidence":0.95}',
        ),
    ]
    svc._repo.list_by_type = AsyncMock(return_value=rows)
    result = list(await svc.list_rules())
    assert len(result) == 16
    custom = next(i for i in result if i.rule_id == "custom_rule")
    assert custom.source == "custom"
    assert custom.category == "HEALTH"
    assert custom.category_label == "健康医疗"


def test_list_categories_has_15() -> None:
    svc = _make_svc()
    cats = svc.list_categories()
    assert len(cats) == 15
    assert len([c for c in cats if c.pii]) == 12
    assert len([c for c in cats if not c.pii]) == 3
    assert any(c.category == "ID_CARD" and c.label == "身份证号" for c in cats)


def test_validate_regex() -> None:
    svc = _make_svc()
    assert svc.validate_regex("(phone|mobile)").valid is True
    bad = svc.validate_regex("(phone")
    assert bad.valid is False
    assert bad.error


async def test_create_rule_persists_json() -> None:
    svc = _make_svc()
    svc._repo.get_item = AsyncMock(return_value=None)
    svc._repo.list_by_type = AsyncMock(return_value=[])
    created = _dict_item(
        "custom",
        '{"category":"PHONE","name_re":"(mobile)","sample_re":null,"confidence":0.9,"pii":true}',
    )
    svc._dict_svc.create_item = AsyncMock(return_value=created)

    from app.services.sensitive_rules.schemas import SensitiveRuleCreate

    item = await svc.create_rule(
        SensitiveRuleCreate(
            label="自定义手机号",
            category="PHONE",
            name_re="(mobile)",
            confidence=0.9,
            pii=True,
        )
    )
    assert item.rule_id == "custom"
    assert item.source == "custom"
    svc._dict_svc.create_item.assert_awaited_once()
    # description 应为合法规则 JSON
    import json

    call = svc._dict_svc.create_item.call_args
    cfg = json.loads(call.args[1].description)
    assert cfg["name_re"] == "(mobile)"
    assert cfg["pii"] is True
    assert cfg["category"] == "PHONE"


async def test_create_rule_duplicate_id_conflict() -> None:
    svc = _make_svc()
    svc._repo.get_item = AsyncMock(
        return_value=_dict_item("phone", '{"category":"PHONE","name_re":"x"}')
    )
    from app.services.sensitive_rules.schemas import SensitiveRuleCreate

    with pytest.raises(ConflictError):
        await svc.create_rule(
            SensitiveRuleCreate(
                rule_id="phone", label="x", category="PHONE", name_re="(x)"
            )
        )


async def test_create_rule_invalid_category() -> None:
    svc = _make_svc()
    from app.services.sensitive_rules.schemas import SensitiveRuleCreate

    with pytest.raises(ValidationError):
        await svc.create_rule(
            SensitiveRuleCreate(label="x", category="NOPE", name_re="(x)")
        )
    # pii=false 时机密类别合法、PII 类别非法
    with pytest.raises(ValidationError):
        await svc.create_rule(
            SensitiveRuleCreate(label="x", category="ID_CARD", name_re="(x)", pii=False)
        )


async def test_update_rule_creates_when_missing() -> None:
    svc = _make_svc()
    svc._repo.get_item = AsyncMock(return_value=None)
    svc._repo.list_by_type = AsyncMock(return_value=[])
    updated = _dict_item(
        "id_card",
        '{"category":"ID_CARD","name_re":"(sfz)","sample_re":null,"confidence":0.95,"pii":true}',
    )
    svc._dict_svc.create_item = AsyncMock(return_value=updated)

    from app.services.sensitive_rules.schemas import SensitiveRuleUpsert

    item = await svc.update_rule(
        "id_card",
        SensitiveRuleUpsert(label="身份证", category="ID_CARD", name_re="(sfz)", confidence=0.95),
    )
    assert item.rule_id == "id_card"
    assert item.source == "custom"
    svc._dict_svc.create_item.assert_awaited_once()


async def test_update_rule_updates_existing() -> None:
    svc = _make_svc()
    existing = _dict_item("phone", '{"category":"PHONE","name_re":"old"}')
    svc._repo.get_item = AsyncMock(return_value=existing)
    updated = _dict_item(
        "phone",
        '{"category":"PHONE","name_re":"(new)","sample_re":null,"confidence":0.9,"pii":true}',
    )
    svc._dict_svc.update_item = AsyncMock(return_value=updated)

    from app.services.sensitive_rules.schemas import SensitiveRuleUpsert

    item = await svc.update_rule(
        "phone",
        SensitiveRuleUpsert(label="手机号", category="PHONE", name_re="(new)", confidence=0.9),
    )
    assert item.name_re == "(new)"
    svc._dict_svc.update_item.assert_awaited_once()
    svc._dict_svc.create_item.assert_not_called()


async def test_set_status_creates_db_row_for_builtin_then_disables() -> None:
    """内置规则无 DB 项时停用：先落库（保留当前内置配置）再改状态。"""
    svc = _make_svc()
    svc._repo.get_item = AsyncMock(return_value=None)
    svc._repo.list_by_type = AsyncMock(return_value=[])
    created = _dict_item(
        "real_name",
        '{"category":"NAME","name_re":"(\\\\bname\\\\b|姓名)","sample_re":null,"confidence":0.7,"pii":true}',
        status="inactive",
    )
    svc._dict_svc.create_item = AsyncMock(return_value=created)
    svc._dict_svc.deactivate_item = AsyncMock(return_value=created)

    item = await svc.set_status("real_name", "deactivate")
    assert item.status == "inactive"
    assert item.source == "custom"
    svc._dict_svc.create_item.assert_awaited_once()
    svc._dict_svc.deactivate_item.assert_awaited_once()


async def test_set_status_unknown_rule_not_found() -> None:
    svc = _make_svc()
    svc._repo.get_item = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.set_status("nonexistent", "activate")


async def test_delete_rule_delegates() -> None:
    svc = _make_svc()
    svc._dict_svc.delete_item = AsyncMock()
    await svc.delete_rule("custom")
    svc._dict_svc.delete_item.assert_awaited_once_with("pii_rule", "custom")


async def test_test_rule_hits_pii() -> None:
    svc = _make_svc()
    db = MagicMock()
    empty = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
    db.execute = AsyncMock(return_value=empty)
    svc._db = db

    from app.services.sensitive_rules.schemas import RuleTestRequest

    resp = await svc.test_rule(
        RuleTestRequest(entity_name="ods_user", column_name="mobile", sample_value="13812345678")
    )
    assert resp.sensitivity_level == "PII"
    assert any(h.rule == "phone" and h.pii for h in resp.hits)


async def test_test_rule_hits_confidential() -> None:
    svc = _make_svc()
    db = MagicMock()
    empty = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
    db.execute = AsyncMock(return_value=empty)
    svc._db = db

    from app.services.sensitive_rules.schemas import RuleTestRequest

    resp = await svc.test_rule(RuleTestRequest(entity_name="cfg", column_name="password"))
    assert resp.sensitivity_level == "CONFIDENTIAL"
    assert not resp.hits  # 机密规则命中不进入 PII 字段明细


async def test_test_rule_no_hit_internal() -> None:
    svc = _make_svc()
    db = MagicMock()
    empty = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
    db.execute = AsyncMock(return_value=empty)
    svc._db = db

    from app.services.sensitive_rules.schemas import RuleTestRequest

    resp = await svc.test_rule(RuleTestRequest(entity_name="fact", column_name="amount"))
    assert resp.sensitivity_level == "INTERNAL"
    assert resp.hits == []
