"""SQL 智能推断的字典驱动能力（单位/粒度关键词 + 平台维度匹配）。

设计动机（2026-08-28 用户审查）：
1. **单位/粒度不硬编码**——推断关键词由 ``system_dict`` 字典项驱动：内置默认关键词
   表（与 ``scripts/seed_domains_dicts.py`` 种子对齐）为兜底，字典项 ``extra.
   infer_keywords`` 可覆盖/新增（系统配置字典管理维护即可，无需发版）。
2. **粒度打通业务实体**——``granularity`` 字典含 8 时间 + 9 业务实体粒度（医生/科室/
   病种…），GROUP BY 业务实体键升级为**粒度维度**（组合粒度唯一性构成，2026-08-28
   方案 B：全部业务实体键升级，不再要求唯一命中）。
3. **维度关联平台维度管理**——GROUP BY 非时间键推断出的维度列，与维度管理
   （``Dimension`` 目录）的 ``dim_code``/``name`` 匹配，命中回填平台维度编码，血缘
   「指标↔维度」边直挂已治理维度节点。

本模块为纯函数 + 轻量异步加载器，供 ``auto_fill``（单条）与 ``sql_split``（批量）
共用，保证两路径语义一致。
"""

from __future__ import annotations

import re
from typing import Any

# 时间粒度 code（字典 granularity 的时间子集，与 seed 对齐）
TIME_GRAIN_CODES: frozenset[str] = frozenset(
    {"minute", "hour", "day", "week", "month", "quarter", "year", "realtime"}
)

# ---- 内置默认推断关键词（兜底；DB 字典 extra.infer_keywords 可覆盖） ----
# 对齐 scripts/seed_domains_dicts.py 的 unit / granularity 种子。匹配用子串包含，
# 关键词须用小写英文/中文；**顺序敏感**：金额量级（CNY_WAN/CNY_YI）先于 CNY，
# 人（PERSON）先于计数（TIMES），否则列名同时含人+计数语义（如 active_doctor_cnt）
# 会被计数兜底误判。
DEFAULT_UNIT_KEYWORDS: dict[str, list[str]] = {
    # 金额量级（先于 CNY：列名含「万元/亿」时优先）
    "CNY_WAN": ["万元", "万", "wan", "wanyuan"],
    "CNY_YI": ["亿元", "亿", "yi", "yiyuan"],
    # 币种金额
    "CNY": [
        "amount", "gmv", "price", "cost", "revenue", "sales", "fee",
        "income", "expense", "金额", "单价", "总价", "余额", "收入", "成本", "费用",
    ],
    "USD": ["usd", "美元"],
    "EUR": ["eur", "欧元"],
    # 比率
    "PERCENT": ["rate", "ratio", "pct", "percent", "proportion", "率", "占比", "百分比"],
    # 笔数（先于 TIMES：订单/笔语义优先于通用计数）
    "ORDER": ["order", "订单", "笔数", "单量"],
    # 人/用户（先于 TIMES：doctor_cnt 应判「人」而非「次」）
    "PERSON": [
        "user", "customer", "member", "account", "patient", "doctor",
        "physician", "nurse", "doctor_id", "patient_id",
        "人数", "用户", "患者", "医生", "医师", "护士", "人次",
    ],
    # 通用计数
    "TIMES": ["cnt", "count", "num", "qty", "quantity", "次数", "数量", "频次"],
    # 时长/时间单位
    "DAY": ["day", "天"],
    "HOUR": ["hour", "小时"],
    "MINUTE": ["minute", "second", "duration", "分钟", "秒", "时长"],
}

# 粒度推断关键词（时间 + 业务实体，对齐 granularity 种子 17 项）
DEFAULT_GRAIN_KEYWORDS: dict[str, list[str]] = {
    # 时间粒度（沿用原 _GRAIN_TOKENS 语义：token 子串命中）
    "minute": ["minute", "_min", "分钟"],
    "hour": ["hour", "_hour", "小时"],
    "day": ["dt", "date", "day", "_day", "天"],
    "week": ["week", "wk", "周"],
    "month": ["month", "mo", "月"],
    "quarter": ["quarter", "qtr", "季"],
    "year": ["year", "yr", "年"],
    "realtime": ["realtime", "实时"],
    # 业务实体粒度（医疗场景：GROUP BY 业务主体 → 实体粒度）
    "register": ["register", "挂号"],
    "visit": ["visit", "就诊"],
    "patient": ["patient", "患者"],
    "doctor": ["doctor", "physician", "医生", "医师", "大夫"],
    # 医院（2026-08-28 组合粒度补：用户示例「按月+医院统计订单金额」——hospital
    # 是医疗最基础实体粒度，字典种子同步补 hospital 项）
    "hospital": ["hospital", "医院"],
    "department": ["dept", "department", "科室", "部门"],
    "disease": ["disease", "icd", "病种", "疾病", "诊断"],
    "prescription": ["prescription", "处方"],
    "pharmacy": ["pharmacy", "药房"],
    "yb_settle": ["yb_settle", "医保", "结算"],
}

