"""全局聚合搜索 Repository（FR-18 全局搜索栏生产化）。

跨 9 类资源（指标/维度/术语/模板/数据源/采集目录表/采集目录字段/主题域/度量目录）
按关键词模糊匹配，统一返回结构化条目供前端顶栏下拉与全局搜索页消费。

检索架构（TD §1.3 / §5.2 降级矩阵）：
- 指标/术语两类优先走 Elasticsearch（metric_idx/term_idx，multi_match 含同义词字段，
  相关度排序优于 MySQL LIKE）；ES 禁用/异常时自动降级 MySQL LIKE（TD 降级边界
  "ES✗→退 MySQL"）。其余 7 类维持 MySQL（未建索引）。
- 中英业务同义词（``search/synonyms.py``）双向扩展关键词：中文→英文候选 OR 进
  LIKE 命中英文表名/字段名/编码，英文→中文命中中文内容；ES 侧由 synonym filter
  在分词层等价扩展。两端共用同一份词对，保证行为一致。
- 全部 MySQL 查询走 SQLAlchemy ORM 参数化（无字符串拼接 SQL）。

安全约束：
- LIKE 通配符（%/_）转义，防用户输入放大模糊匹配面。
- 字段级搜索对 schema_json 用 CAST(... AS CHAR) 粗匹配 + 内存精确提取列名/注释，
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
from app.services.search.synonyms import SYNONYM_MAP


def _escape_like(text: str) -> str:
    """转义 LIKE 通配符，防止用户输入 ``%``/``_`` 做全表模糊放大。

    修复前：转义为 \\% 但调用方 like() 无 escape 参数不生成 ESCAPE 子句，
    MySQL 默认把 \\ 当普通字符、%/_ 仍当通配符 → 转义实际失效。
    现用 / 作转义符（转义 //、/% 和 /_），配合 like(..., escape="/")。
    """
    return text.replace("/", "//").replace("%", "/%").replace("_", "/_")


def _expand_keywords(q: str) -> list[str]:
    """中英业务同义词双向扩展关键词（去重、保序）。

    - 中文命中词对：返回 ``[中文, *英文候选]``（如 ``订单 → 订单, order, sales_order``）；
    - 英文命中某候选：返回 ``[英文, 中文]``（如 ``order → order, 订单``）；
    - 未命中任何词对：仅返回原词。

    Returns:
        原始关键词 + 同义词候选（含原词本身，保证原语义不丢失）。
    """
    raw = q.strip()
    if not raw:
        return []
    expanded = [raw]
    low = raw.lower()
    if low in SYNONYM_MAP:
        expanded.extend(SYNONYM_MAP[low])
    else:
        for cn, en_list in SYNONYM_MAP.items():
            if low in (e.lower() for e in en_list):
                expanded.append(cn)
                break
    seen: set[str] = set()
    out: list[str] = []
    for kw in expanded:
        k = kw.strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out


def _like_any(column: Any, needles: list[str]) -> Any:
    """列命中任一 LIKE 候选（统一 ESCAPE '/'，防通配符放大）。"""
    return or_(*(column.like(n, escape="/") for n in needles))


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

    async def search(
        self,
        q: str,
        limit: int = 5,
        *,
        visible_actor_id: int | None = None,
        visible_role: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """跨 9 类资源聚合搜索，按类型分组返回。

        Args:
            q: 搜索关键词（调用方已去空白，非空）。
            limit: 每类资源返回条数上限。
            visible_actor_id: 读路径行级隔离（P0-3）——非管理角色仅可检索
                公开状态（PUBLISHED/EXPERIMENTAL/DEPRECATED）+ 本人 Owner/副 Owner
                的未发布资产；管理角色传 None 即不加过滤（对齐指标目录语义）。
            visible_role: 调用者角色（配合 visible_actor_id 判定 reviewer 放行）。

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
            "measure": [],
        }
        raw_q = q.strip()
        if not raw_q:
            return groups
        # 中英同义词扩展 → 多 LIKE 候选（各方法共用，保证行为一致）
        needles = [f"%{_escape_like(kw)}%" for kw in _expand_keywords(raw_q)]
        # 9 类资源查询相互独立，并行提交缩短聚合搜索 P95；
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
            groups["measure"],
        ) = await asyncio.gather(
            self._search_metrics(
                needles,
                limit,
                raw_q=raw_q,
                visible_actor_id=visible_actor_id,
                visible_role=visible_role,
            ),
            self._search_dimensions(
                needles,
                limit,
                visible_actor_id=visible_actor_id,
                visible_role=visible_role,
            ),
            self._search_terms(
                needles,
                limit,
                raw_q=raw_q,
                visible_actor_id=visible_actor_id,
                visible_role=visible_role,
            ),
            self._search_templates(needles, limit),
            self._search_data_sources(needles, limit),
            self._search_catalogs(needles, limit),
            self._search_fields(raw_q, limit),
            self._search_subject_domains(needles, limit),
            self._search_measures(needles, limit),
        )
        return groups

    def _visibility_conditions(self, visible_actor_id: int | None, visible_role: str | None) -> Any | None:
        """指标读路径行级隔离（对齐 semantic/repository.py P0-3）。

        非管理角色（platform_admin/domain_admin 之外）仅可检索：
        - 公开状态（PUBLISHED/EXPERIMENTAL/DEPRECATED）；
        - 本人 Owner/副 Owner 的未发布资产（DRAFT/REVIEW 私有工作区不向他人泄露）；
        - reviewer 额外放行 REVIEW（评审工作台需查看待审项）。

        Returns:
            SQLAlchemy OR 条件；管理角色/未传上下文返回 None（不加过滤）。
        """
        if (
            visible_actor_id is not None
            and visible_role is not None
            and visible_role not in ("platform_admin", "domain_admin")
        ):
            visibility: list[Any] = [
                Metric.status.in_(("PUBLISHED", "EXPERIMENTAL", "DEPRECATED")),
                Metric.owner_id == visible_actor_id,
                Metric.backup_owner_id == visible_actor_id,
            ]
            if visible_role == "reviewer":
                visibility.append(Metric.status == "REVIEW")
            return or_(*visibility)
        return None

    def _es_visibility_filter(self, visible_actor_id: int | None, visible_role: str | None) -> list[dict[str, Any]]:
        """ES 查询层可见性过滤（与 MySQL 路径同一语义，跨引擎一致）。

        ES 索引（metric_idx）含 owner_id/backup_owner_id 字段（es_indexer 同步），
        非管理角色在查询层用 bool.filter 收敛可见范围；管理角色返回空列表（不过滤）。
        """
        if (
            visible_actor_id is not None
            and visible_role is not None
            and visible_role not in ("platform_admin", "domain_admin")
        ):
            clauses: list[dict[str, Any]] = [
                {"terms": {"status": ["PUBLISHED", "EXPERIMENTAL", "DEPRECATED"]}},
                {"term": {"owner_id": visible_actor_id}},
                {"term": {"backup_owner_id": visible_actor_id}},
            ]
            if visible_role == "reviewer":
                clauses.append({"term": {"status": "REVIEW"}})
            return [{"bool": {"should": clauses, "minimum_should_match": 1}}]
        return []

    async def _search_metrics(
        self,
        needles: list[str],
        limit: int,
        raw_q: str | None = None,
        *,
        visible_actor_id: int | None = None,
        visible_role: str | None = None,
    ) -> list[dict[str, Any]]:
        """指标检索：ES 优先（相关度排序），ES 禁用/异常/未命中时降级 MySQL LIKE。

        同义词（业务别名，如"支付金额"别名"pay"）存于 ``measure_catalog.synonyms``，
        经 ``metric.measure_id`` 外连接粗匹配；命中来源以 ``match_reason`` 标识
        （``synonym`` 供前端提示"您是不是想找…"，直接命中为 ``field``）。

        可见性（D-1 修复）：MySQL 与 ES 两路径均按 ``_visibility_conditions`` /
        ``_es_visibility_filter`` 收敛——低权限用户不得经搜索侧门检索他人未发布
        草稿/审核中指标（此前全局搜索无任何可见性过滤，DRAFT/REVIEW + PII 标记
        可被任意 viewer 检索，绕过指标目录行级隔离）。
        """
        if raw_q:
            es_items = await self._es_search_assets(
                "metric",
                raw_q,
                limit,
                visible_actor_id=visible_actor_id,
                visible_role=visible_role,
            )
            if es_items is not None:
                return es_items
        code_match = _like_any(Metric.metric_code, needles)
        name_match = _like_any(Metric.name, needles)
        desc_match = _like_any(Metric.description, needles)
        syn_match = _like_any(MeasureCatalog.synonyms.cast(String), needles)
        conditions: list[Any] = [Metric.deleted_at.is_(None)]
        visibility = self._visibility_conditions(visible_actor_id, visible_role)
        if visibility is not None:
            conditions.append(visibility)
        conditions.append(or_(code_match, name_match, desc_match, syn_match))
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
            .where(*conditions)
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

    async def _search_dimensions(
        self,
        needles: list[str],
        limit: int,
        *,
        visible_actor_id: int | None = None,
        visible_role: str | None = None,
    ) -> list[dict[str, Any]]:
        stmt = (
            select(Dimension)
            .where(
                Dimension.deleted_at.is_(None),
                or_(
                    _like_any(Dimension.dim_code, needles),
                    _like_any(Dimension.name, needles),
                    _like_any(Dimension.description, needles),
                ),
            )
            .limit(limit)
        )
        # P0-3 读路径行级隔离（对齐 dimension list_dimensions）：维度 DRAFT/REVIEW
        # 是创建者私有工作区，他人不得经搜索侧门窥探；公开状态可被发现。
        visibility = self._dimension_visibility(visible_actor_id, visible_role)
        if visibility is not None:
            stmt = stmt.where(visibility)
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

    def _dimension_visibility(
        self, visible_actor_id: int | None, visible_role: str | None
    ) -> Any | None:
        """维度读路径行级隔离（对齐 dimension/repository.py P0-3）。"""
        if (
            visible_actor_id is not None
            and visible_role is not None
            and visible_role not in ("platform_admin", "domain_admin")
        ):
            vis: list[Any] = [
                Dimension.status.in_(("PUBLISHED", "DEPRECATED")),
                Dimension.owner_id == visible_actor_id,
            ]
            if visible_role == "reviewer":
                vis.append(Dimension.status == "REVIEW")
            return or_(*vis)
        return None

    async def _search_terms(
        self,
        needles: list[str],
        limit: int,
        raw_q: str | None = None,
        *,
        visible_actor_id: int | None = None,
        visible_role: str | None = None,
    ) -> list[dict[str, Any]]:
        """术语检索：ES 优先（相关度排序），ES 禁用/异常/未命中时降级 MySQL LIKE。"""
        if raw_q:
            es_items = await self._es_search_assets(
                "term",
                raw_q,
                limit,
                visible_actor_id=visible_actor_id,
                visible_role=visible_role,
            )
            if es_items is not None:
                return es_items
        stmt = (
            select(Term)
            .where(
                Term.deleted_at.is_(None),
                or_(
                    _like_any(Term.term_code, needles),
                    _like_any(Term.name, needles),
                    _like_any(Term.definition, needles),
                    # 术语同义词（业务别名）粗匹配：JSON 文本 LIKE，走参数化防注入
                    _like_any(Term.synonyms.cast(String), needles),
                    # 边界说明（可空）纳入检索，扩大"描述类"命中面
                    _like_any(Term.boundary, needles),
                ),
            )
            .limit(limit)
        )
        # P0-3 读路径行级隔离（对齐 glossary list_terms）：术语 DRAFT/REVIEW 私有。
        visibility = self._term_visibility(visible_actor_id, visible_role)
        if visibility is not None:
            stmt = stmt.where(visibility)
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

    def _term_visibility(
        self, visible_actor_id: int | None, visible_role: str | None
    ) -> Any | None:
        """术语读路径行级隔离（对齐 glossary/repository.py P0-3）。"""
        if (
            visible_actor_id is not None
            and visible_role is not None
            and visible_role not in ("platform_admin", "domain_admin")
        ):
            vis: list[Any] = [
                Term.status.in_(("PUBLISHED", "DEPRECATED")),
                Term.owner_id == visible_actor_id,
            ]
            if visible_role == "reviewer":
                vis.append(Term.status == "REVIEW")
            return or_(*vis)
        return None

    async def _es_search_assets(
        self,
        asset_type: str,
        raw_q: str,
        limit: int,
        *,
        visible_actor_id: int | None = None,
        visible_role: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """经 ES 检索指标/术语（multi_match 跨 code/name/description/synonyms，相关度排序）。

        可见性（D-1）：非管理角色在 bool.filter 层收敛可见范围（公开状态 + 本人负责），
        与 MySQL 路径同一语义；管理角色不过滤。

        Returns:
            命中条目列表；ES 禁用/未配置/异常/零命中时返回 ``None``，
            由调用方降级 MySQL LIKE（TD §1.3 降级边界 "ES✗→退 MySQL"）。
        """
        es = self._es()
        if not es.enabled:
            return None
        index = "metric_idx" if asset_type == "metric" else "term_idx"
        # metric_idx 描述字段名 description；term_idx 为 definition（es_indexer.py 定义）。
        # 此前统一写 description 致 ES 路径搜术语定义静默不命中 → 按类型区分（TD§12.7 全链路一致）。
        desc_field = "description" if asset_type == "metric" else "definition"
        try:
            match_query: dict[str, Any] = {
                "multi_match": {
                    "query": raw_q,
                    "fields": ["code^3", "name^2", desc_field, "synonyms"],
                    "type": "best_fields",
                    "fuzziness": "AUTO",
                }
            }
            filters = self._es_visibility_filter(visible_actor_id, visible_role)
            if filters:
                body: dict[str, Any] = {
                    "query": {"bool": {"must": match_query, "filter": filters}}
                }
            else:
                body = {"query": match_query}
            resp = await es.search(index, body, size=limit)
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

    async def _search_templates(self, needles: list[str], limit: int) -> list[dict[str, Any]]:
        stmt = (
            select(MetricTemplate)
            .where(
                MetricTemplate.deleted_at.is_(None),
                or_(
                    _like_any(MetricTemplate.code, needles),
                    _like_any(MetricTemplate.name, needles),
                    _like_any(MetricTemplate.description, needles),
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

    async def _search_data_sources(self, needles: list[str], limit: int) -> list[dict[str, Any]]:
        stmt = (
            select(DataSource)
            .where(DataSource.deleted_at.is_(None))
            .where(
                or_(
                    _like_any(DataSource.name, needles),
                    _like_any(DataSource.source_id, needles),
                    # 用途描述（TD§12.1 描述类字段全覆盖）纳入检索
                    _like_any(DataSource.description, needles),
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

    async def _search_catalogs(self, needles: list[str], limit: int) -> list[dict[str, Any]]:
        """表级搜索：entity_name + 表级业务描述模糊匹配（表/视图/字段实体）。

        表级描述（``DBCatalog.description``，治理补全的"销售订单表"等中文说明）纳入
        匹配，让"搜中文描述找到英文表"成为可能（TD§12.1 描述类字段全覆盖）。
        """
        stmt = (
            select(DBCatalog)
            .where(
                DBCatalog.deleted_at.is_(None),
                or_(
                    _like_any(DBCatalog.entity_name, needles),
                    _like_any(DBCatalog.description, needles),
                ),
            )
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
        """字段级搜索：扫描 db_catalog.schema_json 的 columns[].name/comment 命中关键词。

        SQL 层用 ``CAST(schema_json AS CHAR) LIKE %kw%`` 粗筛（跨方言可用），
        内存层精确提取命中的列名或字段注释，返回所属表 + 列名，避免整表 JSON 文本误报。
        修复：此前内存精筛只匹配 col.name，注释（col.comment）命中的结果被丢弃
        → 现同时匹配列名与字段注释（TD§12.1 描述类字段全覆盖）。
        """
        raw = q.strip()
        if not raw:
            return []
        raw_variants = [k.lower() for k in _expand_keywords(raw)]
        needles = [f"%{_escape_like(kw)}%" for kw in _expand_keywords(raw)]
        stmt = (
            select(DBCatalog)
            .where(
                DBCatalog.deleted_at.is_(None),
                or_(*(cast(DBCatalog.schema_json, String).ilike(n, escape="/") for n in needles)),
            )
            .limit(limit * 3)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        items: list[dict[str, Any]] = []
        for c in rows:
            columns = (c.schema_json or {}).get("columns") or []
            for col in columns:
                if not isinstance(col, dict):
                    continue
                col_name = str(col.get("name", ""))
                col_comment = str(col.get("comment", ""))
                name_hit = col_name and any(rv in col_name.lower() for rv in raw_variants)
                comment_hit = col_comment and any(rv in col_comment.lower() for rv in raw_variants)
                if name_hit or comment_hit:
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

    async def _search_subject_domains(self, needles: list[str], limit: int) -> list[dict[str, Any]]:
        stmt = (
            select(SubjectDomain)
            .where(
                SubjectDomain.deleted_at.is_(None),
                or_(
                    _like_any(SubjectDomain.code, needles),
                    _like_any(SubjectDomain.name, needles),
                    # 域描述（TD§12.1 描述类字段全覆盖）纳入检索
                    _like_any(SubjectDomain.description, needles),
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

    async def _search_measures(self, needles: list[str], limit: int) -> list[dict[str, Any]]:
        """度量目录检索（FR-18 覆盖度量目录模块）：编码/名称/描述/统计口径/源头系统/同义词。

        度量目录为原子指标权威继承源（OneData 原子层），承载"同义词/源头系统/统计口径"
        等描述类信息——全部纳入匹配，让"搜中文口径找到度量"成为可能。
        走 MySQL LIKE（与其余 7 类一致，未建 ES 索引）。
        """
        stmt = (
            select(MeasureCatalog)
            .where(
                MeasureCatalog.deleted_at.is_(None),
                or_(
                    _like_any(MeasureCatalog.measure_code, needles),
                    _like_any(MeasureCatalog.name, needles),
                    _like_any(MeasureCatalog.description, needles),
                    # 统计口径（业务侧如何计算该度量）——描述类字段
                    _like_any(MeasureCatalog.stat_caliber, needles),
                    # 源头系统/同义词为 JSON 数组：CAST 文本粗匹配，参数化防注入
                    _like_any(MeasureCatalog.source_system.cast(String), needles),
                    _like_any(MeasureCatalog.synonyms.cast(String), needles),
                ),
            )
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            {
                "type": "measure",
                "id": m.id,
                "code": m.measure_code,
                "name": m.name,
                "domain": m.domain,
                "status": m.status,
            }
            for m in rows
        ]
