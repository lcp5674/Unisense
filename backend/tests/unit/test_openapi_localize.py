"""OpenAPI 展示层中文化测试。

覆盖：
- ``info.title`` 品牌中文名与说明。
- 33 个业务分组全部携带中文名（``x-displayName``）与分组描述。
- 全部英文 operation summary 均被映射为中文（无残留）。
- 未命中的 summary 保留原文（新接口不破坏契约）。
- ``components.schemas`` 全部 schema 与高频字段 title 中文化，字段名/description 不被破坏。
"""

from __future__ import annotations

import re

from app.main import app
from app.openapi_localize import (
    FIELD_TITLE_ZH,
    SCHEMA_ZH,
    SUMMARY_ZH,
    TAGS_ZH,
    localize_openapi,
)


def _schema() -> dict:
    return app.openapi()


def test_info_localized() -> None:
    schema = _schema()
    info = schema["info"]
    assert info["title"] == "Unisense 指标语义中台"
    assert "认证" in info["description"]
    assert "自动同步" in info["description"]


def test_all_tags_have_chinese_display_name() -> None:
    schema = _schema()
    tags = schema.get("tags", [])
    assert len(tags) == len(TAGS_ZH), "顶层 tags 数量应与业务分组映射一致"
    zh_names = {zh for zh, _ in TAGS_ZH.values()}
    for tag in tags:
        # Swagger UI 5.x 用 tag.name 渲染分组标题 → name 必须已是中文
        assert tag["name"] in zh_names, f"tag name 未中文化: {tag['name']}"
        assert tag.get("description"), f"tag {tag['name']} 缺少分组描述"


def test_operation_tags_reference_localized_names() -> None:
    schema = _schema()
    zh_names = {zh for zh, _ in TAGS_ZH.values()}
    for path, methods in schema["paths"].items():
        for method, op in methods.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            for tag in op.get("tags", []):
                assert tag in zh_names, (
                    f"operation.tags 未同步中文化: {method.upper()} {path} -> {tag}"
                )


def test_all_english_summaries_translated() -> None:
    schema = _schema()
    leftovers: list[str] = []
    for path, methods in schema["paths"].items():
        for method, op in methods.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            summary = op.get("summary") or ""
            if not summary:
                continue
            if not any("\u4e00" <= c <= "\u9fff" for c in summary):
                leftovers.append(f"{method.upper()} {path}: {summary}")
    assert not leftovers, f"仍有英文 summary 未翻译（共 {len(leftovers)} 条）:\n" + "\n".join(
        leftovers[:20]
    )


def test_unmapped_summary_kept_verbatim() -> None:
    schema = _schema()
    # 注入一个映射表中不存在的英文 summary，应保留原文
    sample = schema["paths"]["/health"]["get"]
    original = sample["summary"]
    sample["summary"] = "Brand New Endpoint"
    try:
        localized = localize_openapi(schema)
        assert localized["paths"]["/health"]["get"]["summary"] == "Brand New Endpoint"
    finally:
        sample["summary"] = original


def test_summary_zh_has_no_duplicate_values() -> None:
    # 不同英文 summary 不应映射到同一中文（避免歧义；确需同义时人工确认）
    assert len(SUMMARY_ZH) == len(set(SUMMARY_ZH.values()))


# ============== Schemas 中文化测试 ==============


_API_RESPONSE_INNER_RE = re.compile(r"^ApiResponse_(.+)_$")


def _is_chinese_title(title: str | None) -> bool:
    """判定 schema title 已是中文（含模板前缀）。"""
    if not title:
        return False
    return "统一响应信封" in title or any("\u4e00" <= c <= "\u9fff" for c in title)


def test_api_response_class_title_chinese() -> None:
    schema = _schema()
    api_resp = schema["components"]["schemas"]["ApiResponse"]
    assert api_resp["title"] == "统一响应信封"