# 单位推断类别优先级（匹配顺序 = 数组顺序；金额量级 > 币种金额 > 比率 > 笔数 >
# 人 > 计数 > 时长 > 日/小时）
_UNIT_PRIORITY: tuple[str, ...] = (
    "CNY_WAN", "CNY_YI", "CNY", "USD", "EUR",
    "PERCENT", "ORDER", "PERSON", "TIMES", "DAY", "HOUR", "MINUTE",
)


def extract_grain_and_dims(
    group_by: list[str],
    grain_kw: dict[str, list[str]] | None = None,
) -> tuple[str, list[str], list[str]]:
    """从 GROUP BY 键提取 ``(主粒度, 粒度维度, 普通维度)``。

    组合粒度（2026-08-28 方案 B，用户确认「业务实体键全部升级为粒度维度」）：
    粒度 = 唯一性维度集合——「按月+医院统计订单总金额」的完整粒度是 (month, hospital)，
    其中主粒度（时间频率语义）表达「什么时候的」，粒度维度（业务实体）表达「谁的」。
    规则（单条/批量共用，保证两路径一致）：
    1. **时间粒度优先**：首个命中时间粒度关键词的键 → 主粒度；**全部**命中业务
       实体粒度关键词的键 → 粒度维度（不再要求唯一命中——多实体键同为主粒度下
       的组合粒度构成）；其余键 → 普通维度（可下钻）。
    2. **无时间粒度**：主粒度兜底 ``day``；业务实体键仍全部升级为粒度维度；
       其余键 → 普通维度。
    3. **兜底**：无任何命中 → ``day``，粒度维度空，全部键为普通维度。

    Args:
        group_by: GROUP BY 列名列表（原始 SQL 文本）。
        grain_kw: 粒度关键词映射（code → 关键词）；缺省用内置默认。

    Returns:
        ``(grain_code, grain_dims, normal_dims)``——``grain_code`` 为字典
        granularity 时间粒度 code，``grain_dims`` 为粒度维度（业务实体键，
        组合粒度唯一性构成），``normal_dims`` 为普通维度（可下钻）。
    """
    kw = grain_kw or DEFAULT_GRAIN_KEYWORDS
    group_by = [g for g in (group_by or []) if g and str(g).strip()]
    if not group_by:
        return "day", [], []
    grain: str = "day"
    time_found = False
    grain_dims: list[str] = []
    normal_dims: list[str] = []
    for g in group_by:
        gl = str(g).lower()
        # 时间粒度键 → 主粒度（首个命中决定）
        hit_time = False
        for code, kws in kw.items():
            if code in TIME_GRAIN_CODES and any(k in gl for k in kws):
                if not time_found:
                    grain = code
                    time_found = True
                hit_time = True
                break
        if hit_time:
            continue
        # 业务实体粒度键 → 粒度维度（全部升级，组合粒度唯一性构成）
        hit_entity = False
        for code, kws in kw.items():
            if code not in TIME_GRAIN_CODES and any(k in gl for k in kws):
                grain_dims.append(g)
                hit_entity = True
                break
        if not hit_entity:
            normal_dims.append(g)
    return grain, grain_dims, normal_dims


def infer_unit_from_meta(
    meta: dict[str, Any],
    unit_kw: dict[str, list[str]] | None = None,
) -> str | None:
    """按字典关键词推断单位 code（列元数据/名称信号）。

    信号源：``type/comment/name/label`` 任一包含关键词即命中；按 ``_UNIT_PRIORITY``
    顺序匹配，命中即返回字典 unit code。

    Args:
        meta: 列元数据（含 type/comment/name/label）。
        unit_kw: 单位关键词映射；缺省用内置默认。

    Returns:
        字典 unit code（如 ``PERSON``/``CNY_WAN``）或 None（无法推断）。
    """
    kw = unit_kw or DEFAULT_UNIT_KEYWORDS
    hay = " ".join(str(meta.get(k, "")) for k in ("type", "comment", "name", "label")).lower()
    if not hay:
        return None
    for code in _UNIT_PRIORITY:
        for k in kw.get(code, ()):
            if k in hay:
                return code
    return None


