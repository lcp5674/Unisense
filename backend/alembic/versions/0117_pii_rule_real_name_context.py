"""PII real_name 规则升级：`*_name` 列名统一入口 + 上下文判定

背景（2026-08-28 用户反馈）：
- ``village_department_count_*``（村部门计数表）等表里的裸 ``name`` 列（村名/机构名）
  被 real_name 规则 ``\\bname\\b`` 误判为个人姓名，全库 26% 的 PII 判定因此失真；
- 同时 ``\\bname\\b`` 因 ``_`` 是单词字符，匹配不到 ``patient_name`` 等带人名前缀的
  ``*_name`` 列（既有漏判）。

引擎层 ``SensitivityClassifier`` 已实现姓名列上下文判定（``_is_person_name``：
人名前缀→姓名；机构/地点/技术前缀→非姓名；裸 ``name`` 依表名语义；无法判断保守不判），
对 DB 规则与内置规则统一生效；本迁移仅把 DB 中 real_name 的 ``name_re`` 升级为
``(_?name$|姓名|用户名|昵称)``——让 ``*_name`` 列统一进入 real_name 分支后交由
上下文判定精确区分，修复裸 name 误判与 patient_name 漏判。

幂等：仅 UPDATE 仍含旧 ``\\bname\\b`` 的 real_name 项，可重复执行。
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0117_pii_rule_real_name_context"
down_revision = "0116_pii_rule_word_boundary"
branch_labels = None
depends_on = None

_NEW_NAME_RE = r"(_?name$|姓名|用户名|昵称)"


def upgrade() -> None:
    conn = op.get_bind()
    # 读取 real_name 配置，Python 侧判断是否仍含旧 `\bname\b`（避免 SQL LIKE 对
    # 反斜杠的转义歧义）；新 name_re 不含，天然幂等
    rows = conn.execute(
        sa.text(
            "SELECT id, description FROM system_dict "
            "WHERE dict_type = 'pii_rule' AND code = 'real_name' AND deleted_at IS NULL"
        )
    ).fetchall()
    for row_id, raw in rows:
        try:
            cfg = json.loads(raw or "{}")
        except (ValueError, TypeError):
            continue
        if r"\bname\b" not in str(cfg.get("name_re") or ""):
            continue
        cfg["name_re"] = _NEW_NAME_RE
        conn.execute(
            sa.text(
                "UPDATE system_dict SET description = :desc, updated_at = NOW() "
                "WHERE id = :id"
            ),
            {"id": row_id, "desc": json.dumps(cfg, ensure_ascii=False)},
        )


def downgrade() -> None:
    # 不做反向降级：上下文判定不可逆（旧正则为已知缺陷）；
    # 若确需回退，可通过配置台手工编辑或整体重建种子。
    pass
