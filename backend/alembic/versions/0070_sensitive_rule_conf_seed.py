"""敏感规则配置台：补种机密规则种子（pii_rule, pii=false）。

背景（方案 A）：规则引擎此前仅把 PII 规则种子进 ``system_dict.pii_rule``，
机密规则（密码/税务/商业敏感）不可 DB 配置。本迁移补种 3 条机密规则种子，
使「敏感规则配置台」能展示并编辑全部内置规则。

与 0067 的「类型已有数据跳过」不同，本迁移**按 code 逐条幂等补种**——
不触碰用户已配置/修改的同 code 项，仅插入缺失项。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0070_sensitive_rule_conf_seed"
down_revision = "0069_data_source_coverage_total"
branch_labels = None
depends_on = None

#: 机密规则种子：code -> (label, sort_order, description)
_CONF_RULES: dict[str, tuple[str, int, str]] = {
    "password": (
        "密码/密钥规则",
        100,
        '{"category":"CREDENTIAL","name_re":"(password|pwd|secret|token|credential|api_?key|密钥|口令|密码)","sample_re":null,"confidence":0.95,"pii":false}',
    ),
    "tax": (
        "税务/发票规则",
        101,
        '{"category":"TAX","name_re":"(tax|vat|invoice|税务|税号|发票)","sample_re":null,"confidence":0.9,"pii":false}',
    ),
    "business": (
        "商业敏感规则",
        102,
        '{"category":"BUSINESS","name_re":"(salary|工资|薪酬|income|收入|revenue|营收|profit|利润|cost|成本|price|价格)","sample_re":null,"confidence":0.85,"pii":false}',
    ),
}


def upgrade() -> None:
    conn = op.get_bind()
    for code, (label, sort_order, description) in _CONF_RULES.items():
        existing = conn.execute(
            sa.text("SELECT COUNT(*) FROM system_dict WHERE dict_type = 'pii_rule' AND code = :code"),
            {"code": code},
        ).scalar()
        if existing:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO system_dict (dict_type, code, label, sort_order, status, description, created_at, updated_at) "
                "VALUES ('pii_rule', :code, :label, :sort, 'active', :desc, NOW(), NOW())"
            ),
            {"code": code, "label": label, "sort": sort_order, "desc": description},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for code in _CONF_RULES:
        conn.execute(
            sa.text("DELETE FROM system_dict WHERE dict_type = 'pii_rule' AND code = :code"),
            {"code": code},
        )
