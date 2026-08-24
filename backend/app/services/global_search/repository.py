"""全局聚合搜索 Repository（FR-18 全局搜索栏生产化）。

跨 8 类资源（指标/维度/术语/模板/数据源/采集目录表/采集目录字段/主题域）
按关键词模糊匹配，统一返回结构化条目供前端顶栏下拉与全局搜索页消费。

检索架构（TD §1.3 / §5.2 降级矩阵）：
- 指标/术语两类优先走 Elasticsearch（metric_idx/term_idx，multi_match 含同义词字段，
  相关度排序优于 MySQL LIKE）；ES 禁用/异常时自动降级 MySQL LIKE（TD 降级边界
  "ES✗→退 MySQL"）。其余 6 类维持 MySQL（未建索引）。
- 全部 MySQL 查询走 SQLAlchemy ORM 参数化（无字符串拼接 SQL）。

安全约束：
- LIKE 通配符（%/_）转义，防用户输入放大模糊匹配面。
- 字段级搜索对 schema_json 用 CAST(... AS CHAR) 粗匹配 + 内存精确提取列名，
  命中列以独立 ``field`` 条目返回。
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import String, case, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.es_client import EsClient, get_es_client
from app.models.data_source import DataSource, DBCatalog
from app.models.dimension import Dimension
from app.models.measure_catalog import MeasureCatalog
from app.models.metric import Metric
from app.models.metric_template import MetricTemplate
from app.models.subject_domain import SubjectDomain
from app.models.term import Term


def _escape_like(text: str) -> str:
    """转义 LIKE 通配符，防止用户输入 ``%``/``_`` 做全表模糊放大。

    修复前：转义为 \\% 但调用方 like() 无 escape 参数不生成 ESCAPE 子句，
    MySQL 默认把 \\ 当普通字符、%/_ 仍当通配符 → 转义实际失效。
    现用 / 作转义符（转义 //、/% 和 /_），配合 like(..., escape="/")。
    """
    return text.replace("/", "//").replace("%", "/%").replace("_", "/_")


class GlobalSearchRepository:
    """全局聚合搜索数据访问。

    ``search`` 依次查询各类资源，每类返回至多 ``limit`` 条（默认 5，
    顶栏下拉场景足够），按类型分组组装为 ``{type: [item, ...]}``。
    """

    def __init__(self, session: AsyncSession, *, es_client: EsClient | None = None) -> None:
        self._session = session
        # ES 客户端注入点：测试可注入 disabled/mock；None 时惰性取进程级单例。
        self._es_client = es_client

    def _es(self) -> EsClient:
        if self._es_client is None:
            self._es_client = get_es_client()
        return self._es_client

    async def search(self, q: str, limit: int = 5) -> dict[str, list[dict[str, Any]]]:
        """跨 8 类资源聚合搜索，按类型分组返回。

        Args:
            q: 搜索关键词（调用方已去空白，非空）。
            limit: 每类资源返回条数上限。

        Returns:
            ``{"metric": [...], "dimension": [...], ...}`` 分组结构；
            无结果组为 ``[]``。
        """
        groups: dict[str, list[dict[str, Any]]] = {
            "metric": [],
            "dimension": [],
            "term": [],
            "template": [],
            "data_source": [],
            "catalog": [],
            "field": [],
            "subject_domain": [],
        }
        if not q.strip():
            return groups
        needle = f"%{_escape_like(q.strip())}%"
        # 8 类资源查询相互独立，并行提交缩短聚合搜索 P95；
        # 同一 AsyncSession 由 SQLAlchemy 内部锁串行化底层执行（安全），
        # 未来拆分独立会话时即可真正并行下推。
        (
            groups["metric"],
            groups["dimension"],
            groups["term"],
            groups["template"],
            groups["data_source"],
            groups["catalog"],
            groups["field"],
            groups["subject_domain"],
        ) = await asyncio.gather(
            self._search_metrics(needle, limit, raw_q=q.strip()),
            self._search_dimensions(needle, limit),
            self._search_terms(needle, limit, raw_q=q.strip()),
            self._search_templates(needle, limit),
            self._search_data_sources(needle, limit),
            self._search_catalogs(needle, limit),
            self._search_fields(q, limit),
            self._search_subject_domains(needle, limit),
        )
        return groups

    async def _search_metrics(
        self, needle: str, limit: int, raw_q: str | None = None
    ) -> list[dict[str, Any]]:
        """指标检索：ES 优先（相关度排序），ES 禁用/异常/未命中时降级 MySQL LIKE。

        同义词（业务别名，如"支付金额"别名"pay"）存于 ``measure_catalog.synonyms``，
        经 ``metric.measure_id`` 外连接粗匹配；命中来源以 ``match_reason`` 标识
        （``synonym`` 供前端提示"您是不是想找…"，直接命中为 ``field``）。
        """
        if raw_q:
            es_items = await self._es_search_assets("metric", raw_q, limit)
            if es_items is not None:
                return es_items
        code_match = Metric.metric_code.like(needle, escape="/")
        name_match = Metric.name.like(needle, escape="/")
        desc_match = Metric.description.like(needle, escape="/")
        syn_match = MeasureCatalog.synonyms.cast(String).like(needle, escape="/")
        stmt = (
            select(
                Metric,
                case(
                    (or_(code_match, name_match, desc_match), "field"),
                    (syn_match, "synonym"),
                    else_="field",
                ).label("match_reason"),
            )
            .outerjoin(MeasureCatalog, Metric.measure_id == MeasureCatalog.id)
            .where(
                Metric.deleted_at.is_(None),
                or_(code_match, name_match, desc_match, syn_match),
            )
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            {
                "type": "metric",
                "id": m.id,
                "code": m.metric_code,
                "name": m.name,
                "domain": m.domain,
                "status": m.status,
                "pii_flag": bool(m.pii_flag),
                "match_reason": reason,
            }
            for m, reason in rows
        ]

    async def _search_dimensions(self, needle: str, limit: int) -> list[dict[str, Any]]:
        stmt = (
            select(Dimension)
            .where(
                Dimension.deleted_at.is_(None),
                or_(
                    Dimension.dim_code.like(needle, escape="/"),
                    Dimension.name.like(needle, escape="/"),
                    Dimension.description.like(needle, escape="/"),
                ),
            )
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            {
                "type": "dimension",
                "id": d.id,
                "code": d.dim_code,
                "name": d.name,
                "domain": d.domain,
                "status": d.status,
            }
            for d in rows
        ]

    async def _search_terms(
        self, needle: str, limit: int, raw_q: str | None = None
    ) -> list[dict[str, Any]]:
        """术语检索：ES 优先（相关度排序），ES 禁用/异常/未命中时降级 MySQL LIKE。"""
        if raw_q:
            es_items = await self._es_search_assets("term", raw_q, limit)
            if es_items is not None:
                return es_items
        stmt = (
            select(Term)
            .where(
                Term.deleted_at.is_(None),
                or_(
                    Term.term_code.like(needle, escape="/"),
                    Term.name.like(needle, escape="/"),
                    Term.definition.like(needle, escape="/"),
                    # 术语同义词（业务别名）粗匹配：JSON 文本 LIKE，走参数化防注入
                    Term.synonyms.cast(String).like(needle, escape="/"),
                ),
            )
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            {
                "type": "term",
                "id": t.id,
                "code": t.term_code,
                "name": t.name,
                "domain": t.domain,
                "status": t.status,
            }
            for t in rows
        ]

    async def _es_search_assets(
        self, asset_type: str, raw_q: str, limit: int
    ) -> list[dict[str, Any]] | None:
        """经 ES 检索指标/术语（multi_match 跨 code/name/description/synonyms，相关度排序）。

        Returns:
            命中条目列表；ES 禁用/未配置/异常/零命中时返回 ``None``，
            由调用方降级 MySQL LIKE（TD §1.3 降级边界 "ES✗→退 MySQL"）。
        """
        es = self._es()
        if not es.enabled:
            return None
        index = "metric_idx" if asset_type == "metric" else "term_idx"
        try:
            resp = await es.search(
                index,
                {
                    "query": {
                        "multi_match": {
                            "query": raw_q,
                            "fields": ["code^3", "name^2", "description", "synonyms"],
                            "type": "best_fields",
                            "fuzziness": "AUTO",
                        }
                    }
                },
                size=limit,
            )
            hits = ((resp or {}).get("hits") or {}).get("hits") or []
            items = [self._es_hit_to_item(asset_type, h.get("_source") or {}) for h in hits]
            return items or None
        except Exception:  # noqa: BLE001 - ES 失败静默降级 MySQL，不阻断检索主流程
            return None

    def _es_hit_to_item(self, asset_type: str, src: dict[str, Any]) -> dict[str, Any]:
        """将 ES 文档 _source 组装为与 MySQL 路径一致的条目结构（前端无感知）。"""
        if asset_type == "metric":
            return {
                "type": "metric",
                "id": src.get("id"),
                "code": src.get("metric_code"),
                "name": src.get("name"),
                "domain": src.get("domain"),
                "status": src.get("status"),
                "pii_flag": bool(src.get("pii_flag")),
                "match_reason": "field",
            }
        return {
            "type": "term",
            "id": src.get("id"),
            "code": src.get("term_code"),
            "name": src.get("name"),
            "domain": src.get("domain"),
            "status": src.get("status"),
        }

    async def _search_templates(self, needle: str, limit: int) -> list[dict[str, Any]]:
        stmt = (
            select(MetricTemplate)
            .where(
                MetricTemplate.deleted_at.is_(None),
                or_(
                    MetricTemplate.code.like(needle, escape="/"),
                    MetricTemplate.name.like(needle, escape="/"),
                    MetricTemplate.description.like(needle, escape="/"),
                ),
            )
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            {
                "type": "template",
                "id": t.id,
                "code": t.code,
                "name": t.name,
                "domain": t.domain,
                "status": "ACTIVE" if t.is_active else "INACTIVE",
            }
            for t in rows
        ]

    async def _search_data_sources(self, needle: str, limit: int) -> list[dict[str, Any]]:
        stmt = (
            select(DataSource)
            .where(DataSource.deleted_at.is_(None))
            .where(
                or_(
                    DataSource.name.like(needle, escape="/"),
                    DataSource.source_id.like(needle, escape="/"),
                )
            )
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            {
                "type": "data_source",
                "id": s.id,
                "code": s.source_id,
                "name": s.name,
                "domain": s.domain,
                "status": s.health_status,
                "source_type": s.source_type,
            }
            for s in rows
        ]

    async def _search_catalogs(self, needle: str, limit: int) -> list[dict[str, Any]]:
        """表级搜索：entity_name 模糊匹配（表/视图/字段实体，表名列命中）。"""
        stmt = (
            select(DBCatalog)
            .where(DBCatalog.deleted_at.is_(None), DBCatalog.entity_name.like(needle, escape="/"))
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            {
                "type": "catalog",
                "id": c.id,
                "code": c.entity_name,
                "name": c.entity_name,
                "domain": None,
                "status": None,
                "source_id": c.source_id,
                "entity_type": c.entity_type,
                "sensitivity_level": c.sensitivity_level,
            }
            for c in rows
        ]

    async def _search_fields(self, q: str, limit: int) -> list[dict[str, Any]]:
        """字段级搜索：扫描 db_catalog.schema_json 的 columns[].name 命中关键词。

        SQL 层用 ``CAST(schema_json AS CHAR) LIKE %kw%`` 粗筛（跨方言可用），
        内存层精确提取命中的列名，返回所属表 + 列名，避免整表 JSON 文本误报。
        """
        raw = q.strip().lower()
        if not raw:
            return []
        needle = f"%{_escape_like(q.strip())}%"
        stmt = (
            select(DBCatalog)
            .where(
                DBCatalog.deleted_at.is_(None),
                cast(DBCatalog.schema_json, String).ilike(needle, escape="/"),
            )
            .limit(limit * 3)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        items: list[dict[str, Any]] = []
        for c in rows:
            columns = (c.schema_json or {}).get("columns") or []
            for col in columns:
                col_name = str(col.get("name", "")) if isinstance(col, dict) else ""
                if col_name and raw in col_name.lower():
                    items.append(
                        {
                            "type": "field",
                            "id": c.id,
                            "code": col_name,
                            "name": col_name,
                            "domain": None,
                            "status": None,
                            "source_id": c.source_id,
                            "table_name": c.entity_name,
                            "sensitivity_level": c.sensitivity_level,
                        }
                    )
                    if len(items) >= limit:
                        return items
        return items

    async def _search_subject_domains(self, needle: str, limit: int) -> list[dict[str, Any]]:
        stmt = (
            select(SubjectDomain)
            .where(
                SubjectDomain.deleted_at.is_(None),
                or_(
                    SubjectDomain.code.like(needle, escape="/"),
                    SubjectDomain.name.like(needle, escape="/"),
                ),
            )
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            {
                "type": "subject_domain",
                "id": d.id,
                "code": d.code,
                "name": d.name,
                "domain": d.code,
                "status": d.status,
                "level": d.level,
            }
            for d in rows
        ]
