"""中英业务同义词表（全局搜索共享数据源，FR-18 全局搜索栏生产化）。

同一份词对供两条检索路径消费，避免双份维护漂移：
- MySQL LIKE 路径（``global_search/repository.py``）：中文关键词扩展为英文候选，
  OR 进 LIKE 子句，命中英文表名/字段名/编码（如搜"订单"命中 ``sales_order``）；
- ES 路径（``search/es_indexer.py``）：生成 synonym token filter 的等价组，
  查询/索引时分词层把中文 token 等价为英文 token（如搜"订单"命中 name 中的 order）。

新增词对时两处自动生效。词条选取原则：
- 覆盖业务高频概念（订单/金额/患者/医保等），不追求穷举；
- 英文候选避免过短（如 ``ord`` 会误伤 ``record``），保证 substring/token 匹配质量。
"""

from __future__ import annotations

#: 中文业务词 → 英文候选列表（有序：主词在前，越具体越靠前，减少误伤）
SYNONYM_MAP: dict[str, list[str]] = {
    "订单": ["order", "sales_order"],
    "金额": ["amount", "fee", "price"],
    "患者": ["patient"],
    "门诊": ["outpatient", "clinic"],
    "住院": ["inpatient", "admission"],
    "费用": ["fee", "cost", "expense"],
    "收费": ["charge", "billing"],
    "医保": ["medical_insurance", "insurance"],
    "结算": ["settlement"],
    "药品": ["drug", "medicine"],
    "处方": ["prescription"],
    "成交": ["gmv", "transaction"],
    "支付": ["pay", "payment"],
    "退款": ["refund"],
    "会员": ["member"],
    "用户": ["user", "customer"],
    "医生": ["doctor"],
    "科室": ["department"],
    "库存": ["inventory", "stock"],
    "商品": ["product", "goods"],
    "销售": ["sales"],
    "收入": ["revenue", "income"],
    "成本": ["cost"],
    "利润": ["profit", "margin"],
    "数量": ["quantity", "qty"],
    "比率": ["rate", "ratio"],
    "平均": ["avg", "average"],
    "汇总": ["sum", "total"],
    "日期": ["date", "dt"],
    "时间": ["time", "ts"],
}


def es_synonym_lines() -> list[str]:
    """生成 ES synonym filter 的等价组行（如 ``"订单, order, sales_order"``）。

    ES synonym filter 用逗号分隔的等价组；首词为规范形式，其余为等价词。
    与 MySQL 扩展共用 ``SYNONYM_MAP``，保证两端词对一致。
    """
    return [", ".join([cn, *en]) for cn, en in SYNONYM_MAP.items()]
