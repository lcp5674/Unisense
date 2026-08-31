"""采集链路编码乱码检测测试（TD §12.1 采集质量）。

覆盖：
- ``contains_mojibake`` 检测规则（U+FFFD 替换符 / GBK 二次转码残留 / 正常文本不误判）
- ``BaseCollector`` 乱码登记与取走（``_note_mojibake_field`` / ``_take_mojibake``）
- ``_apply_samples`` 对样本明文的乱码检测（打码前判定）
- Hive 连接器 ``_annotate_mojibake`` 写入 schema_json 标记
- Hive Metastore 连接器 ``_build_spec`` 对注释的乱码检测与标记
"""

from __future__ import annotations

from typing import Any

from app.services.collector.mojibake import contains_mojibake
from app.services.collector.spi import BaseCollector, CollectResult


class _ConcreteCollector(BaseCollector):
    """最小具体子类（BaseCollector 抽象，测试其基类方法须经具体子类）。"""

    async def collect(self, source: Any) -> CollectResult:
        return CollectResult(source_id="test")


# ---- contains_mojibake 检测规则 ----

def test_contains_mojibake_replacement_char() -> None:
    """U+FFFD 替换符命中（用户侧 2010��1��5�� 即每汉字一个替换符）。"""
    assert contains_mojibake("2010\uFFFDFF1\uFFFD5\uFFFD") is True
    assert contains_mojibake("门诊收费\uFFFD") is True


def test_contains_mojibake_gbk_second_pass_markers() -> None:
    """GBK→UTF-8 二次转码经典残留命中（EF BF BD 被按 GBK 读出）。"""
    assert contains_mojibake("锟斤拷") is True
    assert contains_mojibake("前值烫烫烫后") is True


def test_contains_mojibake_normal_text_not_flagged() -> None:
    """正常中文/英文/数字/日期/符号均不误判。"""
    assert contains_mojibake("2010年1月5日") is False
    assert contains_mojibake("门诊收费金额") is False
    assert contains_mojibake("order_amount") is False
    assert contains_mojibake("2026-08-31 12:00:00") is False
    assert contains_mojibake("Peace/和平") is False


def test_contains_mojibake_empty_or_none() -> None:
    """空串与 None 返回 False。"""
    assert contains_mojibake("") is False
    assert contains_mojibake(None) is False


# ---- BaseCollector 乱码登记与取走 ----

def test_take_mojibake_empty() -> None:
    c = _ConcreteCollector()
    assert c._take_mojibake() == {}


def test_note_and_take_mojibake() -> None:
    c = _ConcreteCollector()
    c._note_mojibake_field("col_a")
    c._note_mojibake_field("col_b", comment=True)
    got = c._take_mojibake()
    assert got == {"sample_fields": ["col_a"], "comment_fields": ["col_b"]}
    # 取走后清空，第二次返回空
    assert c._take_mojibake() == {}


def test_note_mojibake_empty_name_ignored() -> None:
    c = _ConcreteCollector()
    c._note_mojibake_field("")
    assert c._take_mojibake() == {}


# ---- _apply_samples 对样本明文的乱码检测 ----

def test_apply_samples_detects_mojibake() -> None:
    """样本值含 U+FFFD 时登记到 sample_fields（打码前对明文判定）。"""
    c = _ConcreteCollector()
    c.set_sampling(10)
    col: dict[str, object] = {"name": "remark"}
    c._apply_samples(col, ["正常值", "乱码\uFFFD值"])
    assert c._take_mojibake() == {"sample_fields": ["remark"]}


def test_apply_samples_clean_values_no_mojibake() -> None:
    """正常中文样本不登记乱码，且 sample 正常写入。"""
    c = _ConcreteCollector()
    c.set_sampling(10)
    col: dict[str, object] = {"name": "name"}
    c._apply_samples(col, ["张三", "李四"])
    assert c._take_mojibake() == {}
    assert col.get("sample") == ["张三", "李四"]


# ---- Hive 连接器 _annotate_mojibake ----

def test_hive_annotate_mojibake_writes_schema_marker() -> None:
    from app.services.collector.connectors.hive import HiveCollector

    c = HiveCollector(host="h", port=10000)
    c._note_mojibake_field("col_a")
    schema_json: dict[str, object] = {"columns": []}
    c._annotate_mojibake(schema_json, "src", "db.tbl")
    assert schema_json["mojibake"] == {"sample_fields": ["col_a"]}


def test_hive_annotate_mojibake_clean_no_marker() -> None:
    from app.services.collector.connectors.hive import HiveCollector

    c = HiveCollector(host="h", port=10000)
    schema_json: dict[str, object] = {"columns": []}
    c._annotate_mojibake(schema_json, "src", "db.tbl")
    assert "mojibake" not in schema_json


# ---- Hive Metastore 连接器 _build_spec 注释乱码检测 ----

def _hms_collector():
    from app.services.collector.connectors.hive_metastore import HiveMetastoreCollector

    # 绕过 __init__（需 DB 连接参数），仅测 _build_spec 的乱码登记逻辑；
    # 手动初始化基类乱码集合（__new__ 不执行 __init__）。
    c = object.__new__(HiveMetastoreCollector)
    c._mojibake_fields = set()
    c._mojibake_comment_fields = set()
    return c


def test_hms_build_spec_detects_comment_mojibake() -> None:
    c = _hms_collector()
    spec = c._build_spec(
        "db",
        "tbl",
        {"tbl_type": "MANAGED_TABLE", "tbl_comment": "测试表"},
        [
            {"column_name": "col_a", "type_name": "string", "comment": "正常注释"},
            {"column_name": "col_b", "type_name": "string", "comment": "乱码\uFFFD注释"},
        ],
    )
    assert spec.schema_json["mojibake"] == {"comment_fields": ["col_b"]}
    # 乱码注释保留原文（仅标记不修改），正常注释不受影响
    cols = {c0["name"]: c0 for c0 in spec.schema_json["columns"]}
    assert cols["col_b"]["comment"] == "乱码\uFFFD注释"
    assert cols["col_a"]["comment"] == "正常注释"


def test_hms_build_spec_clean_comments_no_marker() -> None:
    c = _hms_collector()
    spec = c._build_spec(
        "db",
        "tbl",
        {"tbl_type": "TABLE", "tbl_comment": None},
        [{"column_name": "col_a", "type_name": "string", "comment": "正常"}],
    )
    assert "mojibake" not in spec.schema_json