def test_api_response_template_titles_chinese() -> None:
    """全部 ``ApiResponse[X]`` 模板实例 title 已替换为中文前缀 + 内部类型中文名。"""
    schema = _schema()
    schemas = schema["components"]["schemas"]
    template_keys = [k for k in schemas if k.startswith("ApiResponse_")]
    assert len(template_keys) >= 50, "应覆盖绝大多数接口的响应模板"
    for k in template_keys:
        title = schemas[k]["title"]
        assert _is_chinese_title(title), f"{k} title 未中文化: {title}"
        # 模板实例必须以「统一响应信封」开头
        assert title.startswith("统一响应信封["), f"{k} title 模板前缀错: {title}"


def test_core_business_schemas_have_chinese_titles() -> None:
    """``SCHEMA_ZH`` 命中的核心业务 schema 全部 title 中文化。"""
    schema = _schema()
    schemas = schema["components"]["schemas"]
    missing = [k for k in SCHEMA_ZH if not _is_chinese_title(schemas[k].get("title"))]
    assert not missing, f"核心 schema 未中文化: {missing}"


def test_unmapped_schema_preserves_english_title() -> None:
    """未映射的 schema 保留英文 title（不破坏契约）。"""
    schema = _schema()
    schemas = schema["components"]["schemas"]
    # 注入一个全新的 key 不在 SCHEMA_ZH
    new_schema = {"type": "object", "title": "UnmappedBrandNew"}
    schemas["UnmappedBrandNew"] = new_schema
    try:
        localize_openapi(schema)
        assert schemas["UnmappedBrandNew"]["title"] == "UnmappedBrandNew"
    finally:
        schemas.pop("UnmappedBrandNew", None)


def test_field_title_chinese_without_overwriting_description() -> None:
    """``FIELD_TITLE_ZH`` 命中字段改 title，description 必须保留原值。"""
    schema = _schema()
    api_resp = schema["components"]["schemas"]["ApiResponse"]
    for field_name, expected_title in FIELD_TITLE_ZH.items():
        if field_name not in api_resp["properties"]:
            continue
        actual = api_resp["properties"][field_name]["title"]
        assert actual == expected_title, (
            f"字段 {field_name} title 应为 {expected_title}，实际 {actual}"
        )
    # description 不被覆盖：code 字段原来 description 为「业务码」
    assert api_resp["properties"]["code"]["description"] == "业务码"


def test_field_keys_unchanged() -> None:
    """字段名（properties key）必须保持英文，不被中文化破坏。"""
    schema = _schema()
    api_resp = schema["components"]["schemas"]["ApiResponse"]
    assert set(api_resp["properties"].keys()) == {"code", "message", "data", "trace_id"}


def test_localize_schemas_is_idempotent() -> None:
    """二次调用 localize_openapi 不重复改 title、不清空 description。"""
    schema = _schema()
    localize_openapi(schema)
    api_resp = schema["components"]["schemas"]["ApiResponse"]
    after_first_title = api_resp["title"]
    after_first_desc = api_resp["properties"]["code"]["description"]
    localize_openapi(schema)
    assert api_resp["title"] == after_first_title
    assert api_resp["properties"]["code"]["description"] == after_first_desc


def test_localize_does_not_break_unmapped_field_titles() -> None:
    """未映射的字段 title 保持 Pydantic 自动生成的英文（不破坏契约）。"""
    schema = _schema()
    # 注入测试 schema
    schemas = schema["components"]["schemas"]
    schemas["TestUnmappedFieldSchema"] = {
        "type": "object",
        "title": "TestUnmappedFieldSchema",
        "properties": {
            "unmapped_field_name": {"type": "string", "title": "Unmapped Field Name"},
            "code": {"type": "string", "title": "Code", "description": "业务码"},
        },
    }
    try:
        localize_openapi(schema)
        # 命中字段 title 改为中文
        ar_code_title = schemas["TestUnmappedFieldSchema"]["properties"]["code"]["title"]
        assert ar_code_title == "业务码"
        # 未命中字段保留 Pydantic 默认
        sc = schemas["TestUnmappedFieldSchema"]
        unmapped_title = sc["properties"]["unmapped_field_name"]["title"]
        assert unmapped_title == "Unmapped Field Name"
        # description 不动
        ar_code_desc = sc["properties"]["code"]["description"]
        assert ar_code_desc == "业务码"
    finally:
        schemas.pop("TestUnmappedFieldSchema", None)
