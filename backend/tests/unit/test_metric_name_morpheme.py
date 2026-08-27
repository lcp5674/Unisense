"""命名规范词根硬卡单测（TD §12.3：受控词根/词素校验）。

覆盖：命中词根放行、裸词/无意义命名拦截、中文/英文词根、维度类豁免、空名拦截。
另覆盖词根字典化（dict_type=metric_name_morpheme）：内置默认 ∪ DB 覆盖合并、
load/reset 缓存、字典新增词根即时对命名校验生效。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.semantic.conflict_precheck import (
    CONTROLLED_MORPHEMES,
    ConflictPrechecker,
    get_controlled_morphemes,
    load_metric_name_morphemes,
    reset_metric_name_morpheme_cache,
)


class TestValidateMetricName:
    def test_business_chinese_name_passes(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("每日收入金额")
        assert ok is True
        assert err is None

    def test_gmv_name_passes(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("每日 GMV")
        assert ok is True
        assert err is None

    def test_english_morpheme_case_insensitive(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("Daily GMV Amount")
        assert ok is True

    def test_order_count_passes(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("用户订单量")
        assert ok is True

    def test_rate_passes(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("退款率")
        assert ok is True

    def test_active_abbreviation_passes(self) -> None:
        """A-6：数仓活跃类高频缩写（月活/日活/周活/年活/季活）为合法业务命名，
        词根表须覆盖——否则 SQL 推断候选 name=「月活」被 METRIC_NAME_NO_MORPHEME 误拦。"""
        for name in ("月活", "日活", "周活", "年活", "季活", "医生月活"):
            ok, err = ConflictPrechecker.validate_metric_name(name)
            assert ok is True, f"{name!r} 应命中活跃缩写词根，实际 err={err!r}"

    def test_production_scenario_morphemes_pass(self) -> None:
        """生产场景词根覆盖（0103 扩充）：医疗 HIS 全流程/供应链/质量服务/英文业务对象
        的合法指标名应命中词根，不被 METRIC_NAME_NO_MORPHEME 误拦。"""
        production_names = [
            # 医疗/卫健（对应 auto_fill._CN_COLUMN_LABELS 产出的业务对象）
            "日活跃医生数",
            "在院护士数",
            "医院机构数",
            "科室分布数",
            "病区床位数",
            "疾病诊断数",
            "急诊人次",
            "体检人次",
            "病历数",
            "入院人次",
            "出院人次",
            "预约次数",
            "复诊人次",
            "诊疗次数",
            "护理工作量",
            "康复疗程数",
            "出院随访率",
            "门诊转诊数",
            "抢救成功率",
            "住院床日数",
            "次均住院费用",
            "诊次均费用",
            # 医保基金
            "医保统筹支付金额",
            "自费金额",
            "个账余额",
            # 供应链/业务量
            "发货单量",
            "签收件数",
            "退货率",
            "库存周转天数",
            "缺货率",
            # 质量/服务
            "客户满意度",
            "工单响应时长",
            "履约完成率",
            "风险告警数",
            # 英文业务对象
            "active_doctor_cnt",
            "patient_visit_daily",
            "hospital_ward_daily",
            "Daily Payment Amount",
            "Refund Rate",
        ]
        for name in production_names:
            ok, err = ConflictPrechecker.validate_metric_name(name)
            assert ok is True, f"{name!r} 应命中生产词根，实际 err={err!r}"

    def test_bare_word_rejected_with_clear_error(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("新名称")
        assert ok is False
        assert err is not None
        assert "受控词根" in err
        assert "收入" in err

    def test_nonsense_name_rejected(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("abc")
        assert ok is False

    def test_empty_name_rejected(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("")
        assert ok is False

    def test_whitespace_name_rejected(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("   ")
        assert ok is False

    def test_dimension_metric_exempt(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name(
            "区域", metric_type="dimension"
        )
        assert ok is True
        assert err is None

    def test_non_dimension_not_exempt(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("区域", metric_type="atomic")
        assert ok is False

    def test_controlled_morphemes_exposed_on_class(self) -> None:
        assert ConflictPrechecker.CONTROLLED_MORPHEMES is CONTROLLED_MORPHEMES
        assert "收入" in CONTROLLED_MORPHEMES
        assert "订单" in CONTROLLED_MORPHEMES
        # 0103 生产场景扩充词根（医疗/供应链/质量/英文）
        assert "医生" in CONTROLLED_MORPHEMES
        assert "科室" in CONTROLLED_MORPHEMES
        assert "满意度" in CONTROLLED_MORPHEMES
        assert "patient" in CONTROLLED_MORPHEMES


class TestDictionaryManagedMorphemes:
    """词根字典化（metric_name_morpheme）：内置默认 ∪ DB 覆盖、缓存加载/重置、
    字典新增词根即时对命名校验生效。"""

    @pytest.mark.asyncio
    async def test_unloaded_uses_builtin_only(self) -> None:
        reset_metric_name_morpheme_cache()
        try:
            assert get_controlled_morphemes() == CONTROLLED_MORPHEMES
            # 未加载 DB 覆盖时，字典新增词根尚未生效（「医技」仅存在于 mock DB，不在内置）
            ok, _err = ConflictPrechecker.validate_metric_name("医技专项")
            assert ok is False
        finally:
            reset_metric_name_morpheme_cache()

    @pytest.mark.asyncio
    async def test_loaded_merges_db_overrides_and_takes_effect(self) -> None:
        reset_metric_name_morpheme_cache()
        try:
            with patch(
                "app.services.system_dict.repository.SystemDictRepository.list_by_type",
                new_callable=AsyncMock,
            ) as mock_list:
                mock_list.return_value = [
                    SimpleNamespace(code="医技"),
                    SimpleNamespace(code="  "),  # 空白 code 应被过滤
                ]
                await load_metric_name_morphemes(AsyncMock())
            merged = get_controlled_morphemes()
            # 内置默认保留 + DB 新增合并
            assert "收入" in merged
            assert "医技" in merged
            # 字典新增词根即时对命名校验生效（无需发版）——「医技专项」不含任何内置词根，
            # 仅命中 DB 新增的「医技」，语义精确验证 DB 覆盖生效
            ok, _err = ConflictPrechecker.validate_metric_name("医技专项")
            assert ok is True
        finally:
            reset_metric_name_morpheme_cache()

    @pytest.mark.asyncio
    async def test_load_failure_keeps_builtin(self) -> None:
        reset_metric_name_morpheme_cache()
        try:
            with patch(
                "app.services.system_dict.repository.SystemDictRepository.list_by_type",
                new_callable=AsyncMock,
            ) as mock_list:
                mock_list.side_effect = RuntimeError("db down")
                await load_metric_name_morphemes(AsyncMock())
            # best-effort：加载失败回退内置默认，不抛异常
            assert get_controlled_morphemes() == CONTROLLED_MORPHEMES
        finally:
            reset_metric_name_morpheme_cache()

    @pytest.mark.asyncio
    async def test_reset_clears_overrides(self) -> None:
        reset_metric_name_morpheme_cache()
        try:
            with patch(
                "app.services.system_dict.repository.SystemDictRepository.list_by_type",
                new_callable=AsyncMock,
            ) as mock_list:
                mock_list.return_value = [SimpleNamespace(code="医技")]
                await load_metric_name_morphemes(AsyncMock())
            assert "医技" in get_controlled_morphemes()
            reset_metric_name_morpheme_cache()
            assert "医技" not in get_controlled_morphemes()
        finally:
            reset_metric_name_morpheme_cache()
