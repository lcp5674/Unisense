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

_SPLIT_RE = re.compile(r"[^a-z0-9_]+")
_GRAIN_TOKENS = {"day", "month", "week", "quarter", "year", "hour"}
_UNIT_TOKENS = {"yuan", "cent", "usd", "cny", "fen"}


def _normalize(text: str) -> str:
    return (text or "").strip().lower()


def _tokens(text: str) -> set[str]:
    return {t for t in _SPLIT_RE.split(_normalize(text)) if t}


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
    """口径文本结构相似度（AST 归一 + embedding 的确定性退化实现）。"""
    return _edit_ratio(a, b)


def lineage_overlap(sources_a: list[str], sources_b: list[str]) -> float:
    """源表集合 Jaccard（同源越多越可能冲突）。"""
    return _jaccard(set(sources_a or []), set(sources_b or []))


def composite_score(name_sim: float, def_sim: float, lineage_ov: float) -> float:
    return 0.4 * name_sim + 0.4 * def_sim + 0.2 * lineage_ov


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


def _grain_unit_diff(code_a: str, code_b: str) -> bool:
    ta, tb = _tokens(code_a), _tokens(code_b)
    grains = (ta & _GRAIN_TOKENS) | (tb & _GRAIN_TOKENS)
    units = (ta & _UNIT_TOKENS) | (tb & _UNIT_TOKENS)
    return bool(grains or units) and ta != tb


def _borderline(def_sim: float, composite: float) -> bool:
    """语义补位触发区：词法未达软冲突阈值（<0.85），但已明显接近（>0.45）。

    落在阈值附近的「同义异名 / 表述差异大」口径，词法判定易漏报，交由 LLM 补位。
    """
    return (0.45 <= def_sim < 0.85) or (0.5 <= composite < 0.85)


def is_borderline_match(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    """供服务层判断是否需要对某对口径发起 LLM 语义补位。"""
    cand_def = candidate.get("definition", "")
    ext_def = existing.get("definition", "")
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
    cand_def = candidate.get("definition", "")
    cand_src = candidate.get("source_tables", []) or []
    ext_code = existing.get("metric_code") or existing.get("code") or ""
    ext_domain = existing.get("domain", "")
    ext_def = existing.get("definition", "")
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
        if _grain_unit_diff(cand_code, ext_code):
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
