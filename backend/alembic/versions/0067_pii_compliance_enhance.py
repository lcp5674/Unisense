"""PII 合规增强：目录资产表级复核/脱敏/保留期 + 字段误报标注表 + 敏感规则字典种子。

背景（PII 合规增强 A/B/C）：资产地图此前仅展示「含 PII」红 Tag，缺少治理闭环与
字段级明细。本迁移为目录资产补齐合规治理所需结构与规则配置：

1. ``db_catalog`` 新增 8 列：
   - 表级合规复核三件套（compliance_reviewed / reviewed_by / reviewed_at）；
   - 脱敏策略（masking_policy：none/mask/hash/deny，缺省按敏感级推导）；
   - 保留期与合法性（retention_days / legal_basis / retention_expires_at /
     retention_notified_at）。
2. 新表 ``pii_field_override``：字段级人工标注（suppressed=True 标注误报非 PII，
   False 人工确认为 PII），与 classification.pii_columns 明细互补。
3. ``system_dict`` 种子：``pii_category``（12 类 PII 类别）+ ``pii_rule``
   （12 条规则配置 JSON，供规则引擎 DB 可配置覆盖），幂等插入。

注：revision 挂 0066_data_source_multi_db_schedule（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0067_pii_compliance_enhance"
down_revision = "0066_data_source_multi_db_schedule"
branch_labels = None
depends_on = None

#: PII 类别种子：dict_type=pii_category -> (code, label, sort_order, description)
_PII_CATEGORIES: list[tuple[str, str, int, str]] = [
    ("ID_CARD", "身份证号", 0, "身份证号（样本 18 位）"),
    ("PHONE", "手机/电话", 1, "手机号/电话号码"),
    ("EMAIL", "邮箱", 2, "电子邮箱"),
    ("NAME", "姓名/用户名", 3, "姓名、用户名、昵称"),
    ("ADDRESS", "地址", 4, "住址/收货地址"),
    ("BANK_CARD", "银行卡", 5, "银行卡号/银行账户"),
    ("DOCUMENT", "证件号", 6, "其他证件号（驾照/社保等）"),
    ("PASSPORT", "护照", 7, "护照号"),
    ("GPS", "行踪定位", 8, "GPS/经纬度/定位"),
    ("HEALTH", "健康医疗", 9, "健康/病历/医疗信息（个保法敏感个人信息）"),
    ("BIOMETRIC", "生物特征", 10, "指纹/人脸/虹膜/基因（个保法敏感个人信息）"),
    ("FINANCIAL", "金融敏感", 11, "账户余额/持仓等金融敏感信息"),
]

#: PII 规则种子：dict_type=pii_rule -> (code=rule_id, label, sort_order,
#: description=JSON{name_re,sample_re,confidence,category})——规则引擎从该配置
#: 覆盖内置默认规则（DB 配置优先，未配置用内置）。
_PII_RULES: list[tuple[str, str, int, str]] = [
    ("id_card", "身份证规则", 0, '{"category":"ID_CARD","name_re":"(id_?card|identity_?no|shenfen|sfz|身份证)","sample_re":"^\\\\d{17}[\\\\dXx]$","confidence":0.95}'),
    ("phone", "手机号规则", 1, '{"category":"PHONE","name_re":"(phone|mobile|tel|telephone|手机|电话|手机号|联系电话)","sample_re":"^1[3-9]\\\\d{9}$","confidence":0.9}'),
    ("email", "邮箱规则", 2, '{"category":"EMAIL","name_re":"(email|mail_?addr|邮箱|邮件|电子邮箱)","sample_re":"^[^@\\\\s]+@[^@\\\\s]+\\\\.[^@\\\\s]+$","confidence":0.9}'),
    ("real_name", "姓名规则", 3, '{"category":"NAME","name_re":"(\\\\bname\\\\b|姓名|用户名|user_?name|cust_?name|full_?name|real_?name|昵称)","sample_re":null,"confidence":0.7}'),
    ("address", "地址规则", 4, '{"category":"ADDRESS","name_re":"(address|addr|location_detail|地址|住址|居住地|收货地址)","sample_re":null,"confidence":0.7}'),
    ("bank_card", "银行卡规则", 5, '{"category":"BANK_CARD","name_re":"(bank_?card|bankcard|card_?no|account_?no|银行卡|卡号|银行账号)","sample_re":"^\\\\d{16,19}$","confidence":0.9}'),
    ("id_no", "证件规则", 6, '{"category":"DOCUMENT","name_re":"(id_no|cert_no|证件号|证件|license_no|驾照)","sample_re":null,"confidence":0.85}'),
    ("passport", "护照规则", 7, '{"category":"PASSPORT","name_re":"(passport|护照)","sample_re":null,"confidence":0.85}'),
    ("gps", "定位规则", 8, '{"category":"GPS","name_re":"(lat|lng|longitude|latitude|geo_?point|position|定位|坐标|经纬度)","sample_re":null,"confidence":0.6}'),
    ("health", "健康规则", 9, '{"category":"HEALTH","name_re":"(health|medical|disease|illness|diagnos|blood|pressure|sugar|heart_?rate|bmi|病历|健康|体检|血压|血糖|心率|诊疗|医疗)","sample_re":null,"confidence":0.85}'),
    ("biometric", "生物特征规则", 10, '{"category":"BIOMETRIC","name_re":"(biometric|fingerprint|face_?id|iris|voiceprint|dna|基因|指纹|人脸|虹膜|声纹)","sample_re":null,"confidence":0.9}'),
    ("financial", "金融规则", 11, '{"category":"FINANCIAL","name_re":"(bank_?balance|account_?balance|余额|金融资产|投资|理财|证券|股票|持仓)","sample_re":null,"confidence":0.85}'),
]


def _seed_dicts(dict_type: str, items: list[tuple[str, str, int, str]]) -> None:
    """幂等种子：system_dict 中该类型已有数据时跳过（保护用户自定义环境）。"""
    conn = op.get_bind()
    existing = conn.execute(
        sa.text("SELECT COUNT(*) FROM system_dict WHERE dict_type = :dt"),
        {"dt": dict_type},
    ).scalar()
    if existing:
        return
    for code, label, sort_order, description in items:
        conn.execute(
            sa.text(
                "INSERT INTO system_dict (dict_type, code, label, sort_order, status, description, created_at, updated_at) "
                "VALUES (:dt, :code, :label, :sort, 'active', :desc, NOW(), NOW())"
            ),
            {"dt": dict_type, "code": code, "label": label, "sort": sort_order, "desc": description},
        )


def upgrade() -> None:
    bind = op.get_bind()
    # db_catalog 已有数据的场景下，新增列须给默认值（MySQL 8 严格模式）
    existing = bind.execute(
        sa.text("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'db_catalog' AND column_name = 'compliance_reviewed'")
    ).scalar()
    if not existing:
        op.add_column("db_catalog", sa.Column("compliance_reviewed", sa.Boolean(), nullable=False, server_default=sa.text("0"), comment="PII 合规是否已复核（表级）"))
    op.add_column("db_catalog", sa.Column("compliance_reviewed_by", sa.BigInteger(), nullable=True, comment="合规复核人 ID"))
    op.add_column("db_catalog", sa.Column("compliance_reviewed_at", sa.DateTime(timezone=True), nullable=True, comment="合规复核时间（UTC）"))
    op.add_column("db_catalog", sa.Column("masking_policy", sa.String(length=16), nullable=True, comment="脱敏策略（none/mask/hash/deny）"))
    op.add_column("db_catalog", sa.Column("retention_days", sa.Integer(), nullable=True, comment="保留期（天）"))
    op.add_column("db_catalog", sa.Column("legal_basis", sa.String(length=64), nullable=True, comment="合法性基础"))
    op.add_column("db_catalog", sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True, comment="保留期到期时间（UTC）"))
    op.add_column("db_catalog", sa.Column("retention_notified_at", sa.DateTime(timezone=True), nullable=True, comment="到期提醒时间（UTC）"))

    op.create_index("idx_db_catalog_review", "db_catalog", ["compliance_reviewed"])
    op.create_index("idx_db_catalog_retention", "db_catalog", ["retention_expires_at"])

    op.create_table(
        "pii_field_override",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column("catalog_id", sa.BigInteger(), nullable=False, comment="关联 db_catalog.id"),
        sa.Column("column_name", sa.String(length=128), nullable=False, comment="字段名"),
        sa.Column("suppressed", sa.Boolean(), nullable=False, server_default=sa.text("1"), comment="True=标注非 PII（误报）；False=人工确认是 PII"),
        sa.Column("reason", sa.String(length=256), nullable=True, comment="标注理由"),
        sa.Column("created_by", sa.BigInteger(), nullable=True, comment="标注人 ID"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()"), comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()"), comment="更新时间（UTC）"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间（UTC）"),
        sa.UniqueConstraint("catalog_id", "column_name", name="uk_pii_override_catalog_column"),
    )
    op.create_index("idx_pii_override_catalog", "pii_field_override", ["catalog_id"])

    _seed_dicts("pii_category", _PII_CATEGORIES)
    _seed_dicts("pii_rule", _PII_RULES)


def downgrade() -> None:
    op.drop_index("idx_pii_override_catalog", table_name="pii_field_override")
    op.drop_table("pii_field_override")
    op.drop_index("idx_db_catalog_retention", table_name="db_catalog")
    op.drop_index("idx_db_catalog_review", table_name="db_catalog")
    for col in (
        "retention_notified_at",
        "retention_expires_at",
        "legal_basis",
        "retention_days",
        "masking_policy",
        "compliance_reviewed_at",
        "compliance_reviewed_by",
        "compliance_reviewed",
    ):
        op.drop_column("db_catalog", col)
    # 仅删除种子标准项，不触碰用户自定义项
    conn = op.get_bind()
    rule_codes = [code for code, _label, _sort, _desc in _PII_RULES]
    conn.execute(
        sa.text("DELETE FROM system_dict WHERE dict_type = 'pii_rule' AND code IN :codes"),
        {"codes": tuple(rule_codes)},
    )
    conn.execute(sa.text("DELETE FROM system_dict WHERE dict_type = 'pii_category'"))
