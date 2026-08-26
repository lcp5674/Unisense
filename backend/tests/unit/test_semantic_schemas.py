"""语义层 Schema 校验测试（枚举字段非法值 → 422，而非穿透到 DB 抛 500）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.semantic.schemas import MetricCreateRequest, MetricPublishRequest


def _base_payload(**overrides) -> dict:
    payload = {
        "metric_code": "fin_gmv_amount_daily",
        "name": "GMV",
        "domain": "fin",
        "type": "atomic",
        # OneData 原子层：原子指标关联逻辑度量（度量目录）
        "measure_id": 1,
        "granularity": "DAY",
        "unit": "元",
        "aggregation": "SUM",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "dw_layer": "DWS",
        "metric_tier": "T2",
        "serving_mode": "BATCH_ONLY",
        "additivity": "ADDITIVE",
        "definition_json": {"expression": "SUM(amount)"},
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("aggregation", "MEAN"),
        ("time_semantics", "FOREVER"),
        ("freshness", "WEEKLY"),
        ("dw_layer", "STAGE"),
        ("metric_tier", "T4"),
        ("serving_mode", "STREAM_ONLY"),
        ("additivity", "PARTIAL"),
        ("type", "weird"),
    ],
)
def test_enum_fields_reject_invalid_values(field, bad_value):
    with pytest.raises(ValidationError):
        MetricCreateRequest(**_base_payload(**{field: bad_value}))


def test_valid_enum_values_accepted():
    req = MetricCreateRequest(**_base_payload())
    assert req.aggregation == "SUM"
    assert req.metric_tier == "T2"


@pytest.mark.parametrize(
    "field,value",
    [
        ("aggregation", "MAX"),
        ("aggregation", "MIN"),
        ("aggregation", "MEDIAN"),
        ("aggregation", "PERCENTILE"),
        ("time_semantics", "MOM"),
        ("time_semantics", "YOY"),
        ("freshness", "T0"),
    ],
)
def test_expanded_dict_enum_values_accepted(field, value):
    """字典种子扩展值（MAX/MIN/MEDIAN/PERCENTILE/MOM/YOY/T0）应被后端接受，避免 422。"""
    req = MetricCreateRequest(**_base_payload(**{field: value}))
    assert getattr(req, field) == value


def test_publish_version_optional():
    req = MetricPublishRequest(change_reason="首次发布")
    assert req.version is None
    req2 = MetricPublishRequest(version=3, change_reason="发布指定版本")
    assert req2.version == 3


# ---- definition_json 校验：SQL 语法 + source_tables 规范化 ----


def test_definition_sql_valid_accepted():
    """合法 SQL 通过校验，并保留在 definition_json.sql。"""
    req = MetricCreateRequest(
        **_base_payload(
            definition_json={
                "sql": (
                    "SELECT SUM(amount) AS gmv FROM catalog.sales.orders WHERE dt >= '2026-01-01'"
                )
            }
        )
    )
    assert "SELECT" in req.definition_json["sql"]


def test_definition_sql_invalid_rejected():
    """非法 SQL（语法错误）→ 422。"""
    with pytest.raises(ValidationError) as exc:
        MetricCreateRequest(**_base_payload(definition_json={"sql": "SELEC FROM WHERE"}))
    assert "SQL 语法错误" in str(exc.value)


def test_definition_sql_empty_rejected():
    """空 SQL → 422。"""
    with pytest.raises(ValidationError) as exc:
        MetricCreateRequest(**_base_payload(definition_json={"sql": "   "}))
    assert "非空字符串" in str(exc.value)


def test_definition_source_tables_normalized():
    """source_tables 规范化：去重 + 去空白 + 转字符串。"""
    req = MetricCreateRequest(
        **_base_payload(
            definition_json={
                "expression": "SUM(amount)",
                "source_tables": [" catalog.sales.orders ", "catalog.sales.orders", "  "],
            }
        )
    )
    assert req.definition_json["source_tables"] == ["catalog.sales.orders"]


def test_definition_source_tables_not_list_rejected():
    """source_tables 非数组 → 422。"""
    with pytest.raises(ValidationError) as exc:
        MetricCreateRequest(
            **_base_payload(definition_json={"source_tables": "catalog.sales.orders"})
        )
    assert "必须为数据表名数组" in str(exc.value)


def test_definition_downstream_tables_normalized():
    """downstream_tables（下游使用表）规范化：去重 + 去空白 + 转字符串。"""
    req = MetricCreateRequest(
        **_base_payload(
            definition_json={
                "expression": "SUM(amount)",
                "downstream_tables": [" ads.gmv_report ", "ads.gmv_report", "  "],
            }
        )
    )
    assert req.definition_json["downstream_tables"] == ["ads.gmv_report"]


def test_definition_downstream_tables_not_list_rejected():
    """downstream_tables 非数组 → 422。"""
    with pytest.raises(ValidationError) as exc:
        MetricCreateRequest(
            **_base_payload(definition_json={"downstream_tables": "ads.gmv_report"})
        )
    assert "必须为数据表名数组" in str(exc.value)


def test_definition_pseudo_and_dw_normalized():
    """口径双字段规范化：pseudo_definition/dw_definition 去空白保留。"""
    req = MetricCreateRequest(
        **_base_payload(
            definition_json={
                "expression": "SUM(amount)",
                "pseudo_definition": "  SUM(收费金额) 按结算日期去重  ",
                "dw_definition": "  SELECT visit_date, SUM(real_amount) FROM dwd.fee_bill_di  ",
            }
        )
    )
    assert req.definition_json["pseudo_definition"] == "SUM(收费金额) 按结算日期去重"
    assert req.definition_json["dw_definition"] == (
        "SELECT visit_date, SUM(real_amount) FROM dwd.fee_bill_di"
    )


def test_definition_pseudo_empty_rejected():
    """pseudo_definition 空字符串 → 422（防空白字段入库）。"""
    with pytest.raises(ValidationError) as exc:
        MetricCreateRequest(
            **_base_payload(definition_json={"pseudo_definition": "   "})
        )
    assert "必须为非空字符串" in str(exc.value)


def test_definition_dw_pseudo_survives_sql_not_required():
    """dw_definition 不强制走 sqlglot 校验（数仓开发维护，可含伪 SQL/建模口径文本）。"""
    req = MetricCreateRequest(
        **_base_payload(
            definition_json={
                "dw_definition": "由数仓开发维护的详细加工口径，非完整可执行 SQL",
            }
        )
    )
    assert req.definition_json["dw_definition"] == "由数仓开发维护的详细加工口径，非完整可执行 SQL"


def test_definition_update_sql_invalid_rejected():
    """更新请求的 definition_json 同样校验 SQL 语法（非法 SQL → 422）。"""
    from app.services.semantic.schemas import MetricUpdateRequest

    with pytest.raises(ValidationError):
        MetricUpdateRequest(
            change_reason="更新口径",
            definition_json={"sql": "SELEC FORM sales"},
        )


def test_definition_update_none_passes():
    """更新请求 definition_json 为 None 时跳过校验。"""
    from app.services.semantic.schemas import MetricUpdateRequest

    req = MetricUpdateRequest(change_reason="更新指标名称")
    assert req.definition_json is None


def test_template_create_owner_id_validation():
    """MetricTemplateCreateRequest.owner_id：合法值通过、0 被拒绝（ge=1）。"""
    from app.services.semantic.schemas import MetricTemplateCreateRequest

    # 合法 owner_id → 通过
    req = MetricTemplateCreateRequest(name="GMV 模板", domain="fin", owner_id=7)
    assert req.owner_id == 7
    # 不传 → None（兼容存量）
    assert MetricTemplateCreateRequest(name="GMV 模板", domain="fin").owner_id is None
    # 0 → 422
    with pytest.raises(ValidationError):
        MetricTemplateCreateRequest(name="GMV 模板", domain="fin", owner_id=0)


def test_template_create_onedata_presets():
    """方案A：模板 OneData 预设字段（measure_id/mount/三方责任）创建请求。"""
    from app.services.semantic.schemas import MetricTemplateCreateRequest

    req = MetricTemplateCreateRequest(
        name="派生 GMV 模板",
        domain="sales",
        type="derived",
        measure_id=5,
        mount={
            "source_table": "dwd.sales_detail",
            "source_column": "amount",
            "granularity": "日",
            "default_period": "day",
            "domain": "sales",
        },
        product_owner_id=2,
        product_owner_name="外部产品",
        serving_mode="REALTIME_ONLY",
    )
    assert req.measure_id == 5
    # dict → MetricMountInput 内嵌
    assert req.mount.source_table == "dwd.sales_detail"
    assert req.product_owner_id == 2
    assert req.product_owner_name == "外部产品"


def test_template_enum_literals_aligned_with_metric_create():
    """方案A：模板预设枚举与 MetricCreateRequest 同源——非法值（REALTIME）被拒。

    修复前模板 serving_mode 为宽松 str，模板作者可预设 "REALTIME"，实例化时撞
    MetricCreateRequest Literal 校验 422。收严后创建/编辑即拦截，从源头消除漂移。
    """
    from app.services.semantic.schemas import (
        MetricTemplateCreateRequest,
        MetricTemplateUpdateRequest,
    )

    with pytest.raises(ValidationError):
        MetricTemplateCreateRequest(name="x", domain="s", serving_mode="REALTIME")
    with pytest.raises(ValidationError):
        MetricTemplateCreateRequest(name="x", domain="s", aggregation="FOO")
    with pytest.raises(ValidationError):
        MetricTemplateUpdateRequest(serving_mode="REALTIME")
    # 合法值通过
    MetricTemplateCreateRequest(name="x", domain="s", serving_mode="REALTIME_ONLY")
    MetricTemplateUpdateRequest(additivity="SEMI_ADDITIVE")


# ---- 指标类型化口径校验（PRD 4.5：三类指标生产配置差异）----


def test_atomic_with_expression_passes():
    """原子指标关联逻辑度量（measure_id）+ 计算表达式 → 通过（OneData 原子层）。"""
    req = MetricCreateRequest(**_base_payload(definition_json={"expression": "SUM(amount)"}))
    assert req.definition_json["expression"] == "SUM(amount)"
    assert req.measure_id == 1


def test_atomic_requires_measure_id_or_physical_source():
    """OneData：原子指标必须关联逻辑度量（measure_id），仅表达式不足以锚定度量。

    - 有 measure_id → 通过（技术口径 expression 可选）
    - 无 measure_id 但提供来源表+度量列 → 通过（兼容旧式/批量注册路径）
    - 既无 measure_id 也无来源 → 422
    """
    # measure_id + 空 definition → 通过（原子=逻辑度量+聚合，不强制技术口径）
    req = MetricCreateRequest(**_base_payload(definition_json={}))
    assert req.measure_id == 1

    # 无 measure_id，仅 expression → 422（OneData：表达式不替代逻辑度量）
    with pytest.raises(ValidationError) as exc:
        MetricCreateRequest(
            **_base_payload(
                measure_id=None, definition_json={"expression": "SUM(amount)"}
            )
        )
    assert "逻辑度量" in str(exc.value)

    # 无 measure_id，提供来源表+度量列（旧式）→ 通过
    req = MetricCreateRequest(
        **_base_payload(
            measure_id=None,
            definition_json={"source_table": "dwd.sales_detail", "measure_column": "amount"},
        )
    )
    assert req.definition_json["measure_column"] == "amount"


def test_atomic_with_physical_source_passes():
    """原子指标无 measure_id 但提供来源表+度量列 → 通过（旧式/批量注册路径）。"""
    req = MetricCreateRequest(
        **_base_payload(
            measure_id=None,
            definition_json={"source_table": "dwd.sales_detail", "measure_column": "amount"},
        )
    )
    assert req.definition_json["measure_column"] == "amount"


def test_atomic_with_top_level_source_passes():
    """definition 为空但顶层 source_table/measure_column 提供来源 → 通过（自动推断）。"""
    req = MetricCreateRequest(
        **_base_payload(
            definition_json={},
            source_table="dwd.sales_detail",
            measure_column="amount",
        )
    )
    assert req.source_table == "dwd.sales_detail"


def test_atomic_without_measure_or_source_rejected():
    """原子指标既无 measure_id 也无来源表/度量列 → 422（OneData：原子须锚定逻辑度量）。"""
    with pytest.raises(ValidationError) as exc:
        MetricCreateRequest(
            **_base_payload(
                measure_id=None,
                definition_json={"expression": "SUM(amount)"},
            )
        )
    assert "逻辑度量" in str(exc.value)


def test_atomic_sql_mode_passes():
    """原子指标 SQL 模式口径（definition_json.sql）+ measure_id → 通过。"""
    req = MetricCreateRequest(
        **_base_payload(definition_json={"sql": "SELECT SUM(amount) FROM dwd.sales"})
    )
    assert req.definition_json["sql"]


def test_derived_requires_dependencies_and_expression():
    """OneData 派生 = 原子 + 时间周期：依赖可选，但须有计算表达式。"""
    # 派生无依赖但有表达式 → 通过（纯周期派生，如「本月活跃医生数」不依赖其他指标）
    req = MetricCreateRequest(
        **_base_payload(type="derived", definition_json={"expression": "SUM(active_cnt)"})
    )
    assert req.type == "derived"

    # 派生无表达式（无论有无依赖）→ 422
    with pytest.raises(ValidationError) as exc:
        MetricCreateRequest(
            **_base_payload(type="derived", definition_json={"dependencies": ["gmv"]})
        )
    assert "计算表达式" in str(exc.value)

    # 派生带依赖 + 表达式 → 通过（依赖型派生，血缘建 DERIVED_FROM 边）
    req = MetricCreateRequest(
        **_base_payload(
            type="derived",
            definition_json={
                "expression": "gmv / order_cnt",
                "dependencies": ["sales_gmv_amount_daily"],
            },
        )
    )
    assert req.type == "derived"


def test_composite_requires_dependencies_and_expression():
    """复合指标缺依赖（跨域多指标聚合的前提）或表达式 → 422；齐备 → 通过。"""
    with pytest.raises(ValidationError) as exc:
        MetricCreateRequest(
            **_base_payload(
                type="composite",
                definition_json={"dependencies": [], "expression": "sum(a)/sum(b)"},
            )
        )
    assert "依赖指标" in str(exc.value)

    req = MetricCreateRequest(
        **_base_payload(
            type="composite",
            definition_json={
                "expression": "SUM(region_in_east_gmv) / SUM(total_gmv)",
                "dependencies": ["east_gmv", "total_gmv"],
            },
        )
    )
    assert req.type == "composite"
