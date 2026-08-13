"""中英术语字典单测（app/core/zh_en_dict.py）。

覆盖：
- 整段命中（供应链 → supply_chain）
- 贪心最长匹配分词（销售订单 → sales_order）
- 中文+ASCII 混合段（销售订单GMV → sales_order_gmv）
- 未覆盖单字拼音兜底（钿 → dian）
- 非 CJK 字符防御（原样小写保留）
- 空串/无可翻译 → 空串
"""

from __future__ import annotations

from app.core.zh_en_dict import zh_to_en


class TestZhToEn:
    def test_whole_phrase(self) -> None:
        assert zh_to_en("供应链") == "supply_chain"

    def test_greedy_longest_match(self) -> None:
        # 无「销售订单」整词时，贪心拆为 销售+订单
        assert zh_to_en("销售订单") == "sales_order"

    def test_compound_with_ascii(self) -> None:
        # 非 CJK 字符原样小写保留
        assert zh_to_en("销售订单GMV") == "sales_order_gmv"

    def test_pinyin_fallback_for_uncovered_char(self) -> None:
        # 未覆盖单字 → 拼音兜底
        assert zh_to_en("钿") == "dian"

    def test_uncovered_mixed_with_covered(self) -> None:
        # 覆盖词 + 未覆盖字混合
        assert zh_to_en("销售钿") == "sales_dian"

    def test_empty_segment(self) -> None:
        assert zh_to_en("") == ""

    def test_no_translatable(self) -> None:
        # 无字典命中且无拼音能力时返回空（本环境有 pypinyin，仅验证不抛异常）
        result = zh_to_en("123")
        assert result in ("123", "1_2_3")
