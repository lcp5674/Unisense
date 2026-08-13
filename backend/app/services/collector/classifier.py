"""敏感分级规则引擎（对齐 TD §12.1「敏感分级（PII/机密）」）。

纯函数、确定性、可单测：依据实体名与字段名（schema_json.columns）匹配
PII / 机密 关键字与令牌正则，给出 sensitivity_level。
默认为 INTERNAL（PUBLIC 仅用于显式公开维度，不在自动分级中给出）。
"""

from __future__ import annotations

import re
from typing import Any

_PII_NAME_PATTERNS = [
    re.compile(r"(id_card|idcard|身份证|sfz)", re.I),
    re.compile(r"(phone|mobile|tel|手机|电话|手机号)", re.I),
    re.compile(r"(email|mail|邮箱|邮件)", re.I),
    re.compile(r"(name|姓名|用户名|user_name|real_name|昵称)", re.I),
    re.compile(r"(address|地址|住址|居住地)", re.I),
    re.compile(r"(bank_card|bankcard|银行卡|卡号|card_no|卡号)", re.I),
    re.compile(r"(id_no|证件号|cert_no|passport|护照)", re.I),
]
_PII_TOKEN_PATTERNS = [
    re.compile(r"\b\d{17}[\dxX]\b"),  # 身份证 18 位
    re.compile(r"\b1[3-9]\d{9}\b"),  # 手机号
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # 邮箱
]
_CONFIDENTIAL_PATTERNS = [
    re.compile(
        r"(salary|工资|薪酬|income|收入|revenue|营收|profit|利润|cost|成本|price|价格)", re.I
    ),
    re.compile(r"(password|pwd|secret|token|密钥|口令|密码|credential)", re.I),
    re.compile(r"(tax|税务|税|invoice|发票|vat)", re.I),
]


def _column_names(columns: list[Any]) -> list[str]:
    """提取列名（兼容字符串列与 dict 列两种 schema 格式）。

    postgres/clickhouse/hive/kafka 的 columns 为 ``[{"name": ..., "type": ...}]``
    结构，直接 ``str(col)`` 会引入字面量键名（如 "name"）而命中 PII 规则，
    导致这些连接器采集的每一张表都被误判为 PII（P0-1）。
    """
    names: list[str] = []
    for col in columns:
        if isinstance(col, dict):
            name = col.get("name")
            if name:
                names.append(str(name))
        elif isinstance(col, str):
            names.append(col)
    return names


class SensitivityClassifier:
    """敏感分级分类器（无状态，可注入用于测试）。"""

    def classify(self, entity_name: str, schema_json: dict[str, Any]) -> str:
        """返回 sensitivity_level：PII / CONFIDENTIAL / INTERNAL。

        Args:
            entity_name: 实体（表/视图）名。
            schema_json: 含 ``columns`` 字段的字典。

        Returns:
            敏感级别字符串。
        """
        columns = schema_json.get("columns", []) if isinstance(schema_json, dict) else []
        haystack = f"{entity_name} " + " ".join(_column_names(columns))
        for pattern in _PII_NAME_PATTERNS:
            if pattern.search(haystack):
                return "PII"
        for pattern in _PII_TOKEN_PATTERNS:
            if pattern.search(haystack):
                return "PII"
        for pattern in _CONFIDENTIAL_PATTERNS:
            if pattern.search(haystack):
                return "CONFIDENTIAL"
        return "INTERNAL"
