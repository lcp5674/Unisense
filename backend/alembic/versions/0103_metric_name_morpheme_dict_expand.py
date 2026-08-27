"""受控词根字典扩充（生产场景全覆盖）。

在 0102 种入的 91 条词根基础上，从实际生产场景出发补齐 197 条：
- 对照 auto_fill._CN_COLUMN_LABELS（SQL 推断中文名映射）已产出的业务对象
  （医生/护士/医院/机构/科室/病区/床位/疾病/诊断/症状/急诊/体检/病历/入院/出院/预约等）
  此前词根表未覆盖 → 推断名可能被 METRIC_NAME_NO_MORPHEME 误拦
- HIS 门诊全流程：收费/处方/药品/护理/住院/医保基金（统筹/自费/自付/个账/床日/次均/诊次）
- 通用财务/运营/用户流量/供应链/质量服务指标命名

对齐 conflict_precheck.CONTROLLED_MORPHEMES（内置默认已同步扩充，本迁移负责把新增词根
补种进已应用的 system_dict（0102 幂等 seed 对已存在的字典类型直接跳过，不会补种新增项）。

幂等：逐条 INSERT ... WHERE NOT EXISTS（不重复、不动已有词根）；downgrade 仅删除
本迁移补种的新增词根，不触碰用户后续新增项。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0103_metric_name_morpheme_dict_expand"
down_revision = "0102_metric_name_morpheme_dict_seed"  # 0102 的 revision 是长名（对齐 92ec3685 教训）
branch_labels = None
depends_on = None

#: 生产场景新增词根（dict_type=metric_name_morpheme）；与 conflict_precheck 内置
#: CONTROLLED_MORPHEMES 中新增部分一致。分组与内置保持同构，便于字典管理浏览。
EXPAND_GROUPS: list[tuple[str, list[str]]] = [
    # 财务/经营类（交易与资金流转）
    ("财务/经营类", ["交易", "成交", "支付", "收款", "付款", "应收", "应付", "预收", "预付", "折扣", "返利", "佣金", "工资", "薪酬", "奖金", "预算", "决算", "坏账"]),
    # 用户/流量类（注册/访问/内容互动）
    ("用户/流量类", ["注册", "登录", "访问", "访客", "浏览", "曝光", "下载", "安装", "启动", "分享", "点赞", "评论", "收藏", "关注", "订阅", "观看", "播放", "完播", "停留", "跳出", "跳失", "召回", "流失", "沉默"]),
    # 业务量/供应链类
    ("业务量/供应链类", ["发货", "签收", "退货", "换货", "售后", "进货", "补货", "铺货", "动销", "缺货", "库龄", "单量", "件数", "箱数", "笔数", "批次", "台次", "车次", "班次", "航次"]),
    # 业务动因词
    ("业务动因词", ["达成", "完成", "超额", "缺口", "净增"]),
    # 度量词根（统计/时点）
    ("度量词根", ["均值", "中位数", "方差", "标准差", "百分比", "千分比", "万分比", "单价", "均价", "时点", "期末", "期初"]),
    # 医疗/卫健类（人员/机构/资源/流程）
    ("医疗/卫健类", ["医生", "护士", "医院", "机构", "科室", "病区", "床位", "疾病", "诊断", "症状", "急诊", "体检", "病历", "入院", "出院", "预约", "复诊", "诊疗", "治疗", "护理", "康复", "随访", "转诊", "会诊", "抢救", "死亡", "治愈", "好转", "留观", "取药", "发药", "退药", "耗材", "器械", "西药", "中药", "中成药", "草药", "统筹", "自费", "自付", "个账", "床日", "周转", "次均", "诊次"]),
    # 质量/服务/管理类
    ("质量/服务/管理类", ["投诉", "客诉", "满意度", "健康度", "响应", "工单", "咨询", "线索", "商机", "合同", "回款", "开票", "履约", "超时", "风险", "告警"]),
    # 英文业务词根（医疗/业务对象/通用流量）
    ("英文业务词根", ["doctor", "nurse", "patient", "prescription", "drug", "medicine", "diagnosis", "disease", "symptom", "dept", "department", "hospital", "hosp", "ward", "bed", "operation", "surgery", "checkup", "admission", "discharge", "appointment", "emergency", "visit", "register", "payment", "pay", "income", "expense", "price", "total", "fee", "qty", "quantity", "num", "cnt", "percent", "duration", "hours", "minutes", "dau", "mau", "retention", "active", "refund", "stock", "inventory", "delivery", "login", "click", "view", "play", "share", "rating", "satisfaction", "coverage", "achievement"]),
]


def _expand_rows() -> list[tuple[str, str, int, str]]:
    rows: list[tuple[str, str, int, str]] = []
    # sort_order 从 1000 起（避开 0102 种入的 0..90），保证列表浏览顺序稳定在新增区段
    sort_order = 1000
    for group, codes in EXPAND_GROUPS:
        for code in codes:
            rows.append((code, code, sort_order, group))
            sort_order += 1
    return rows


def upgrade() -> None:
    bind = op.get_bind()
    for code, label, sort_order, description in _expand_rows():
        bind.execute(
            sa.text(
                "INSERT INTO system_dict "
                "(dict_type, code, label, sort_order, status, description, "
                "created_at, updated_at) "
                "SELECT 'metric_name_morpheme', :code, :label, :sort_order, 'active', "
                ":description, NOW(), NOW() "
                "WHERE NOT EXISTS (SELECT 1 FROM system_dict "
                "WHERE dict_type = 'metric_name_morpheme' AND code = :code)"
            ),
            {
                "code": code,
                "label": label,
                "sort_order": sort_order,
                "description": description,
            },
        )


def downgrade() -> None:
    """删除本迁移补种的新增词根（保留 0102 已有与用户后续新增项）。"""
    bind = op.get_bind()
    for code, _label, _sort, _desc in _expand_rows():
        bind.execute(
            sa.text(
                "DELETE FROM system_dict WHERE dict_type = 'metric_name_morpheme' "
                "AND code = :code"
            ),
            {"code": code},
        )