def match_platform_dimensions(
    keys: list[str],
    platform_dims: list[dict[str, str]],
) -> list[str]:
    """GROUP BY 非时间键 → 平台维度编码匹配。

    命中规则（由宽到严，防止误匹配）：
    1. 列名与 ``dim_code`` 精确相等（``hospital`` == ``hospital``）；
    2. ``dim_code`` 是列名子串（``doctor`` ⊂ ``doctor_id``）；
    3. 列名拆词 token（≥4 字符）是 ``dim_code`` 前缀（``hosp`` → ``hospital``）。

    未命中保留原始列名（前端「关联维度」tags 输入仍可编辑；血缘可挂未采集维度
    节点，与既有 dimensions 语义一致）。

    Args:
        keys: 推断出的维度列名列表。
        platform_dims: 平台维度列表（``[{"dim_code", "name"}]``，PUBLISHED）。

    Returns:
        与 keys 等长的维度编码列表（命中处替换为平台 ``dim_code``）。
    """
    if not keys or not platform_dims:
        return list(keys)
    out: list[str] = []
    for key in keys:
        kl = str(key).strip().lower()
        if not kl:
            out.append(key)
            continue
        matched: str | None = None
        for dim in platform_dims:
            dc = str(dim.get("dim_code") or "").strip().lower()
            if not dc:
                continue
            if kl == dc or dc in kl:
                matched = str(dim["dim_code"])
                break
            # 列名拆词前缀匹配（如 hosp_code → hospital）
            tokens = [t for t in re.split(r"[^a-z0-9]+", kl) if len(t) >= 4]
            if any(dc.startswith(t) for t in tokens):
                matched = str(dim["dim_code"])
                break
        out.append(matched if matched is not None else key)
    return out


async def load_infer_dicts(db: Any) -> dict[str, dict[str, list[str]]]:
    """从 system_dict 加载 unit/granularity 推断关键词。

    字典项 ``extra.infer_keywords``（字符串数组）覆盖内置默认；仅 active 项生效。
    DB 不可用/异常 → 返回内置默认（绝不阻断推断）。

    Args:
        db: 异步会话。

    Returns:
        ``{"unit": {code: [kw, ...]}, "granularity": {code: [kw, ...]}}``。
    """
    base = {
        "unit": {k: list(v) for k, v in DEFAULT_UNIT_KEYWORDS.items()},
        "granularity": {k: list(v) for k, v in DEFAULT_GRAIN_KEYWORDS.items()},
    }
    if db is None:
        return base
    try:
        from sqlalchemy import select

        from app.models.system_dict import SystemDict

        rows = (
            await db.execute(
                select(SystemDict.dict_type, SystemDict.code, SystemDict.extra).where(
                    SystemDict.dict_type.in_(("unit", "granularity")),
                    SystemDict.status == "active",
                )
            )
        ).all()
        for dict_type, code, extra in rows:
            kws = (extra or {}).get("infer_keywords")
            if isinstance(kws, list) and kws:
                cleaned = [str(k).strip().lower() for k in kws if str(k).strip()]
                if cleaned:
                    base[dict_type][code] = cleaned
        return base
    except Exception:  # noqa: BLE001 - 字典加载失败仅降级默认，不阻断推断
        return base


async def load_platform_dimensions(
    db: Any, domain_code: str | None = None
) -> list[dict[str, str]]:
    """加载平台维度目录（PUBLISHED，可选按域过滤）供 SQL 推断维度匹配。

    Args:
        db: 异步会话。
        domain_code: 业务域编码（缺省加载全部已发布维度）。

    Returns:
        ``[{"dim_code", "name"}]``；DB 异常返回空列表（推断不阻断）。
    """
    if db is None:
        return []
    try:
        from sqlalchemy import select

        from app.models.dimension import Dimension

        conds = [Dimension.status == "PUBLISHED", Dimension.deleted_at.is_(None)]
        if domain_code:
            conds.append(Dimension.domain == domain_code)
        rows = (
            await db.execute(
                select(Dimension.dim_code, Dimension.name)
                .where(*conds)
                .order_by(Dimension.id.asc())
            )
        ).all()
        return [{"dim_code": r[0], "name": r[1]} for r in rows]
    except Exception:  # noqa: BLE001 - 维度加载失败降级空列表，推断不阻断
        return []
