"""dp 调度血缘 LLM 协议（共识确认 / 兜底提炼 + JSON 解析容错）。

对齐 `spec/dp-lineage-ingest/plan.md` §5（LLM 协议）与决策 D5/D6：
- 复杂节点：sqlglot 结果交 LLM 共识确认（agree/disagree），分歧建待抉择单
- 失败节点：LLM 兜底提炼数据流转（confidence=low，仅作参考，用户采纳才落正式血缘）
- 无法提炼：返回空流转（ok=False），建 unparseable 单展示原文供手动配置

本模块为纯函数/无副作用（不实际调用 LLM、不连 DB）；LLM 调用由调用方注入
``llm_chat`` 可调用对象（对齐 LlmClient.chat 的 messages/返回 dict 语义），
便于 mock 测试与成本/熔断控制。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

#: 共识确认 system prompt：只报告与 sqlglot 的差异，禁止复述；输出合法 JSON。
CONFIRM_SYSTEM_PROMPT = (
    "你是数据血缘校验助手。给定一段 SQL 与一个 sqlglot 解析出的血缘结果，"
    "请判断 sqlglot 的血缘提取是否正确。\n"
    "规则：\n"
    "1. 只报告与给定血缘结果的【差异】（漏掉的边、错误的边），不要复述已给出的边。\n"
    "2. 表名格式必须为 库名.表名（无库名的按 SQL 中 USE 上下文推断，推断不出给提示）。\n"
    "3. 字段映射格式 [源表.源列, 目标表.目标列]；表达式派生列（count/sum/concat 等）"
    "没有直接字段来源，不要臆造字段映射。\n"
    "4. 若 sqlglot 结果完整正确，输出 {\"agree\": true}。\n"
    "5. 若无法判断（SQL 语义不明确），输出 {\"agree\": false, \"reason\": \"无法判断：...\"}。\n"
    "只输出一个合法 JSON 对象，不要输出任何其他文字。"
)

#: 兜底提炼 system prompt：sqlglot 解析失败时提炼真实数据流转。
FALLBACK_SYSTEM_PROMPT = (
    "你是数据血缘提取助手。给定一段 SQL（可能含 set/use/drop 前缀、Hive/Spark 方言、"
    "多语句或残缺片段），请尽力提炼其中真实存在的数据流转。\n"
    "规则：\n"
    "1. 只提炼确实能看出的源表与目标表（库名.表名，无库名按 USE 上下文推断）。\n"
    "2. 字段映射格式 [源表.源列, 目标表.目标列]；聚合/计算列无直接字段来源则省略。\n"
    "3. 明显临时表（tmp/temp/_bak/adhoc）不列入目标表。\n"
    "4. 若 SQL 无法理解、无法提炼任何可信流转，输出 {\"target_tables\": [], "
    "\"source_tables\": [], \"field_mappings\": [], \"note\": \"无法提炼原因\"}。\n"
    "5. 不确定的流转宁缺毋滥，禁止臆造。\n"
    "只输出一个合法 JSON 对象，不要输出任何其他文字。"
)


class DpSyncLlmError(Exception):
    """LLM 协议错误（空输出 / JSON 解析失败 / 字段缺失）。"""


@dataclass
class ConfirmVerdict:
    """共识确认结果。"""

    agree: bool
    missing_edges: list[dict] = field(default_factory=list)
    wrong_edges: list[dict] = field(default_factory=list)
    reason: str | None = None


@dataclass
class FallbackFlow:
    """兜底提炼结果（仅作参考，采纳才落正式血缘）。"""

    target_tables: list[str] = field(default_factory=list)
    source_tables: list[str] = field(default_factory=list)
    field_mappings: list[list[str]] = field(default_factory=list)
    note: str | None = None

    @property
    def ok(self) -> bool:
        """提炼出可信流转（目标表非空）才算成功。"""
        return bool(self.target_tables)


def build_confirm_messages(sql: str, sqlglot_json: dict) -> list[dict[str, str]]:
    """构造共识确认请求消息。"""
    user_content = (
        f"SQL：\n```sql\n{sql}\n```\n\nsqlglot 提取的血缘结果（JSON）：\n"
        f"{json.dumps(sqlglot_json, ensure_ascii=False, indent=2)}\n\n"
        "请校验并只输出差异。"
    )
    return [
        {"role": "system", "content": CONFIRM_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def build_fallback_messages(sql: str) -> list[dict[str, str]]:
    """构造兜底提炼请求消息。"""
    return [
        {"role": "system", "content": FALLBACK_SYSTEM_PROMPT},
        {"role": "user", "content": f"SQL：\n```sql\n{sql}\n```\n请提炼数据流转。"},
    ]


def extract_json(text: str) -> dict:
    """从 LLM 输出文本中提取 JSON 对象（容错 code fence/前后噪音/截断）。

    Raises:
        DpSyncLlmError: 找不到合法 JSON 对象或解析失败。
    """
    if not text or not text.strip():
        raise DpSyncLlmError("LLM 返回空内容")
    # 剥离 ```json ... ``` code fence
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    candidate = fence.group(1) if fence else text
    # 找第一个 { 到最后一个 } 之间的子串（容忍前后说明文字）
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise DpSyncLlmError(f"LLM 输出中未找到 JSON 对象：{text[:200]}")
    try:
        data = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        raise DpSyncLlmError(f"LLM 输出 JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise DpSyncLlmError("LLM 输出 JSON 不是对象")
    return data


def parse_confirm_response(text: str) -> ConfirmVerdict:
    """解析共识确认响应为裁决。

    宽容解析：布尔兼容字符串；agree 缺失时按 missing/wrong 边非空推断不同意。
    T1：未显式给 agree（LLM 规则 5「无法判断」漏发 ``agree:false``，或返回空
    对象）**一律按不同意**——共识确认是入库门禁，缺省方向放行（sqlglot 边直接
    入库 + 写 auto-accept 记忆、后续轮次永不复查）会让「无法判断」被当成「同意」，
    安全方向是建分歧单交人工而非静默放行。
    """
    data = extract_json(text)
    agree_raw = data.get("agree", None)
    agree = _as_bool(agree_raw)
    if agree is None:
        # 未显式给 agree：无论是否有差异报告都按不同意（保守建分歧单）。
        # reason 保留给调用方展示 LLM 的「无法判断」说明。
        agree = False
    return ConfirmVerdict(
        agree=bool(agree),
        missing_edges=[e for e in (data.get("missing_edges") or []) if isinstance(e, dict)],
        wrong_edges=[e for e in (data.get("wrong_edges") or []) if isinstance(e, dict)],
        reason=data.get("reason"),
    )


def parse_fallback_response(text: str) -> FallbackFlow:
    """解析兜底提炼响应。"""
    data = extract_json(text)
    return FallbackFlow(
        target_tables=[str(t) for t in (data.get("target_tables") or [])],
        source_tables=[str(s) for s in (data.get("source_tables") or [])],
        field_mappings=[
            [str(x) for x in pair]
            for pair in (data.get("field_mappings") or [])
            if isinstance(pair, list)
        ],
        note=data.get("note"),
    )


def edges_to_json(
    table_edges: list, field_edges: list
) -> dict:
    """把 sqlglot 表级/字段级边序列化为给 LLM 的 JSON（忽略非 dataclass 防御）。"""
    return {
        "table_edges": [
            {"source": e.source, "target": e.target} for e in table_edges
        ],
        "field_edges": [
            {
                "source_table": e.source_table,
                "source_column": e.source_column,
                "target_table": e.target_table,
                "target_column": e.target_column,
                "expression": e.expression,
                "degraded": e.degraded,
            }
            for e in field_edges
        ],
    }


def _as_bool(value: object) -> bool | None:
    """宽松布尔解析：bool 原样；字符串 'true'/'false' 转布尔；其余 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.strip().lower() in ("true", "yes", "1"):
            return True
        if value.strip().lower() in ("false", "no", "0"):
            return False
    return None
