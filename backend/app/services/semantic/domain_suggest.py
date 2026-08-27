"""业务域建议（FR-010 域建议增强）。

业务域是"组织归属"概念，SQL 语法里没有直接信号——本模块从两类已建资产**反向定位**域：

1. 采集目录：``DBCatalog.source_id → DataSource.domain``（表归属数据源，数据源绑定域）；
2. 挂载实体：``MetricMount.source_table → domain``（既有指标挂载表的归属域）。

均未命中（表未被采集——如用户粘贴大段 SQL 引用了平台外实体）→ **LLM 兜底**从
SQL/表名推断域（不可用/返回非法域编码时降级为无法建议）。

返回四态：
- ``unique``：唯一命中，直接预选该域（置信度 0.9/0.85）；
- ``multiple``：多域候选，列出让用户挑（跨域共用 DWD 层表是常态）；
- ``llm``：目录/挂载均未命中，LLM 推断出域（置信度封顶 0.7）；
- ``none``：无法建议（LLM 不可用或未给出合法域），用户手动选择。

对齐 spec FR-010/FR-011, plan.md D3（域是推断的前提而非结果——这里只做"建议"，
最终确认权始终在用户）。
"""

from __future__ import annotations

from typing import Any, TypedDict

from sqlalchemy import or_, select

from app.models.data_source import DataSource, DBCatalog
from app.models.metric_mount import MetricMount


class DomainCandidate(TypedDict):
    """域建议候选。"""

    code: str
    name: str
    confidence: float
    source: str  # catalog | mount | llm
    reason: str


async def _domain_map(db: Any) -> dict[str, str]:
    """域 code → name 扁平映射（含子域；供名称回填与 LLM 提示词用）。"""
    from app.services.subject_domain.service import SubjectDomainService

    tree = await SubjectDomainService(db).list_tree()

    out: dict[str, str] = {}

    def _walk(nodes: list[Any]) -> None:
        for node in nodes:
            out[node.code] = node.name
            _walk(node.children)

    _walk(tree)
    return out


def _candidate_tables(*, sql: str | None = None, source_table: str | None = None) -> list[str]:
    """收集候选表：显式源表 + SQL 解析出的源表（去重、保序）。"""
    tables: list[str] = []
    if source_table and source_table.strip():
        tables.append(source_table.strip())
    if sql and sql.strip():
        try:
            from app.services.semantic.sql_infer import parse_sql_profile

            parsed = parse_sql_profile(sql)
            for t in parsed.source_tables:
                if t and t not in tables:
                    tables.append(t)
        except Exception:
            pass  # SQL 解析失败不阻断（显式源表入参已收集）
    return tables


def _match_table(row_name: str, candidates: list[str]) -> str | None:
    """判断采集目录/挂载实体行是否命中任一候选表（库前缀容忍，仅比较末段）。"""
    row = row_name.strip().lower()
    row_last = row.split(".")[-1]
    for t in candidates:
        t_lower = t.strip().lower()
        t_last = t_lower.split(".")[-1]
        if row == t_lower or row_last == t_last:
            return t
    return None


async def _lookup_tables(db: Any, tables: list[str]) -> list[tuple[str, DomainCandidate]]:
    """采集目录 + 挂载实体反查，返回 ``[(候选表, 域候选)]``。"""
    matches: list[tuple[str, DomainCandidate]] = []

    # 1) 采集目录：DBCatalog.entity_name → DataSource.domain（表归属数据源，数据源绑定域）
    conds = []
    for t in tables:
        last = t.strip().lower().split(".")[-1]
        # 通配符转义（对齐 FR-035）：表名用户可控，含 %/_ 时防模糊放大
        esc = last.replace("/", "//").replace("%", "/%").replace("_", "/_")
        conds.append(DBCatalog.entity_name.like(f"%{esc}", escape="/"))
    if conds:
        stmt = (
            select(DBCatalog.entity_name, DataSource.domain)
            .join(DataSource, DataSource.source_id == DBCatalog.source_id)
            .where(DBCatalog.deleted_at.is_(None), or_(*conds))
            .limit(100)
        )
        rows = (await db.execute(stmt)).all()
        for entity_name, domain in rows:
            hit = _match_table(str(entity_name), tables)
            if hit and domain:
                matches.append(
                    (
                        hit,
                        DomainCandidate(
                            code=domain,
                            name="",
                            confidence=0.9,
                            source="catalog",
                            reason=f"采集目录中表 {entity_name} 归属数据源绑定域",
                        ),
                    )
                )

    # 2) 挂载实体：MetricMount.source_table → domain（既有派生指标挂载表的域）
    mount_stmt = select(MetricMount.source_table, MetricMount.domain).where(
        MetricMount.source_table.in_([t.strip().lower() for t in tables])
    )
    mount_rows = (await db.execute(mount_stmt)).all()
    for src_table, domain in mount_rows:
        hit = _match_table(str(src_table), tables)
        if hit and domain:
            matches.append(
                (
                    hit,
                    DomainCandidate(
                        code=domain,
                        name="",
                        confidence=0.85,
                        source="mount",
                        reason=f"挂载实体 {src_table} 的归属域",
                    ),
                )
            )

    return matches


