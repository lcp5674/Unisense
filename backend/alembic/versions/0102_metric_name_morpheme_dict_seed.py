"""受控词根字典化种子（指标命名词根接入 system_dict）。

将指标命名规范（TD §12.3）的受控词根从代码常量（conflict_precheck.CONTROLLED_MORPHEMES）
种入 system_dict（dict_type=metric_name_morpheme），供「系统设置 → 字典管理」在线
增删改/启停用词根（原为代码常量，改词根需发版）。后端 ``get_controlled_morphemes``
读取「内置默认 ∪ DB active 词根」——迁移种入的词根与内置常量一致，行为不变，
字典管理新增/停用词根即时对命名校验生效。

幂等：仅当 metric_name_morpheme 类型在 system_dict 尚无任何数据时种入（对齐
0094_measure_category_dict_seed 模式）；已有数据（用户自定义/已种子）则跳过。
downgrade 仅删除本迁移种入的词根，不触碰用户后续新增项。

注意：本迁移只种字典数据，不改表结构；conflict_precheck 的内置 CONTROLLED_MORPHEMES
保留作为兜底默认（未加载 DB 或测试环境仍用内置）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0102_metric_name_morpheme_dict_seed"
down_revision = "0101_master_data_row_version"
branch_labels = None
depends_on = None

#: 受控词根种子（dict_type=metric_name_morpheme）：code 与 conflict_precheck 内置
#: CONTROLLED_MORPHEMES 一致；英文词根统一小写存储（匹配时本来就统一小写）。
SEED_GROUPS: list[tuple[str, list[str]]] = [
    ("财务/经营类", ["收入", "营收", "成本", "利润", "毛利", "净利", "金额", "总额", "余额", "资产", "负债", "税费", "费用"]),
    ("用户/客户类", ["用户", "客户", "会员", "粉丝", "客单"]),
    ("业务量类", ["订单", "销售", "销量", "产量", "产值", "库存", "退款", "复购", "转化", "留存", "活跃"]),
    ("数仓活跃缩写", ["月活", "日活", "周活", "年活", "季活"]),
    ("业务动因词", ["新增", "覆盖", "达标", "份额"]),
    ("度量词根", ["数量", "数", "量", "额", "价", "费", "率", "占比", "比例", "时长", "频次", "次数", "平均", "累计", "环比", "同比", "增长", "下降"]),
    ("医疗/医保类", ["门诊", "挂号", "就诊", "人次", "处方", "药品", "用药", "住院", "患者", "病人", "医保", "结算", "报销", "药占比", "检查", "检验", "手术", "抗菌", "候诊", "病种"]),
    ("英文业务词根", ["gmv", "arpu", "revenue", "cost", "profit", "user", "customer", "order", "amount", "count", "rate", "ratio", "sales", "sum", "avg"]),
]


def _seed_rows() -> list[tuple[str, str, int, str]]:
    rows: list[tuple[str, str, int, str]] = []
    sort_order = 0
    for group, codes in SEED_GROUPS:
        for code in codes:
            rows.append((code, code, sort_order, group))
            sort_order += 1
    return rows


def upgrade() -> None:
    bind = op.get_bind()
    existing = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM system_dict WHERE dict_type = 'metric_name_morpheme'"
        )
    ).scalar()
    if existing:
        return

    for code, label, sort_order, description in _seed_rows():
        bind.execute(
            sa.text(
                "INSERT INTO system_dict "
                "(dict_type, code, label, sort_order, status, description, "
                "created_at, updated_at) "
                "VALUES ('metric_name_morpheme', :code, :label, :sort_order, 'active', "
                ":description, NOW(), NOW())"
            ),
            {
                "code": code,
                "label": label,
                "sort_order": sort_order,
                "description": description,
            },
        )


def downgrade() -> None:
    """删除本迁移种入的词根（保留用户后续新增项）。"""
    bind = op.get_bind()
    for code, _label, _sort, _desc in _seed_rows():
        bind.execute(
            sa.text(
                "DELETE FROM system_dict WHERE dict_type = 'metric_name_morpheme' "
                "AND code = :code"
            ),
            {"code": code},
        )
