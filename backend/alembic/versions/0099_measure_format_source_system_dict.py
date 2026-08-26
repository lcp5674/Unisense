"""度量格式与源头系统字典化（原子指标口径库 measure_format/source_system 接入 system_dict）。

背景：
- ``measure_format`` 原为 MySQL ENUM（AMOUNT/RATIO/NUMERIC）硬编码，改为字典数据
  （dict_type=measure_format）供「系统设置 → 字典管理」在线增删改/启停用；每个格式
  字典项经 ``extra`` 扩展属性携带默认单位/小数位（``{"unit": "元", "decimal": 2}``），
  前端格式切换据此联动（PRD FR-02-08）。为此把 measure_catalog.measure_format 列从
  ENUM 改为 VARCHAR(32)，允许字典自定义值写入（对齐 category 字典化先例）。
- ``source_system`` 原为前端 tags 自由输入（无候选），改为字典数据
  （dict_type=source_system）提供候选（PRD FR-04-03 源头系统从业务系统选择，保留
  tags 自由输入），种入医疗场景标准源头系统。

幂等：measure_format / source_system 类型在 system_dict 尚无数据时才种入标准项；
已有数据（用户自定义/已种子）则跳过。downgrade 删除本迁移种入的标准项、恢复
measure_format 列 ENUM、删除 extra 列（不触碰用户后续新增项）。
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None

#: 度量格式种子（dict_type=measure_format）：extra 携带联动默认（unit/decimal）
FORMAT_SEED: list[tuple[str, str, int, str, dict]] = [
    ("AMOUNT", "金额", 0, "金额（默认单位 元、小数 2 位）", {"unit": "元", "decimal": 2}),
    ("RATIO", "比率", 1, "比率（默认单位 小数、小数 4 位）", {"unit": "小数", "decimal": 4}),
    ("NUMERIC", "数值", 2, "数值（自定义单位、小数按需）", {"unit": "", "decimal": None}),
]

#: 源头系统种子（dict_type=source_system，PRD FR-04-03 医疗场景标准源头系统）
SOURCE_SYSTEM_SEED: list[tuple[str, str, int, str]] = [
    ("HIS", "医院信息系统（HIS）", 0, "医院信息系统：挂号/收费/医嘱等业务主源"),
    ("EMR", "电子病历（EMR）", 1, "电子病历系统：病历文书/诊断记录"),
    ("LIS", "检验系统（LIS）", 2, "检验系统：检验申请/结果数据"),
    ("PACS", "影像系统（PACS）", 3, "影像系统：检查影像/报告数据"),
    ("MEDICAL_INSURANCE_PLATFORM", "医保结算平台", 4, "医保结算平台：医保结算/报销数据"),
    ("YINHAI_HIS", "银海his", 5, "银海 HIS：门诊/住院业务系统"),
    ("TIANXIN_HIS", "天信his", 6, "天信 HIS：门诊/住院业务系统"),
    ("PRE_POST_HIS_AUDIT", "事前事中his审核", 7, "事前事中 HIS 审核：医保智能审核"),
]


def upgrade() -> None:
    bind = op.get_bind()

    # 1) system_dict 加 extra 扩展属性列（度量格式联动默认单位/小数位）
    op.add_column("system_dict", sa.Column("extra", sa.JSON(), nullable=True, comment="扩展属性（JSON）"))

    # 2) measure_catalog.measure_format 从 ENUM 改 VARCHAR(32)——允许字典自定义格式写入
    op.execute(
        "ALTER TABLE measure_catalog "
        "MODIFY COLUMN measure_format VARCHAR(32) NOT NULL DEFAULT 'AMOUNT' COMMENT '度量格式（字典化，AMOUNT/RATIO/NUMERIC 及自定义）'"
    )

    # 3) 度量格式字典种子（含 extra 联动属性）
    existing_formats = bind.execute(
        sa.text("SELECT COUNT(*) FROM system_dict WHERE dict_type = 'measure_format'")
    ).scalar()
    if not existing_formats:
        for code, label, sort_order, description, extra in FORMAT_SEED:
            bind.execute(
                sa.text(
                    "INSERT INTO system_dict "
                    "(dict_type, code, label, sort_order, status, description, extra, "
                    "created_at, updated_at) "
                    "VALUES ('measure_format', :code, :label, :sort_order, 'active', "
                    ":description, :extra, NOW(), NOW())"
                ),
                {
                    "code": code,
                    "label": label,
                    "sort_order": sort_order,
                    "description": description,
                    "extra": json.dumps(extra, ensure_ascii=False),
                },
            )

    # 4) 源头系统字典种子
    existing_sources = bind.execute(
        sa.text("SELECT COUNT(*) FROM system_dict WHERE dict_type = 'source_system'")
    ).scalar()
    if not existing_sources:
        for code, label, sort_order, description in SOURCE_SYSTEM_SEED:
            bind.execute(
                sa.text(
                    "INSERT INTO system_dict "
                    "(dict_type, code, label, sort_order, status, description, "
                    "created_at, updated_at) "
                    "VALUES ('source_system', :code, :label, :sort_order, 'active', "
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
    bind = op.get_bind()

    # 删除本迁移种入的度量格式标准项（保留用户自定义项）
    for code, _label, _sort, _desc, _extra in FORMAT_SEED:
        bind.execute(
            sa.text("DELETE FROM system_dict WHERE dict_type = 'measure_format' AND code = :code"),
            {"code": code},
        )
    # 删除本迁移种入的源头系统标准项
    for code, _label, _sort, _desc in SOURCE_SYSTEM_SEED:
        bind.execute(
            sa.text("DELETE FROM system_dict WHERE dict_type = 'source_system' AND code = :code"),
            {"code": code},
        )

    # 恢复 measure_format 列为 ENUM（对齐迁移 0099 前定义）
    op.execute(
        "ALTER TABLE measure_catalog "
        "MODIFY COLUMN measure_format ENUM('AMOUNT','RATIO','NUMERIC') "
        "NOT NULL DEFAULT 'AMOUNT' COMMENT '度量格式（AMOUNT/RATIO/NUMERIC）'"
    )
    # 删除 extra 列
    op.drop_column("system_dict", "extra")
