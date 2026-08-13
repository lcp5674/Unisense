"""系统字典种子数据（FR-005）。

迁移 0026 只建表未种子，导致字典管理页与指标录入下拉为空（空壳）。
本迁移为 10 种字典类型种入标准初始选项，覆盖指标注册全部字段枚举值，
并对齐当前 ``MetricCreateRequest`` 的 Literal 校验值，确保种子后字典校验
（``_validate_dict_fields``）不再因缺项阻断创建。

幂等：``system_dict`` 已有任意数据时跳过，保护用户自定义/已配置环境。
downgrade 仅删除种子标准项，不触碰用户后续新增项。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_system_dict_seed"
down_revision = "0029_quality_softdelete"
branch_labels = None
depends_on = None

#: 种子数据：dict_type -> [(code, label, sort_order, description), ...]
#: code 必须覆盖 MetricCreateRequest 当前 Literal 枚举值 + 常见业务选项。
SEED_DATA: dict[str, list[tuple[str, str, int, str]]] = {
    "granularity": [
        ("minute", "分钟", 0, "分钟级粒度"),
        ("hour", "小时", 1, "小时级粒度"),
        ("day", "日", 2, "日级粒度"),
        ("week", "周", 3, "周级粒度"),
        ("month", "月", 4, "月级粒度"),
        ("quarter", "季度", 5, "季度粒度"),
        ("year", "年", 6, "年级粒度"),
        ("realtime", "实时", 7, "实时粒度"),
    ],
    "unit": [
        ("CNY", "人民币元", 0, "人民币（元）"),
        ("USD", "美元", 1, "美元"),
        ("EUR", "欧元", 2, "欧元"),
        ("PERCENT", "百分比", 3, "百分比（%）"),
        ("CNY_WAN", "万元", 4, "人民币（万元）"),
        ("CNY_YI", "亿元", 5, "人民币（亿元）"),
        ("ORDER", "单", 6, "订单量单位"),
        ("TIMES", "次", 7, "次数单位"),
        ("PERSON", "人", 8, "人数单位"),
        ("DAY", "天", 9, "天（时间跨度）"),
        ("HOUR", "小时", 10, "小时（时间跨度）"),
        ("MINUTE", "分钟", 11, "分钟（时间跨度）"),
    ],
    "aggregation": [
        ("SUM", "求和", 0, "SUM 求和"),
        ("AVG", "平均", 1, "AVG 平均"),
        ("COUNT", "计数", 2, "COUNT 计数"),
        ("COUNT_DISTINCT", "去重计数", 3, "COUNT_DISTINCT 去重计数"),
        ("LAST_VALUE", "最后值", 4, "LAST_VALUE 取最后值"),
        ("MAX", "最大值", 5, "MAX 最大值"),
        ("MIN", "最小值", 6, "MIN 最小值"),
        ("MEDIAN", "中位数", 7, "MEDIAN 中位数"),
        ("PERCENTILE", "分位数", 8, "PERCENTILE 分位数"),
    ],
    "time_semantics": [
        ("PERIOD", "周期值", 0, "当期值（如当月/当日）"),
        ("YTD", "年初至今", 1, "Year-To-Date 累计"),
        ("TTM", "滚动12月", 2, "Trailing Twelve Months"),
        ("AVG", "平均值", 3, "时间平均值"),
        ("MOM", "环比", 4, "Month-over-Month 环比"),
        ("YOY", "同比", 5, "Year-over-Year 同比"),
    ],
    "freshness": [
        ("REALTIME", "实时", 0, "实时更新"),
        ("T0", "当日", 1, "当日产出"),
        ("T1", "次日", 2, "T+1 次日产出"),
        ("HOURLY", "小时级", 3, "小时级产出"),
    ],
    "dw_layer": [
        ("ODS", "贴源层", 0, "操作数据存储层"),
        ("DWD", "明细层", 1, "数据明细层"),
        ("DWS", "汇总层", 2, "数据汇总层"),
        ("ADS", "应用层", 3, "应用数据层"),
        ("DM", "数据集市", 4, "数据集市"),
    ],
    "metric_type": [
        ("atomic", "原子指标", 0, "基于明细直接计算的指标"),
        ("derived", "派生指标", 1, "由原子指标推导的指标"),
        ("composite", "复合指标", 2, "多指标复合计算"),
    ],
    "additivity": [
        ("ADDITIVE", "可加", 0, "跨维度直接加总"),
        ("SEMI_ADDITIVE", "半可加", 1, "部分维度可加（如余额）"),
        ("NON_ADDITIVE", "不可加", 2, "不可加总（如比率）"),
    ],
    "serving_mode": [
        ("BATCH_ONLY", "仅批量", 0, "仅批量产出"),
        ("REALTIME_ONLY", "仅实时", 1, "仅实时产出"),
        ("BATCH_REALTIME_DUAL", "批量+实时双模", 2, "批量与实时双通道"),
    ],
    "metric_tier": [
        ("T1", "核心指标", 0, "核心经营指标（最高 SLA）"),
        ("T2", "重要指标", 1, "重要业务指标"),
        ("T3", "一般指标", 2, "一般辅助指标"),
    ],
}


def upgrade() -> None:
    bind = op.get_bind()
    existing = bind.execute(sa.text("SELECT COUNT(*) FROM system_dict")).scalar()
    if existing:
        # 已有数据（用户自定义或已种子）→ 跳过，避免覆盖/重复
        return

    for dict_type, items in SEED_DATA.items():
        for code, label, sort_order, description in items:
            bind.execute(
                sa.text(
                    "INSERT INTO system_dict "
                    "(dict_type, code, label, sort_order, status, description, "
                    "created_at, updated_at) "
                    "VALUES (:dict_type, :code, :label, :sort_order, 'active', "
                    ":description, NOW(), NOW())"
                ),
                {
                    "dict_type": dict_type,
                    "code": code,
                    "label": label,
                    "sort_order": sort_order,
                    "description": description,
                },
            )


def downgrade() -> None:
    """删除种子标准项（仅删除本迁移种入的项，保留用户自定义项）。"""
    bind = op.get_bind()
    for dict_type, items in SEED_DATA.items():
        for code, _label, _sort, _desc in items:
            bind.execute(
                sa.text("DELETE FROM system_dict WHERE dict_type = :dict_type AND code = :code"),
                {"dict_type": dict_type, "code": code},
            )