def _aggregate(
    matches: list[tuple[str, DomainCandidate]], domain_map: dict[str, str]
) -> list[DomainCandidate]:
    """按域聚合：多表命中同一域取最高置信度；回填域名称；置信度降序。"""
    best: dict[str, DomainCandidate] = {}
    for _t, cand in matches:
        code = cand["code"]
        if code not in best or cand["confidence"] > best[code]["confidence"]:
            best[code] = dict(cand)
    for code, cand in best.items():
        cand["name"] = domain_map.get(code, code)
        cand["reason"] = f"{cand['reason']}（{cand['name']}）"
    return sorted(best.values(), key=lambda c: float(c["confidence"]), reverse=True)


async def _llm_suggest(
    db: Any,
    tables: list[str],
    sql: str | None,
    domain_map: dict[str, str],
    client: Any | None = None,
) -> DomainCandidate | None:
    """LLM 兜底：目录/挂载均未命中（表未被采集）时从 SQL/表名推断域。

    best-effort：LLM 不可用、超时或返回非法域编码一律返回 ``None``（降级为无法建议）。

    Args:
        db: 异步会话（client 缺省时构建用）。
        client: 复用已构建的 LLM 客户端（批量解析场景由调用方一次构建传入，
            避免每个兜底重复 DB 查询+解密）；None 时内部构建。
    """
    if not domain_map:
        return None
    try:
        from app.services.llm.config_service import LlmConfigService
        from app.services.llm.parse import parse_domain_infer_result

        if client is None:
            client = await LlmConfigService(db).build_client()
        if not getattr(client, "enabled", False):
            return None

        domain_list = "；".join(f"{code}: {name}" for code, name in sorted(domain_map.items()))
        context = (sql or "").strip() or f"源表：{'、'.join(tables)}"
        prompt = (
            "你是指标治理平台的数据资产专家。根据下面的指标定义 SQL 与源表，"
            "判断该指标最可能属于哪个业务域。\n\n"
            f"平台业务域清单（编码: 名称）：\n{domain_list}\n\n"
            f"指标定义 SQL：\n{context}\n\n"
            "请只返回 JSON（不要解释、不要 Markdown 代码块）："
            '{"domain_code": "最匹配的域编码", "confidence": 0.0~1.0, "reason": "一句话依据"}'
        )
        resp = await client.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=128,
            retries=1,  # 推断类调用收紧重试：限流重试大概率仍 429，避免叠加放大墙钟
        )
        raw = (resp.get("content") or "").strip()
        parsed = parse_domain_infer_result(raw)
        if not parsed:
            return None
        code = parsed["domain_code"]
        if code not in domain_map:
            return None
        return DomainCandidate(
            code=code,
            name=domain_map[code],
            # LLM 推断是"猜"，置信度封顶 0.7（区别于目录/挂载的硬绑定）
            confidence=min(max(float(parsed["confidence"]), 0.0), 0.7),
            source="llm",
            reason=parsed.get("reason") or "AI 依据 SQL/表名推断的业务域",
        )
    except Exception:
        return None  # LLM 不可用/超时 → 无法建议


async def suggest_domain(
    db: Any,
    *,
    sql: str | None = None,
    source_table: str | None = None,
    llm_budget: dict[str, int] | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """业务域建议主函数。

    Args:
        db: 异步会话。
        sql: 指标定义 SQL（大段 SQL 场景，可空）。
        source_table: 显式源表（可空；sql 与 source_table 至少提供一个）。
        llm_budget: 批级 LLM 调用预算 ``{"used", "limit"}``（批量解析场景传入，
            与度量提取/周期推断共用限额；None 表示不限额，如单条创建场景）。
            ``_llm_suggest`` 仅在目录/挂载未命中时调用，预算用于防止跨域脚本
            逐语句建议打满 LLM 配额（P1-1）。
        client: 复用已构建的 LLM 客户端（批量解析场景由调用方一次构建传入，
            避免每个兜底重复构建）；None 时内部构建。

    Returns:
        ``{"status", "domain", "candidates", "matched_tables"}``。
        ``matched_tables`` 为命中归属的表（未命中=空），前端可提示"该表未被采集"。
    """
    tables = _candidate_tables(sql=sql, source_table=source_table)
    if not tables:
        return {"status": "none", "domain": None, "candidates": [], "matched_tables": []}

    domain_map = await _domain_map(db)
    matches = await _lookup_tables(db, tables)
    matched_tables = sorted({t for t, _ in matches})
    domains = _aggregate(matches, domain_map)

    if not domains:
        llm = None
        if llm_budget is None or llm_budget["used"] < llm_budget["limit"]:
            if llm_budget is not None:
                llm_budget["used"] += 1
            llm = await _llm_suggest(db, tables, sql, domain_map, client=client)
        if llm:
            return {
                "status": "llm",
                "domain": llm,
                "candidates": [],
                "matched_tables": matched_tables,
            }
        return {
            "status": "none",
            "domain": None,
            "candidates": [],
            "matched_tables": matched_tables,
        }

    if len(domains) == 1:
        return {
            "status": "unique",
            "domain": domains[0],
            "candidates": [],
            "matched_tables": matched_tables,
        }

    return {
        "status": "multiple",
        "domain": None,
        "candidates": domains,
        "matched_tables": matched_tables,
    }
