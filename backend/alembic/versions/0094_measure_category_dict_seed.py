"""度量分类字典化种子（度量目录 category 接入 system_dict）。

将逻辑度量（原子指标口径库）的 ``category`` 字段从硬编码枚举（MeasureCategory）
改为 system_dict 字典数据（dict_type=measure_category），供「系统设置 → 字典管理」
在线增删改/启停用分类（原为代码常量，改分类需发版）。

幂等：仅当 measure_category 类型在 system_dict 尚无任何数据时种入标准 7 项
（对齐 MeasureCategory 枚举），已有数据（用户自定义/已种子）则跳过。
downgrade 仅删除本迁移种入的标准项，不触碰用户后续新增项。

注意：本迁移只种字典数据，不改表结构（measure_catalog.category 仍为 String(32)，
值来源从枚举放宽到字典，写入校验在 service 层由枚举改为字典校验）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0094_measure_category_dict_seed"
down_revision = "0093_metric_guide_source"
branch_labels = None
depends_on = None

#: 度量分类种子（dict_type=measure_category）：code 对齐 MeasureCategory 枚举值
SEED_DATA: list[tuple[str, str, int, str]] = [
    ("FLOW", "流量类", 0, "流量类（人次/单量：门诊人次、订单量）"),
    ("FEE", "费用类", 1, "费用类（金额：门诊费用、GMV）"),
    ("DRUG", "药品类", 2, "药品类（处方、药品用量/费用）"),
    ("MEDICAL_INSURANCE", "医保类", 3, "医保类（结算金额、报销比例）"),
    ("EFFICIENCY", "效率类", 4, "效率类（次均/人效、单价）"),
    ("QUALITY", "质量类", 5, "质量类（率/占比、质控指标）"),
    ("OTHER", "其他", 6, "其他/未分类"),
]


def upgrade() -> None:
    bind = op.get_bind()
    existing = bind.execute(
        sa.text("SELECT COUNT(*) FROM system_dict WHERE dict_type = 'measure_category'")
    ).scalar()
    if existing:
        return

    for code, label, sort_order, description in SEED_DATA:
        bind.execute(
            sa.text(
                "INSERT INTO system_dict "
                "(dict_type, code, label, sort_order, status, description, "
                "created_at, updated_at) "
                "VALUES ('measure_category', :code, :label, :sort_order, 'active', "
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
    """删除本迁移种入的标准分类（保留用户自定义项）。"""
    bind = op.get_bind()
    for code, _label, _sort, _desc in SEED_DATA:
        bind.execute(
            sa.text(
                "DELETE FROM system_dict WHERE dict_type = 'measure_category' AND code = :code"
            ),
            {"code": code},
        )
