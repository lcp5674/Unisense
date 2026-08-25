"""冲突相似度计算（TD §12.4 相似度模型，PRD 4.7.2）。

综合分 = 0.4 × name_similarity + 0.4 × definition_similarity + 0.2 × lineage_overlap

按 TD 容错约定，LLM embedding 不可用时退化为：编辑距离(SequenceMatcher) + Jaccard(分词)
+ 源表集合 Jaccard，全程本地计算、无外部依赖，保证硬冲突检测不依赖 LLM。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.models.conflict import ConflictType

#: 非字母数字字符作为分隔符（含下划线——修复前不含 `_`，带下划线编码整体成单 token，
#: 致 name_similarity 的 Jaccard 半腿失效、_GRAIN_TOKENS/_UNIT_TOKENS 交集恒空、
#: GRAIN_UNIT 分支从未被编码真正触发）
_SPLIT_RE = re.compile(r"[^a-z0-9]+")
#: CJK 字符区间：连续中文按字符 bigram 切分（P1-H 中文分词失效修复）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_GRAIN_TOKENS = {"day", "month", "week", "quarter", "year", "hour"}
_UNIT_TOKENS = {"yuan", "cent", "usd", "cny", "fen"}


def _normalize(text: str) -> str:
    return (text or "").strip().lower()


def _tokens(text: str) -> set[str]:
    """分词：ASCII/数字按分隔符切，连续中文按字符 bigram 切（P1-H）。

    修复前 ``_SPLIT_RE`` 把连续中文切成**单个 token**（如 "挂号人次" 整体），
    Jaccard 对中文名/口径基本失效、退化为编辑距离单腿。bigram 能捕捉
    "挂号人次"→{挂号,号人,人次} 的重叠，中文语义同义检测显著提升。
    """
    norm = _normalize(text)
    tokens = {t for t in _SPLIT_RE.split(norm) if t}
    for seq in _CJK_RE.findall(norm):
        if len(seq) >= 2:
            tokens.update(seq[i : i + 2] for i in range(len(seq) - 1))
        else:
            tokens.add(seq)
    return tokens


def _edit_ratio(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def name_similarity(a: str, b: str) -> float:
    """0.5 × 编辑距离比 + 0.5 × 分词 Jaccard。"""
    return 0.5 * _edit_ratio(a, b) + 0.5 * _jaccard(_tokens(a), _tokens(b))


def definition_similarity(a: str, b: str) -> float:
    """口径文本结构相似度（AST 归一 + embedding 的确定性退化实现）。

    P1-H 中文增强：编辑距离捕获字面改动，中文 bigram Jaccard 捕获语义重叠
    （修复前连续中文退化为单 token、Jaccard 失效，中文"同义异名/措辞差异"
    大面积漏检）。0.7/0.3 加权保证英文/数字口径行为基本不变（其 token
    Jaccard 与编辑距离高度相关），中文检测显著提升。
    """
    if not _normalize(a) and not _normalize(b):
        return 1.0 if a == b else 0.0
    edit = _edit_ratio(a, b)
    jac = _jaccard(_tokens(a), _tokens(b))
    return 0.7 * edit + 0.3 * jac


def lineage_overlap(sources_a: list[str], sources_b: list[str]) -> float:
    """源表集合 Jaccard（同源越多越可能冲突）。

    P1-E：双方均无源表时返回 0（无同源证据不抬高综合分）。修复前空集对空集
    返回 1.0，权重 0.2 系统性拉满综合分，配合 ``score>=0.85`` 分支易误报
    重复建设（两个无源表指标的 name/def 略接近即被抬到阈值以上）。
    """
    sa, sb = set(sources_a or []), set(sources_b or [])
    if not sa and not sb:
        return 0.0
    return _jaccard(sa, sb)


def composite_score(name_sim: float, def_sim: float, lineage_ov: float) -> float:
    return 0.4 * name_sim + 0.4 * def_sim + 0.2 * lineage_ov


#: 参与口径比对的要素（P1-D 口径要素归一）：维度/过滤/粒度/聚合/依赖/单位
#: 不参与对比会造成「维度不同但文本相同→误判重复建设」「过滤不同→语义漏检」。
_DEFINITION_FEATURE_KEYS: tuple[tuple[str, str], ...] = (
    ("dimensions", "维度"),
    ("filters", "过滤"),
    ("granularity", "粒度"),
    ("aggregation", "聚合"),
    ("dependencies", "依赖"),
    ("unit", "单位"),
)


def _definition_compare_text(d: dict[str, Any]) -> str:
    """口径比对富文本（P1-D 口径要素归一 + 向后兼容纯文本 definition）。

    优先从 ``definition_json`` 拼「口径 + 维度/过滤/粒度/聚合/依赖/单位」，
    使 ``def_sim`` 对这些关键要素差异敏感；无 ``definition_json``（如前端
    手动预检只传纯文本 definition）时退化用 ``definition``/``expression``。
    """
    defn = d.get("definition_json")
    if isinstance(defn, dict) and defn:
        parts: list[str] = []
        base = defn.get("definition") or defn.get("expression") or d.get("definition") or ""
        if base:
            parts.append(str(base))
        for key, label in _DEFINITION_FEATURE_KEYS:
            val = defn.get(key)
            if val not in (None, "", [], {}):
                parts.append(f"{label}={val}")
        return " ".join(parts)
    return str(d.get("definition") or d.get("expression") or "")


def _name_equivalent(
    code_a: str, code_b: str, syn_a: list[str] | None = None, syn_b: list[str] | None = None
) -> bool:
    """编码不同但互为同义词（P2-K）→ 名称语义等价。

    "gmv/成交总额/销售总额" 这类术语表同义词在冲突检测中零使用——检索已接线，
    冲突检测仍漏检语义等价。此处把度量目录/术语的同义词并入 name 比对：一方
    编码命中对方同义词集、或双方同义词集相交，即视为名称高度相近。
    """
    if not code_a or not code_b:
        return False
    if code_a == code_b:
        return True
    sa = {str(s).lower() for s in (syn_a or [])}
    sb = {str(s).lower() for s in (syn_b or [])}
    if code_a.lower() in sb or code_b.lower() in sa:
        return True
    return bool(sa & sb)


@dataclass
class ConflictDetection:
    conflict_type: ConflictType
    score: float
    existing_code: str
    existing_metric_id: int | None
    severity: str  # "hard" | "soft"
    block_publish: bool
    reason: str = ""
    llm_confirmed: bool = False


def _grain_unit_diff(
    code_a: str,
    code_b: str,
    defn_a: dict[str, Any] | None = None,
    defn_b: dict[str, Any] | None = None,
) -> bool:
    """统计周期/单位是否存在差异（P2-J：从口径定义补充，而非仅依赖编码 token）。

    修复前只查 metric_code token 中的 day/month/yuan 等词——编码不带粒度/单位词
    （如 ``sales_gmv_amount`` vs ``sales_gmv_amount_m``）则不识别，GRAIN_UNIT
    覆盖很窄。此处把 definition_json 的 granularity/unit 并入，只要两侧
    粒度或单位**确实不同**即命中（避免补入后恒真导致过度命中）。
    """
    ta, tb = _tokens(code_a), _tokens(code_b)
    grains_a = set(ta & _GRAIN_TOKENS)
    grains_b = set(tb & _GRAIN_TOKENS)
    units_a = set(ta & _UNIT_TOKENS)
    units_b = set(tb & _UNIT_TOKENS)
    if defn_a:
        g = str(defn_a.get("granularity") or "").lower()
        u = str(defn_a.get("unit") or "").lower()
        if g:
            grains_a.add(g)
        if u:
            units_a.add(u)
    if defn_b:
        g = str(defn_b.get("granularity") or "").lower()
        u = str(defn_b.get("unit") or "").lower()
        if g:
            grains_b.add(g)
        if u:
            units_b.add(u)
    grain_diff = bool(grains_a | grains_b) and grains_a != grains_b
    unit_diff = bool(units_a | units_b) and units_a != units_b
    return (grain_diff or unit_diff) and ta != tb


def _borderline(def_sim: float, composite: float) -> bool:
    """语义补位触发区：词法未达软冲突阈值（<0.85），但已明显接近（>0.45）。

    落在阈值附近的「同义异名 / 表述差异大」口径，词法判定易漏报，交由 LLM 补位。
    """
    return (0.45 <= def_sim < 0.85) or (0.5 <= composite < 0.85)


def is_borderline_match(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    """供服务层判断是否需要对某对口径发起 LLM 语义补位。"""
    cand_def = _definition_compare_text(candidate)
    ext_def = _definition_compare_text(existing)
    if not (cand_def and ext_def):
        return False
    cand_code = candidate.get("metric_code") or candidate.get("code") or ""
    ext_code = existing.get("metric_code") or existing.get("code") or ""
    cand_src = candidate.get("source_tables", []) or []
    ext_src = existing.get("source_tables", []) or []
    name_sim = name_similarity(cand_code, ext_code)
    def_sim = definition_similarity(cand_def, ext_def)
    lin_ov = lineage_overlap(cand_src, ext_src)
    score = composite_score(name_sim, def_sim, lin_ov)
    return _borderline(def_sim, score)


def detect_conflict(
    candidate: dict[str, Any],
    existing: dict[str, Any],
    llm_judge: Callable[[str, str], bool | None] | None = None,
) -> ConflictDetection | None:
    """比较候选口径与已存在口径，返回冲突判定（None 表示无冲突）。

    Args:
        candidate: 候选口径字典。
        existing: 已存在口径字典。
        llm_judge: 可选语义判定回调 ``(cand_def, ext_def) -> bool | None``。
            当词法判定无冲突但落入「补位触发区」时调用，返回 ``True`` 则升级为
            语义同义软冲突（``llm_confirmed=True``）；返回 ``None`` 弃权保持无冲突。
    """
    cand_code = candidate.get("metric_code") or candidate.get("code") or ""
    cand_domain = candidate.get("domain", "")
    cand_def = _definition_compare_text(candidate)
    cand_src = candidate.get("source_tables", []) or []
    ext_code = existing.get("metric_code") or existing.get("code") or ""
    ext_domain = existing.get("domain", "")
    ext_def = _definition_compare_text(existing)
    ext_src = existing.get("source_tables", []) or []
    ext_id = existing.get("metric_id") or existing.get("id")
    cand_id = candidate.get("metric_id") or candidate.get("id")

    # 自我引用防御：候选与现有携带同一指标行 ID（同一条真实指标）→ 不构成冲突。
    # 指标与自身比对（无论域/定义如何）永远不该产出冲突——否则仲裁联动会
    # 把"落败方"（=胜方自身）作废，导致数据被误删（TD §12.4 自我冲突事故根因）。
    if cand_id is not None and ext_id is not None and cand_id == ext_id:
        return None

    # PII 特殊路由：含 PII 且未授权 → 不进普通仲裁，转交 governance.pii_review
    if candidate.get("has_pii") and not candidate.get("pii_authorized"):
        return ConflictDetection(
            conflict_type=ConflictType.PII,
            score=1.0,
            existing_code=ext_code,
            existing_metric_id=ext_id,
            severity="hard",
            block_publish=True,
            reason="含 PII 口径在未授权域被引用，转交 governance.pii_review",
        )

    name_sim = name_similarity(cand_code, ext_code)
    # P2-K：术语/度量同义词等价（gmv↔成交总额）→ 名称视为高度相近，避免语义漏检
    if _name_equivalent(
        cand_code,
        ext_code,
        candidate.get("synonyms"),
        existing.get("synonyms"),
    ):
        name_sim = max(name_sim, 0.95)
    def_sim = definition_similarity(cand_def, ext_def)
    lin_ov = lineage_overlap(cand_src, ext_src)
    score = composite_score(name_sim, def_sim, lin_ov)

    # ① 同名不同义（硬冲突，最高优先级，阻断发布）
    if cand_code and cand_code == ext_code and (cand_domain != ext_domain or def_sim < 0.85):
        return ConflictDetection(
            conflict_type=ConflictType.SAME_NAME_DIFF_DEF,
            score=round(score, 4),
            existing_code=ext_code,
            existing_metric_id=ext_id,
            severity="hard",
            block_publish=True,
            reason="同名口径定义/域不同，须协商或裁决后方可发布",
        )

    # ①' 口径版本冲突（VERSION_CONFLICT，软冲突，P0-C）：同码同义同域但版本/修订差异。
    # 枚举与迁移早已声明，但 detect_conflict 从未产生该分支——同一口径被不同
    # Owner 修订/多版本并存的场景不覆盖。自我引用防御已在前置拦截（同 metric_id
    # 返回 None），此处仅当「不同指标行但同码」（历史数据/灰度版本并存）或候选
    # 未落库时命中，提示核对权威版本，不阻断发布。
    if cand_code and cand_code == ext_code and def_sim >= 0.85 and cand_domain == ext_domain:
        return ConflictDetection(
            conflict_type=ConflictType.VERSION_CONFLICT,
            score=round(max(score, def_sim), 4),
            existing_code=ext_code,
            existing_metric_id=ext_id,
            severity="soft",
            block_publish=False,
            reason="同名同义口径存在版本/修订差异，建议核对权威版本",
        )

    # ② 同义不同名（重复建设，建议合并，不阻断）：口径实质相同即判，不依赖综合分阈值
    if cand_code != ext_code and def_sim >= 0.85:
        return ConflictDetection(
            conflict_type=ConflictType.SAME_DEF_DIFF_NAME,
            score=round(max(score, def_sim), 4),
            existing_code=ext_code,
            existing_metric_id=ext_id,
            severity="soft",
            block_publish=False,
            reason="口径实质相同但命名各异，建议合并",
        )

    # 综合分高（命名相近 + 口径相近）也判为重复建设
    if score >= 0.85:
        return ConflictDetection(
            conflict_type=ConflictType.SAME_DEF_DIFF_NAME,
            score=round(score, 4),
            existing_code=ext_code,
            existing_metric_id=ext_id,
            severity="soft",
            block_publish=False,
            reason="口径实质相同但命名各异，建议合并",
        )

    # ③/④ 软冲突（粒度/单位或跨域同口径）
    if score >= 0.6:
        cand_defn = candidate.get("definition_json")
        ext_defn = existing.get("definition_json")
        if _grain_unit_diff(
            cand_code, ext_code,
            cand_defn if isinstance(cand_defn, dict) else None,
            ext_defn if isinstance(ext_defn, dict) else None,
        ):
            ctype = ConflictType.GRAIN_UNIT
            reason = "同名但统计周期/单位不同，提示消费方绑定正确粒度/单位"
        elif cand_domain != ext_domain:
            ctype = ConflictType.CROSS_DOMAIN_SAME_DEF
            reason = "跨域同口径异源，提示合并或明确权威源"
        else:
            ctype = ConflictType.GRAIN_UNIT
            reason = "口径相似，提示 Owner 关注"
        return ConflictDetection(
            conflict_type=ctype,
            score=round(score, 4),
            existing_code=ext_code,
            existing_metric_id=ext_id,
            severity="soft",
            block_publish=False,
            reason=reason,
        )

    # ---- LLM 语义补位 ----
    # 词法未达软冲突阈值，但落入「补位触发区」且双方均有定义：交由 LLM 判定语义同义。
    if llm_judge is not None and _borderline(def_sim, score) and cand_def and ext_def:
        try:
            same = llm_judge(cand_def, ext_def)
        except Exception:  # noqa: BLE001 - LLM 回调异常按弃权处理，不阻断
            same = None
        if same is True:
            return ConflictDetection(
                conflict_type=ConflictType.SAME_DEF_DIFF_NAME,
                score=round(max(score, def_sim), 4),
                existing_code=ext_code,
                existing_metric_id=ext_id,
                severity="soft",
                block_publish=False,
                reason="LLM 语义判定为同义口径（补位），建议合并",
                llm_confirmed=True,
            )

    return None
