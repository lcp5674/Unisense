"""OpenAPI 展示层中文化测试。

覆盖：
- ``info.title`` 品牌中文名与说明。
- 33 个业务分组全部携带中文名（``x-displayName``）与分组描述。
- 全部英文 operation summary 均被映射为中文（无残留）。
- 未命中的 summary 保留原文（新接口不破坏契约）。
"""

from __future__ import annotations

from app.main import app
from app.openapi_localize import SUMMARY_ZH, TAGS_ZH, localize_openapi


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
