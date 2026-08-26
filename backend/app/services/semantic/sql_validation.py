"""SQL 解析正确性的 LLM 校验层 + 一致性仲裁（方案 A：默认全量校验）。

背景：规则解析（sqlglot AST）最大的风险不是「解析不出」（失败会兜底），而是
「静默解析错」——返回了结果但是错的（漏度量 / 聚合归一错 / 条件聚合丢失），
而这恰恰是测试覆盖不到、规则自己不会发现的。方案 A 让每次 SQL 解析都过一遍
LLM 校验：LLM 当「校验者/评审者」而非「替代者」，对规则解析结果做封闭选择
验证，再用确定性仲裁层收敛——随机性发生在 LLM，一致性由算法层保证。

校验设计（对齐「规则锚定 + LLM 补全 + 规范收敛」架构）：
- **锚定**：规则解析出的度量清单（column/agg/alias/table）是确定可审计的；
- **LLM 封闭选择**：不让 LLM 自由生成度量，只对每个规则度量做
  ``is_measure``（是否真度量）/ ``agg``（从注册枚举选）/ ``table``（从解析
  源表选）/ ``period``（从白名单选）判断 + ``missed``（扫描规则漏掉的度量）——
  决策面从「生成几十个字段」压缩到「对 N 个度量各做 2-3 个选择 + 补漏扫描」；
- **单次调用**：整段 SQL 只花 1 次 LLM 调用，``temperature=0`` + ``json_object``；
- **规范收敛（``merge_validation``）**：聚合/周期必须命中白名单、列名回映到
  解析出的源表、高置信度才采纳纠正/剔除、低置信度标记 ``needs_review`` 不静默
  落库——LLM 的随机性与个别误判在算法层被剥掉。

LLM 不可用 / 超时 / 解析失败 → 返回 ``None``，上层保持规则结果不动，绝不阻断。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 注册聚合枚举白名单（与 sql/services/schemas.py 的 aggregation Literal 一致，
# 防 LLM 幻觉产出非法聚合致创建整批失败，对齐 P1-4 教训）。
AGG_ENUM: frozenset[str] = frozenset(
    {
        "SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", "FIRST_VALUE",
        "MAX", "MIN", "MEDIAN", "PERCENTILE",
    }
)

# 置信度阈值：≥ 此值才采纳 LLM 纠正/剔除；低于此值标记需人工确认
_CONFIDENCE_ACCEPT = 0.7


def _sane_column(col: Any) -> bool:
    """度量列名合法性：非空、纯标识符（允许 ``*`` 仅供 COUNT 类）。

    防 LLM 幻觉产出函数名/带括号表达式/含空白串等不可能存在的列。
    """
    if not col or not isinstance(col, str):
        return False
    name = col.strip()
    if not name or len(name) > 128:
        return False
    if name == "*":
        return True
    return all(ch.isalnum() or ch in "._-" for ch in name)


def parse_sql_validation_result(raw: str) -> dict[str, Any] | None:
    """解析 LLM 校验输出（约定 JSON 结构）。

    ``{"items": [{"key", "is_measure", "agg", "table", "period",
    "confidence", "reason"}], "missed": [{"column", "agg", "alias",
    "table", "confidence"}], "period": "...", "confidence": 0.9}``

    - ``items``：规则度量的逐项校验（key 为提示中的稳定序号）；
    - ``missed``：规则漏掉、LLM 扫描补充的度量（column/agg 必填白名单校验）；
    - 聚合必须命中注册枚举、周期经 ``normalize_period`` 白名单归一、列名过
      ``_sane_column``；非法项丢弃——防幻觉产出污染结果。
    """
    from app.services.llm.parse import (
        extract_numeric_field,
        extract_str_field,
        normalize_period,
        parse_json_object,
    )

    obj = parse_json_object(raw)
    if obj is None:
        return None
    items: list[dict[str, Any]] = []
    raw_items = obj.get("items") or obj.get("candidates") or obj.get("annotations")
    if isinstance(raw_items, list):
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            key = extract_str_field(it, "key", "idx", "id")
            if key is None:
                continue
            entry: dict[str, Any] = {"key": key}
            is_measure = it.get("is_measure", it.get("is_metric", True))
            entry["is_measure"] = (
                is_measure is True or str(is_measure).lower() in ("true", "yes", "是", "1")
            )
            agg = str(it.get("agg") or it.get("aggregation") or "").upper().strip()
            if agg in AGG_ENUM:
                entry["agg"] = agg
            table = extract_str_field(it, "table", "source_table")
            if table:
                entry["table"] = table.strip()[:256]
            period = normalize_period(extract_str_field(it, "period", "granularity", "grain"))
            if period:
                entry["period"] = period
            confidence = extract_numeric_field(
                it, "confidence", "score", "prob", min_value=0.0, max_value=1.0
            )
            if confidence is not None:
                entry["confidence"] = confidence
            reason = extract_str_field(it, "reason", "note", "explanation")
            if reason:
                entry["reason"] = reason
            items.append(entry)
    missed: list[dict[str, Any]] = []
    raw_missed = obj.get("missed") or obj.get("missed_measures") or obj.get("extra")
    if isinstance(raw_missed, list):
        for mt in raw_missed:
            if not isinstance(mt, dict):
                continue
            column = extract_str_field(mt, "column", "measure_column", "col")
            agg = str(mt.get("agg") or mt.get("aggregation") or "").upper().strip()
            if not _sane_column(column) or agg not in AGG_ENUM:
                continue
            entry: dict[str, Any] = {"column": column.strip(), "agg": agg}
            alias = extract_str_field(mt, "alias", "name")
            if alias:
                entry["alias"] = alias.strip()[:128]
            table = extract_str_field(mt, "table", "source_table")
            if table:
                entry["table"] = table.strip()[:256]
            confidence = extract_numeric_field(
                mt, "confidence", "score", "prob", min_value=0.0, max_value=1.0
            )
            if confidence is not None:
                entry["confidence"] = confidence
            missed.append(entry)
    if not items and not missed:
        return None
    result: dict[str, Any] = {"items": items, "missed": missed}
    period = normalize_period(extract_str_field(obj, "period", "granularity"))
    if period:
        result["period"] = period
    return result


async def llm_validate_measures(
    db: Any,
    full_sql: str,
    measures: list[dict[str, Any]],
    tables: list[str],
) -> dict[str, Any] | None:
    """LLM 校验规则解析出的度量清单（单条 auto-suggest 默认全量校验）。

    对每个规则度量做封闭选择（is_measure / agg / table / period）并扫描漏检
    度量；失败返回 ``None``（上层保持规则结果不动）。

    Args:
        db: 异步会话（LLM 客户端构建）。
        full_sql: 完整原始 SQL 脚本。
        measures: 规则解析出的度量（``SqlProfile.measures`` 结构）。
        tables: 规则解析出的源表集合（LLM 只能从其中选择表）。

    Returns:
        ``parse_sql_validation_result`` 结构（items/missed/period 可选）；
        失败返回 ``None``。
    """
    try:
        from app.services.llm.config_service import LlmConfigService
    except Exception:  # noqa: BLE001 - 校验层不可用不影响规则结果
        return None
    try:
        client = await LlmConfigService(db).build_client()
        if not getattr(client, "enabled", False):
            return None
        rows = "\n".join(
            f"- key={i} | 度量列={m.get('column') or '-'} | 聚合={m.get('agg') or '派生'} | "
            f"别名={m.get('alias') or '-'} | 源表={m.get('table') or '-'}"
            for i, m in enumerate(measures)
        )
        tables_txt = ", ".join(tables) if tables else "（未识别）"
        prompt = (
            "下面是一段完整的 SQL 脚本（可能含多条语句/建表/注释等）：\n"
            f"{full_sql}\n\n"
            "已用程序（sqlglot 规则解析）从中提取出以下候选度量列，请逐项校验其正确性"
            "（key 为稳定序号，只能对已有项判断，不能新增/改名已有项）：\n"
            f"{rows}\n\n"
            "对每个候选判断：\n"
            "1. is_measure：它真的是一个业务度量指标吗？（true/false；分组键/常量/普通"
            "业务键不是度量）\n"
            "2. agg：聚合方式，只从 "
            f"{sorted(AGG_ENUM)} 选（程序可能归一错了，请纠正；派生表达式选最接近的）\n"
            "3. table：它来自哪个源表？只从解析出的源表里选："
            f"{tables_txt}（不要发明源表）\n"
            "4. period：统计周期，只从 day|week|month|quarter|year|hour 选\n"
            "5. confidence：0 到 1 的小数，你对以上判断的确信度\n"
            "另外请扫描整个 SQL：程序可能**漏掉**了某些度量列（尤其是 COALESCE 包裹、"
            "CASE 条件聚合、嵌套子查询里的聚合）。在 missed 字段列出你发现但程序漏掉的"
            "度量（column/agg/alias，agg 只从上述枚举选，不要列分组键/常量）。\n"
            "只返回 JSON（不要解释、不要 Markdown 代码块）："
            '{"items": [{"key": "0", "is_measure": true, "agg": "SUM", "table": "ods.t",'
            ' "period": "day", "confidence": 0.9, "reason": "一句话依据"}],'
            ' "missed": [{"column": "cnt", "agg": "COUNT", "alias": "visit_cnt",'
            ' "confidence": 0.8}], "period": "day"}'
        )
        resp = await client.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2048,
        )
        raw = (resp.get("content") or "").strip()
        return parse_sql_validation_result(raw)
    except Exception:  # noqa: BLE001 - 校验层任何异常保持规则结果不动
        logger.warning("SQL 解析 LLM 校验失败，保持规则结果", exc_info=True)
        return None


def merge_validation(
    measures: list[dict[str, Any]],
    validation: dict[str, Any],
    tables: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """一致性仲裁：LLM 校验结果合并回规则度量清单（纯确定性算法）。

    「每次业务结果一致」的实现——无论 LLM 输出顺序/个别判断怎么漂，最终清单
    在此层收敛：

    - **聚合纠正**：仅当 LLM 值在注册枚举白名单且置信度 ≥0.7 且与规则不同才采纳；
      否则保留规则（规则解析已过枚举白名单，防幻觉）。
    - **表纠正**：LLM 选的表必须在规则解析出的源表集合内（防幻觉出错误表）。
    - **is_measure=false**：高置信度（≥0.7）才移出度量；低置信度保守保留并标记
      需人工确认（规则说有就保留）。
    - **漏检补充**：LLM 的 missed 项过列名合法性 + 聚合白名单后才加入；别名与
      既有度量冲突则跳过（防重复）。
    - **低置信度**：``needs_review`` 标记（前端提示人工核对，不静默落库）。

    Returns:
        ``(合并后的度量清单, 变更摘要)``。
        变更摘要含 ``agg_corrected``/``dropped``/``added``/``needs_review``/
        ``period_override``（供前端展示校验结果）。
    """
    items_by_key = {str(it["key"]): it for it in validation.get("items", [])}
    out: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "agg_corrected": [],
        "dropped": [],
        "added": [],
        "needs_review": [],
        "period_override": validation.get("period"),
    }
    for i, m in enumerate(measures):
        item = items_by_key.get(str(i))
        if item is None:
            out.append(m)
            continue
        conf = item.get("confidence")
        if not item.get("is_measure", True):
            if conf is None or conf >= _CONFIDENCE_ACCEPT:
                summary["dropped"].append(
                    {"column": m.get("column"), "agg": m.get("agg"),
                     "reason": item.get("reason")}
                )
                continue
            summary["needs_review"].append(
                {"column": m.get("column"), "reason": "LLM 判定非度量但置信度低"}
            )
        corrected = False
        agg = item.get("agg")
        if agg and agg in AGG_ENUM and agg != m.get("agg") and (
            conf is None or conf >= _CONFIDENCE_ACCEPT
        ):
            m = dict(m)
            m["agg"] = agg
            corrected = True
            summary["agg_corrected"].append(
                {"column": m.get("column"), "from": None, "to": agg}
            )
        table = item.get("table")
        if table and table in tables and table != m.get("table"):
            m = dict(m)
            m["table"] = table
            m.pop("llm_corrected_table", None)
            corrected = True
        if corrected:
            m["llm_validated"] = True
            m["llm_confidence"] = conf
        elif conf is not None and conf < _CONFIDENCE_ACCEPT:
            m = dict(m)
            m["llm_validated"] = True
            m["llm_confidence"] = conf
            summary["needs_review"].append(
                {"column": m.get("column"), "reason": "LLM 校验置信度低，建议人工核对"}
            )
        out.append(m)
    # 漏检补充（LLM 扫描出的规则漏掉度量）
    existing_aliases = {
        (str(m.get("alias")) or str(m.get("column"))).lower() for m in out
    }
    for missed in validation.get("missed", []):
        column = str(missed.get("column") or "").strip()
        agg = missed.get("agg")
        alias = str(missed.get("alias") or "").strip() or None
        if not _sane_column(column) or agg not in AGG_ENUM:
            continue
        if alias and alias.lower() in existing_aliases:
            continue
        entry: dict[str, Any] = {
            "column": column,
            "agg": agg,
            "llm_added": True,
            "llm_confidence": missed.get("confidence"),
        }
        if alias:
            entry["alias"] = alias
        table = missed.get("table")
        if table and table in tables:
            entry["table"] = table
        elif tables:
            entry["table"] = tables[0]
        out.append(entry)
        existing_aliases.add((alias or column).lower())
        summary["added"].append(
            {"column": column, "agg": agg, "alias": alias}
        )
    return out, summary
