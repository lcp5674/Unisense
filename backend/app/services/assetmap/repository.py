"""资产地图 Repository（TD §12.11 / FR-18）。

只读聚合：元数据目录（db_catalog）、分类（classification）、指标（metric）。
P2 增强：图谱降级查询、热力聚合、责任人视图。
产品补充（FR-18 生产化）：全局搜索、资产健康、PII 合规视图、变更追踪、
我的资产、详情增强（血缘边列表 + 关联指标 + 源健康/新鲜度）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.models.collector_models import SchemaDriftLog
from app.models.consume import MetricValueSnapshot
from app.models.data_source import ColumnDescription, DataSource, DBCatalog
from app.models.enums import SensitivityLevelEnum
from app.models.governance import Classification, PiiFieldOverride
from app.models.lineage import LineageEdge
from app.models.metric import Metric
from app.models.user import User

logger = structlog.get_logger("unisense.assetmap.repository")


def _prune_graph_by_depth(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    depth: int,
    seed_nodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """从指标（seed）出发沿血缘边 BFS ``depth`` 层，收敛图谱规模。

    血缘是下游汇聚到指标的有向图：指标作为 BFS 起点向上游逐层展开，
    ``depth=1`` 只保留指标与其直连表，``depth=2`` 再展开一层中间表。
    返回 (收敛后节点, 两端均在被保留节点内的边)。
    """
    seed_ids = {n["id"] for n in seed_nodes}
    adj: dict[str, set[str]] = {}
    for e in edges:
        adj.setdefault(e["source"], set()).add(e["target"])
        adj.setdefault(e["target"], set()).add(e["source"])

    # BFS 无向遍历（血缘上下游都算邻居）；visited 防环
    frontier = set(seed_ids)
    visited = set(seed_ids)
    for _ in range(depth):
        nxt: set[str] = set()
        for nid in frontier:
            nxt |= adj.get(nid, set())
        nxt -= visited
        if not nxt:
            break
        visited |= nxt
        frontier = nxt

    kept_ids = set(visited)
    pruned_nodes = [n for n in nodes if n["id"] in kept_ids]
    pruned_edges = [
        e for e in edges if e["source"] in kept_ids and e["target"] in kept_ids
    ]
    return pruned_nodes, pruned_edges


class AssetMapRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- FULLTEXT 表级搜索（审查 T19：LIKE 前导通配符全表扫 → FULLTEXT 加速）----
    # 进程级能力标志：None=未探测 / True=可用 / False=不可用（SQLite 或索引缺失 → 回退 LIKE）。
    _fulltext_ok: bool | None = None

    @staticmethod
    def _catalog_name_like(kw: str) -> Any:
        """LIKE 回退条件（% / _ 转义防模糊放大）。"""
        escaped = kw.replace("/", "//").replace("%", "/%").replace("_", "/_")
        return or_(
            DBCatalog.entity_name.ilike(f"%{escaped}%", escape="/"),
            DBCatalog.source_id.ilike(f"%{escaped}%", escape="/"),
        )

    @staticmethod
    def _catalog_name_fulltext(kw: str) -> Any:
        """FULLTEXT 短语条件（ngram 2-gram，短语模式语义≈子串；<2 字符由调用方回退 LIKE）。"""
        phrase = kw.strip().replace('"', "")
        return text(
            "MATCH(db_catalog.entity_name, db_catalog.source_id) AGAINST (:kw IN BOOLEAN MODE)"
        ).bindparams(kw=f'"{phrase}"')

    async def _catalog_name_cond(self, kw: str) -> Any:
        """选择表级搜索条件：MySQL + ≥2 字符 → FULLTEXT（一次进程级探测，失败永久回退 LIKE）；
        其余 → LIKE。"""
        kw = kw.strip()
        if not kw:
            return None
        if self._fulltext_ok is False or len(kw) < 2:
            return self._catalog_name_like(kw)
        try:
            bind = self._session.get_bind()
            if bind.dialect.name != "mysql":
                type(self)._fulltext_ok = False
                return self._catalog_name_like(kw)
        except Exception:  # noqa: BLE001 - 探测失败回退 LIKE
            return self._catalog_name_like(kw)
        if self._fulltext_ok is None:
            # 索引是否就绪：LIMIT 0 仅验证可执行、不扫数据（迁移 0113 应用后为 True）
            try:
                probe = text(
                    "SELECT 1 FROM db_catalog WHERE MATCH(entity_name, source_id) "
                    "AGAINST (:kw IN BOOLEAN MODE) LIMIT 0"
                ).bindparams(kw="测试")
                await self._session.execute(probe)
                type(self)._fulltext_ok = True
            except Exception:  # noqa: BLE001 - 索引缺失（迁移未应用）/非 MySQL → 回退 LIKE
                type(self)._fulltext_ok = False
                logger.warning("assetmap_fulltext_unavailable_fallback_like")
                return self._catalog_name_like(kw)
        return self._catalog_name_fulltext(kw)

    async def list_tables(
        self,
        source_id: str | None,
        sensitivity: str | None,
        limit: int,
        domain: str | None = None,
        owner_id: int | None = None,
        schema_status: str | None = None,
        keyword: str | None = None,
        database: str | None = None,
        org_id: int | None = None,
        offset: int = 0,
    ) -> tuple[list[DBCatalog], int]:
        """数据表目录多维度过滤（数据表 Tab / CSV 导出共用）。

        支持：数据源 / 敏感度 / 业务域（经 data_source 继承）/ 责任人 /
        Schema 完整性（complete|incomplete）/ 关键字（表名或数据源模糊）/
        库名（entity_name 前缀，对齐采集目录 description-coverage 库筛选）。

        P2-1：支持 offset 分页并返回真实总数（此前 total=len(items) 为静默
        截断后的假总数）。``offset`` 缺省 0。
        """
        stmt = select(DBCatalog).where(
            DBCatalog.entity_type == "table", DBCatalog.deleted_at.is_(None)
        )
        if source_id:
            stmt = stmt.where(DBCatalog.source_id == source_id)
        if sensitivity:
            stmt = stmt.where(DBCatalog.sensitivity_level == sensitivity)
        if owner_id == 0:
            # 约定：owner_id=0 表示「无责任人」（未分配，孤儿表）
            stmt = stmt.where(DBCatalog.owner_id.is_(None))
        elif owner_id is not None:
            stmt = stmt.where(DBCatalog.owner_id == owner_id)
        if schema_status == "incomplete":
            stmt = stmt.where(DBCatalog.schema_incomplete.is_(True))
        elif schema_status == "complete":
            stmt = stmt.where(DBCatalog.schema_incomplete.is_(False))
        if domain or org_id is not None:
            # db_catalog 无 domain/org 列，经数据源继承过滤（仅活跃源归属明确）
            stmt = stmt.join(DataSource, DataSource.source_id == DBCatalog.source_id).where(
                DataSource.deleted_at.is_(None),
            )
            if domain:
                stmt = stmt.where(DataSource.domain == domain)
            # 多租户隔离（P1 加固）：org_id 非 None 时仅返回本组织数据源资产
            if org_id is not None:
                stmt = stmt.where(DataSource.org_id == org_id)
        if database:
            # 库名 = entity_name 前缀（库.表）；LIKE 通配符转义防模糊放大
            esc_db = database.replace("/", "//").replace("%", "/%").replace("_", "/_")
            stmt = stmt.where(DBCatalog.entity_name.ilike(f"{esc_db}.%", escape="/"))
        if keyword:
            # 表级搜索（T19 审查修复）：FULLTEXT（MySQL ≥2 字符）加速，LIKE 回退
            cond = await self._catalog_name_cond(keyword)
            if cond is not None:
                stmt = stmt.where(cond)
        total = (
            await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar() or 0
        rows = (
            await self._session.execute(stmt.limit(limit).offset(max(offset, 0)))
        ).scalars().all()
        return list(rows), int(total)

    async def orphan_assets(
        self,
        keyword: str | None = None,
        source_id: str | None = None,
        domain: str | None = None,
        entity_type: str | None = None,
        sensitivity: str | None = None,
        schema_status: str | None = None,
        database: str | None = None,
        limit: int = 200,
        org_id: int | None = None,
        offset: int = 0,
    ) -> tuple[list[DBCatalog], int]:
        """孤儿资产（无责任人）多维度过滤，镜像 ``list_tables``。

        支持：关键字 / 数据源 / 业务域（经 data_source 继承）/ 实体类型 /
        敏感度 / Schema 完整性（complete|incomplete）/ 库名（entity_name 前缀）。
        无参调用返回全部（概览下钻「孤儿资产明细」兼容）。

        P2-1：支持 offset 分页并返回真实总数（此前 total=len(items) 为静默
        截断后的假总数）。``offset`` 缺省 0。
        """
        stmt = select(DBCatalog).where(
            DBCatalog.owner_id.is_(None), DBCatalog.deleted_at.is_(None)
        )
        if source_id:
            stmt = stmt.where(DBCatalog.source_id == source_id)
        if entity_type:
            stmt = stmt.where(DBCatalog.entity_type == entity_type)
        if sensitivity:
            stmt = stmt.where(DBCatalog.sensitivity_level == sensitivity)
        if schema_status == "incomplete":
            stmt = stmt.where(DBCatalog.schema_incomplete.is_(True))
        elif schema_status == "complete":
            stmt = stmt.where(DBCatalog.schema_incomplete.is_(False))
        if domain or org_id is not None:
            # db_catalog 无 domain/org 列，经数据源继承过滤（仅活跃源归属明确）
            stmt = stmt.join(DataSource, DataSource.source_id == DBCatalog.source_id).where(
                DataSource.deleted_at.is_(None),
            )
            if domain:
                stmt = stmt.where(DataSource.domain == domain)
            # 多租户隔离（P1 加固）：org_id 非 None 时仅返回本组织数据源资产
            if org_id is not None:
                stmt = stmt.where(DataSource.org_id == org_id)
        if database:
            # 库名 = entity_name 前缀（库.表）；LIKE 通配符转义防模糊放大
            esc_db = database.replace("/", "//").replace("%", "/%").replace("_", "/_")
            stmt = stmt.where(DBCatalog.entity_name.ilike(f"{esc_db}.%", escape="/"))
        if keyword:
            # 表级搜索（T19 审查修复）：FULLTEXT（MySQL ≥2 字符）加速，LIKE 回退
            cond = await self._catalog_name_cond(keyword)
            if cond is not None:
                stmt = stmt.where(cond)
        total = (
            await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar() or 0
        rows = (
            await self._session.execute(stmt.limit(limit).offset(max(offset, 0)))
        ).scalars().all()
        return list(rows), int(total)

    @staticmethod
    def _summarize_schema(schema_json: Any) -> Any:
        """将 schema_json 压缩为可读摘要（字段名/类型/注释列表）。

        不直接返回原始 schema_json：其字段可能含敏感细节，摘要仅暴露
        字段级元数据，满足资产地图详情展示（TD §12.11 流程 #5）。
        """
        if not isinstance(schema_json, dict):
            return None
        fields = schema_json.get("fields") or schema_json.get("columns") or []
        if isinstance(fields, list):
            summary: list[dict[str, Any]] = []
            for f in fields:
                if isinstance(f, dict):
                    summary.append(
                        {
                            "name": f.get("name") or f.get("column"),
                            "type": f.get("type") or f.get("data_type"),
                            "comment": f.get("comment"),
                            # 脱敏样本值（采样开启时才有）：采集侧已用
                            # classifier.mask_sample 打码（手机 138****5678），
                            # 不含原始敏感值，可安全暴露给治理端查看。
                            "sample": f.get("sample"),
                        }
                    )
                else:
                    summary.append({"name": str(f)})
            return summary
        return schema_json

    @staticmethod
    def _merge_descriptions(
        summary: list[dict[str, Any]],
        descriptions: Sequence[ColumnDescription],
    ) -> list[dict[str, Any]]:
        """将 column_descriptions 按 manual>llm>schema 优先级合并到 schema_summary。

        Args:
            summary: _summarize_schema 的输出列表。
            descriptions: column_descriptions 表记录。

        Returns:
            增强后的 summary，每条字段增加 description 和 description_source。
        """
        desc_map: dict[str, ColumnDescription] = {d.column_name: d for d in descriptions}
        for field in summary:
            col_name = field.get("name")
            if col_name and col_name in desc_map:
                d = desc_map[col_name]
                field["description"] = d.description
                field["description_source"] = d.source
            elif field.get("comment"):
                # 无独立描述记录，但有原始 comment → 使用 schema_json 原始 comment
                field["description"] = field["comment"]
                field["description_source"] = "schema"
            else:
                field["description"] = None
                field["description_source"] = None
        return summary

    async def get_entity_detail(
        self, entity_id: int, org_id: int | None = None
    ) -> dict[str, Any] | None:
        """资产实体详情：元数据 + 敏感度 + PII + 血缘边列表 + 关联指标 + 源健康/新鲜度。

        Args:
            entity_id: db_catalog 主键。
            org_id: 当前用户组织 ID；非 None 时校验实体归属本组织（P1 多租户隔离）。

        Returns:
            详情字典；实体不存在/已删除/跨组织返回 ``None``。
        """
        row = (
            await self._session.execute(
                select(DBCatalog).where(DBCatalog.id == entity_id, DBCatalog.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        # P1 多租户隔离：实体所属数据源非本组织时视为不可见（防跨组织读详情）
        if org_id is not None:
            src = (
                await self._session.execute(
                    select(DataSource).where(DataSource.source_id == row.source_id)
                )
            ).scalar_one_or_none()
            if src is None or src.org_id != org_id:
                return None

        variants = self._lineage_variants(row.entity_name)
        lineage_edges = await self._lineage_edges_for(variants, limit=50)
        lineage_count = len(lineage_edges)

        # 关联指标：血缘下游指向 metric: 前缀的节点（指标级血缘）
        related_metrics = await self._related_metrics_for(variants)

        # 源健康/新鲜度：关联 data_source 的最后健康检查与健康状态
        source_health = await self._source_health(row.source_id)
        # 业务域：db_catalog 无 domain 列，经 data_source 继承（生产详情展示）
        domain = await self._source_domain(row.source_id)

        sens = (row.sensitivity_level or "").upper()

        # 查询 column_descriptions 并合并到 schema_summary
        schema_summary = self._summarize_schema(row.schema_json)
        if isinstance(schema_summary, list):
            descriptions = await self._session.execute(
                select(ColumnDescription).where(
                    ColumnDescription.catalog_id == row.id,
                    ColumnDescription.deleted_at.is_(None),
                )
            )
            desc_list = descriptions.scalars().all()
            schema_summary = self._merge_descriptions(schema_summary, desc_list)

        # PII 合规增强：字段级命中明细（classification.pii_columns + 人工标注）
        # 其中 _entity_pii_fields 内部已合并人工标注（一次查询）
        pii_fields = await self._entity_pii_fields(row)
        pii_overrides = [
            {
                "column": f["column"],
                "suppressed": f["suppressed"],
                "reason": f.get("override_reason"),
            }
            for f in pii_fields
            if f.get("override_reason") is not None or f.get("suppressed")
        ]

        return {
            "id": row.id,
            "entity_name": row.entity_name,
            "entity_type": row.entity_type,
            "source_id": row.source_id,
            "source_name": (source_health or {}).get("source_name"),
            "domain": domain,
            "sensitivity_level": row.sensitivity_level,
            "owner_id": row.owner_id,
            # 责任人展示名（display_name 优先，缺省回退 username）——生产场景需可读
            "owner_name": await self._owner_display_name(row.owner_id),
            "column_count": len(schema_summary) if isinstance(schema_summary, list) else None,
            "schema_incomplete": row.schema_incomplete,
            "content_signature": row.content_signature,
            "schema_summary": schema_summary,
            # 表级业务描述（治理补全，TD §12.1）
            "description": row.description,
            "description_source": row.description_source,
            "description_updated_at": row.description_updated_at,
            "lineage_count": int(lineage_count),
            "lineage_edges": lineage_edges,
            "related_metrics": related_metrics,
            "source_health": source_health,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "pii_flag": "PII" in sens,
            # PII 合规增强字段
            "pii_fields": pii_fields,
            "pii_field_count": sum(1 for f in pii_fields if not f.get("suppressed")),
            "pii_categories": sorted(
                {f["category"] for f in pii_fields if not f.get("suppressed")}
            ),
            "pii_overrides": pii_overrides,
            "compliance_reviewed": bool(row.compliance_reviewed),
            "compliance_reviewed_by": row.compliance_reviewed_by,
            "compliance_reviewed_at": row.compliance_reviewed_at,
            "masking_policy": row.masking_policy,
            "retention_days": row.retention_days,
            "legal_basis": row.legal_basis,
            "retention_expires_at": row.retention_expires_at,
            "retention_expiring": await self._retention_expiring(row),
            # etl_sql 属敏感字段（可能内嵌连接串），详情接口不返回
            "etl_sql": None,
        }

    @staticmethod
    def _merge_pii_fields(
        fields: list[dict[str, Any]], overrides: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """合并规则命中字段与人工标注（suppressed 覆盖规则判定；人工确认补入）。"""
        override_map = {o["column"]: o for o in overrides}
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for f in fields:
            seen.add(f["column"])
            ov = override_map.get(f["column"])
            if ov is not None:
                f["suppressed"] = ov["suppressed"]
                f["override_reason"] = ov["reason"]
            else:
                f["suppressed"] = False
                f["override_reason"] = None
            merged.append(f)
        # 人工确认是 PII 但规则未命中的字段（suppressed=False 覆盖）
        for col, ov in override_map.items():
            if col not in seen and not ov["suppressed"]:
                merged.append(
                    {
                        "column": col,
                        "category": "MANUAL",
                        "rule": "manual_confirm",
                        "confidence": 1.0,
                        "matched_by": "manual",
                        "suppressed": False,
                        "override_reason": ov["reason"],
                    }
                )
        return merged

    async def _entity_pii_fields(self, row: DBCatalog) -> list[dict[str, Any]]:
        """资产字段级 PII 命中明细（classification.pii_columns 为主，缺失时实时检测）。

        返回结构 ``[{column, category, rule, confidence, matched_by, suppressed,
        override_reason}]``，并按 ``pii_field_override`` 合并人工标注
        （suppressed=True 标注误报非 PII）。
        """
        fields: list[dict[str, Any]] = []
        row_data = row.schema_json if isinstance(row.schema_json, dict) else {}
        if row.sensitivity_level and "PII" in row.sensitivity_level.upper():
            classification = (
                await self._session.execute(
                    select(Classification)
                    .where(
                        Classification.catalog_id == row.id,
                        Classification.deleted_at.is_(None),
                    )
                    .order_by(Classification.created_at.desc())
                )
            ).scalar_one_or_none()
            if classification is not None and isinstance(classification.pii_columns, list):
                for item in classification.pii_columns:
                    if isinstance(item, dict) and item.get("column"):
                        fields.append(
                            {
                                "column": str(item["column"]),
                                "category": str(item.get("category") or "PII"),
                                "rule": str(item.get("rule") or ""),
                                "confidence": float(item.get("confidence") or 0),
                                "matched_by": str(item.get("matched_by") or "name"),
                            }
                        )
            if not fields:
                # 旧数据无 pii_columns 明细：实时检测补齐（best-effort）
                from app.services.collector.classifier import SensitivityClassifier

                for hit in SensitivityClassifier().detect_pii_fields(row.entity_name, row_data):
                    fields.append(
                        {
                            "column": hit.column,
                            "category": hit.category,
                            "rule": hit.rule,
                            "confidence": hit.confidence,
                            "matched_by": hit.matched_by,
                        }
                    )
        # 合并人工标注（suppressed 覆盖规则判定）
        overrides = await self._entity_pii_overrides(row.id)
        return self._merge_pii_fields(fields, overrides)

    async def _entity_pii_overrides(self, catalog_id: int) -> list[dict[str, Any]]:
        """查询资产字段级人工标注列表（含软删过滤）。"""
        rows = (
            await self._session.execute(
                select(PiiFieldOverride).where(
                    PiiFieldOverride.catalog_id == catalog_id,
                    PiiFieldOverride.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        return [
            {
                "id": r.id,
                "column": r.column_name,
                "suppressed": bool(r.suppressed),
                "reason": r.reason,
                "created_by": r.created_by,
                "created_at": r.created_at,
            }
            for r in rows
        ]

    async def _retention_expiring(self, row: DBCatalog) -> bool:
        """保留期是否临近到期（30 天内）或已到期。"""
        if row.retention_expires_at is None:
            return False
        horizon = datetime.now(UTC) + timedelta(days=30)
        return bool(row.retention_expires_at <= horizon)

    @staticmethod
    def _lineage_variants(entity_name: str) -> list[str]:
        """实体名的血缘节点编码形态（裸名/table:/field: 前缀）。"""
        return [entity_name, f"table:{entity_name}", f"field:{entity_name}"]

    async def _lineage_edges_for(self, variants: list[str], limit: int) -> list[dict[str, Any]]:
        """查询与某实体相关的血缘边明细（含类型/粒度/置信度/来源）。"""
        rows = (
            await self._session.execute(
                select(
                    LineageEdge.source_node,
                    LineageEdge.target_node,
                    LineageEdge.edge_type,
                    LineageEdge.granularity,
                    LineageEdge.confidence,
                    LineageEdge.provenance,
                )
                .where(
                    LineageEdge.deleted_at.is_(None),
                    or_(
                        LineageEdge.source_node.in_(variants),
                        LineageEdge.target_node.in_(variants),
                    ),
                )
                .limit(limit)
            )
        ).all()
        return [
            {
                "source": r.source_node,
                "target": r.target_node,
                "edge_type": r.edge_type,
                "granularity": r.granularity,
                "confidence": float(r.confidence or 0),
                "provenance": r.provenance,
            }
            for r in rows
        ]

    async def _related_metrics_for(self, variants: list[str]) -> list[dict[str, Any]]:
        """查询血缘下游指向该实体的关联指标（metric: 前缀节点）。"""
        rows = (
            await self._session.execute(
                select(LineageEdge.target_node, LineageEdge.edge_type)
                .where(
                    LineageEdge.deleted_at.is_(None),
                    LineageEdge.source_node.in_(variants),
                    LineageEdge.target_node.like("metric:%"),
                )
                .limit(50)
            )
        ).all()
        return [
            {
                "metric_node": r.target_node,
                "edge_type": r.edge_type,
            }
            for r in rows
        ]

    async def _source_health(self, source_id: str) -> dict[str, Any]:
        """查询数据源健康状态与最近健康检查时间（无则返回 unknown）。"""
        row = (
            await self._session.execute(
                select(
                    DataSource.health_status, DataSource.last_health_check, DataSource.name
                ).where(DataSource.source_id == source_id)
            )
        ).first()
        if row is None:
            return {"health_status": "unknown", "last_health_check": None, "source_name": None}
        return {
            "health_status": row.health_status,
            "last_health_check": row.last_health_check,
            "source_name": row.name,
        }

    async def _source_domain(self, source_id: str) -> str | None:
        """数据源所属业务域（db_catalog 无 domain 列，经 data_source 继承）。"""
        row = (
            await self._session.execute(
                select(DataSource.domain).where(
                    DataSource.source_id == source_id, DataSource.deleted_at.is_(None)
                )
            )
        ).first()
        return row[0] if row else None

    async def _owner_display_name(self, owner_id: int | None) -> str | None:
        """责任人可读名（display_name 优先，缺省回退 username）；无归属返回 None。"""
        if owner_id is None:
            return None
        row = (
            await self._session.execute(
                select(User.display_name, User.username).where(User.id == owner_id)
            )
        ).first()
        if row is None:
            return None
        return row[0] or row[1] or None

    async def enrich_catalog_items(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """批量给目录条目补源名称/业务域/责任人名（列表下钻与详情展示用，幂等）。"""
        if not items:
            return items
        source_ids = {it.get("source_id") for it in items if it.get("source_id")}
        owner_ids = {it.get("owner_id") for it in items if it.get("owner_id") is not None}
        src_map: dict[str, tuple[str | None, str | None]] = {}
        if source_ids:
            src_rows = (
                await self._session.execute(
                    select(DataSource.source_id, DataSource.name, DataSource.domain).where(
                        DataSource.source_id.in_(source_ids)
                    )
                )
            ).all()
            src_map = {r[0]: (r[1], r[2]) for r in src_rows}
        usr_map: dict[int, str] = {}
        if owner_ids:
            usr_rows = (
                await self._session.execute(
                    select(User.id, User.display_name, User.username).where(
                        User.id.in_(owner_ids)
                    )
                )
            ).all()
            usr_map = {r[0]: (r[1] or r[2]) for r in usr_rows}
        for it in items:
            sid = it.get("source_id")
            name, domain = src_map.get(sid, (None, None)) if sid is not None else (None, None)
            if it.get("source_name") is None:
                it["source_name"] = name
            if it.get("domain") is None:
                it["domain"] = domain
            oid = it.get("owner_id")
            if oid is not None and it.get("owner_name") is None:
                it["owner_name"] = usr_map.get(oid)
        return items

    async def catalog_summary(self) -> dict[str, Any]:
        total = (
            await self._session.execute(
                select(func.count()).select_from(DBCatalog).where(DBCatalog.deleted_at.is_(None))
            )
        ).scalar() or 0
        by_type = (
            await self._session.execute(
                select(DBCatalog.entity_type, func.count())
                .where(DBCatalog.deleted_at.is_(None))
                .group_by(DBCatalog.entity_type)
            )
        ).all()
        by_sens = (
            await self._session.execute(
                select(DBCatalog.sensitivity_level, func.count())
                .where(DBCatalog.deleted_at.is_(None))
                .group_by(DBCatalog.sensitivity_level)
            )
        ).all()
        orphans = (
            await self._session.execute(
                select(func.count())
                .select_from(DBCatalog)
                .where(DBCatalog.owner_id.is_(None), DBCatalog.deleted_at.is_(None))
            )
        ).scalar() or 0
        # 按数据源分布：LEFT JOIN 取源名称（含已软删源，名称保留追溯）；count 降序
        by_source = (
            await self._session.execute(
                select(DBCatalog.source_id, DataSource.name, func.count())
                .select_from(DBCatalog)
                .outerjoin(DataSource, DataSource.source_id == DBCatalog.source_id)
                .where(DBCatalog.deleted_at.is_(None))
                .group_by(DBCatalog.source_id, DataSource.name)
                .order_by(func.count().desc())
            )
        ).all()
        # 按库分布：entity_name 首段（库.表/库.表.字段），排除无点号异常行；count 降序
        db_expr = func.substring_index(DBCatalog.entity_name, ".", 1)
        by_database = (
            await self._session.execute(
                select(db_expr, func.count())
                .where(
                    DBCatalog.deleted_at.is_(None),
                    DBCatalog.entity_name.like("%.%"),
                )
                .group_by(db_expr)
                .order_by(func.count().desc())
            )
        ).all()
        # 目录资产 PII 合规：敏感资产（PII/CONFIDENTIAL）按敏感度 × 是否已复核聚合，
        # 合规率 = 已复核 / 敏感总数（无敏感资产视为 100%）。口径与 observability
        # pii_review_pending 一致（sensitivity_level IN (PII,CONFIDENTIAL) AND 未删）。
        pii_rows = (
            await self._session.execute(
                select(
                    DBCatalog.sensitivity_level,
                    DBCatalog.compliance_reviewed,
                    func.count(),
                )
                .where(
                    DBCatalog.deleted_at.is_(None),
                    DBCatalog.sensitivity_level.in_(["PII", "CONFIDENTIAL"]),
                )
                .group_by(DBCatalog.sensitivity_level, DBCatalog.compliance_reviewed)
            )
        ).all()
        pii_sens: dict[str, int] = {"PII": 0, "CONFIDENTIAL": 0}
        pii_reviewed = 0
        pii_pending = 0
        for sens, reviewed, cnt in pii_rows:
            n = int(cnt or 0)
            pii_sens[sens] = pii_sens.get(sens, 0) + n
            if reviewed:
                pii_reviewed += n
            else:
                pii_pending += n
        pii_total = pii_reviewed + pii_pending
        return {
            "total": total,
            "by_entity_type": dict(cast("Sequence[tuple[Any, Any]]", by_type)),
            "by_sensitivity": dict(cast("Sequence[tuple[Any, Any]]", by_sens)),
            "orphan_assets": orphans,
            "by_source": [
                {"source_id": sid, "source_name": sname or sid, "count": int(cnt or 0)}
                for sid, sname, cnt in by_source
            ],
            "by_database": [
                {"database": db, "count": int(cnt or 0)} for db, cnt in by_database
            ],
            "pii_compliance": {
                "sensitive_total": pii_total,
                "reviewed": pii_reviewed,
                "pending": pii_pending,
                "compliance_rate": (
                    round(pii_reviewed / pii_total * 100, 1) if pii_total else 100.0
                ),
                "by_sensitivity": pii_sens,
            },
        }

    async def classification_summary(self) -> dict[str, Any]:
        rows = (
            await self._session.execute(
                select(Classification.sensitivity_level, func.count()).group_by(
                    Classification.sensitivity_level
                )
            )
        ).all()
        return {"by_sensitivity": dict(cast("Sequence[tuple[Any, Any]]", rows))}

    async def metric_summary(self) -> dict[str, Any]:
        # C4（第七轮）：按域分布排除草稿/已废弃（对齐域树活跃指标口径，避免资产地图
        # 「指标总数」把 DRAFT/DEPRECATED 一并计入虚高）；按状态分布保留全量（分布视图）。
        by_domain = (
            await self._session.execute(
                select(Metric.domain, func.count())
                .where(
                    Metric.deleted_at.is_(None),
                    Metric.status.not_in(("DRAFT", "DEPRECATED")),
                )
                .group_by(Metric.domain)
            )
        ).all()
        by_status = (
            await self._session.execute(
                select(Metric.status, func.count())
                .where(Metric.deleted_at.is_(None))
                .group_by(Metric.status)
            )
        ).all()
        return {
            "by_domain": dict(cast("Sequence[tuple[Any, Any]]", by_domain)),
            "by_status": dict(cast("Sequence[tuple[Any, Any]]", by_status)),
        }

    async def _metric_distribution(self, column: Any) -> dict[str, int]:
        """按指定列聚合指标分布（null 值归入 ``__null__``，前端可显式展示）。"""
        rows = (
            await self._session.execute(
                select(column, func.count())
                .where(Metric.deleted_at.is_(None))
                .group_by(column)
            )
        ).all()
        out: dict[str, int] = {}
        for key, cnt in rows:
            out[str(key) if key is not None else "__null__"] = int(cnt or 0)
        return out

    async def metric_dimension_summary(self) -> dict[str, Any]:
        """指标体系聚合：指标多维分布 + PII 合规率。

        13 类维度：类型/粒度/分层/分级/单位/币种/聚合/时间语义/新鲜度/服务模式/可加性/状态/域。
        复用 SQL GROUP BY，与热力聚合同源（TD §12.11），避免指标体系口径漂移。
        """
        # 合规率：已复核 PII 指标 / 全部 PII 指标。
        # 排除 DEPRECATED——已废弃指标已下线、不再对外服务，其复核状态无意义，
        # 不应参与合规统计（否则废弃测试指标会撑起虚假合规率）。
        pii_total = (
            await self._session.execute(
                select(func.count())
                .select_from(Metric)
                .where(
                    Metric.deleted_at.is_(None),
                    Metric.pii_flag.is_(True),
                    Metric.status != "DEPRECATED",
                )
            )
        ).scalar() or 0
        pii_reviewed = (
            await self._session.execute(
                select(func.count())
                .select_from(Metric)
                .where(
                    Metric.deleted_at.is_(None),
                    Metric.pii_flag.is_(True),
                    Metric.compliance_reviewed.is_(True),
                    Metric.status != "DEPRECATED",
                )
            )
        ).scalar() or 0
        metric_total = (
            await self._session.execute(
                select(func.count()).select_from(Metric).where(Metric.deleted_at.is_(None))
            )
        ).scalar() or 0

        return {
            "by_type": await self._metric_distribution(Metric.type),
            "by_granularity": await self._metric_distribution(Metric.granularity),
            "by_dw_layer": await self._metric_distribution(Metric.dw_layer),
            "by_metric_tier": await self._metric_distribution(Metric.metric_tier),
            "by_unit": await self._metric_distribution(Metric.unit),
            "by_currency": await self._metric_distribution(Metric.currency),
            "by_aggregation": await self._metric_distribution(Metric.aggregation),
            "by_time_semantics": await self._metric_distribution(Metric.time_semantics),
            "by_freshness": await self._metric_distribution(Metric.freshness),
            "by_serving_mode": await self._metric_distribution(Metric.serving_mode),
            "by_additivity": await self._metric_distribution(Metric.additivity),
            "by_status": await self._metric_distribution(Metric.status),
            "by_domain": await self._metric_distribution(Metric.domain),
            "pii_compliance": {
                "pii_total": int(pii_total),
                "pii_reviewed": int(pii_reviewed),
                "pii_unreviewed": int(pii_total - pii_reviewed),
                # 无 PII 指标时为 None（前端展示「暂无 PII 指标」空态），
                # 与「有 PII 但 0% 合规」区分开，避免 0/100% 误导。
                "review_rate": round(float(pii_reviewed) / pii_total, 4) if pii_total else None,
            },
            "total": int(metric_total),
        }


    # ----------------------------------------------------------------
    # P2 Enhancement: 图谱降级、热力聚合、责任人视图
    # ----------------------------------------------------------------

    # 图谱表/视图节点上限（力导向图可读性：节点过多会失去地图形态）
    _GRAPH_CATALOG_LIMIT = 200

    async def _graph_metric_nodes(
        self, domain: str | None, pii_only: bool
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """metric 节点：id=``metric:{code}``，按域/PII 过滤。"""
        metric_stmt = select(
            Metric.metric_code,
            Metric.domain,
            Metric.pii_flag,
            Metric.owner_id,
            Metric.status,
        ).where(Metric.deleted_at.is_(None))
        if domain:
            metric_stmt = metric_stmt.where(Metric.domain == domain)
        if pii_only:
            metric_stmt = metric_stmt.where(Metric.pii_flag.is_(True))

        rows = (await self._session.execute(metric_stmt)).all()
        nodes: list[dict[str, Any]] = []
        allowed: set[str] = set()
        seen: set[str] = set()
        for row in rows:
            node_id = f"metric:{row.metric_code}"
            if node_id in seen:
                continue
            seen.add(node_id)
            allowed.add(node_id)
            nodes.append(
                {
                    "id": node_id,
                    "type": "metric",
                    "label": row.metric_code,
                    "pii": bool(row.pii_flag),
                    "domain": row.domain,
                    "owner": str(row.owner_id) if row.owner_id else None,
                }
            )
        return nodes, allowed

    async def _graph_lineage_table_names(self) -> set[str]:
        """血缘边引用的表/视图名集合（``table:`` 前缀节点），用于优先让业务表进图。

        若直接全量取 db_catalog 表并按插入序 LIMIT，会混入已删除探针源的系统表，
        导致 catalog 节点与血缘边（业务表）无法匹配、图退化为孤立散点。
        """
        rows = (
            (
                await self._session.execute(
                    select(LineageEdge.source_node).where(
                        LineageEdge.deleted_at.is_(None),
                        LineageEdge.source_node.like("table:%"),
                    )
                )
            )
            .scalars()
            .all()
        )
        return {r.split(":", 1)[1] for r in rows}

    async def _graph_catalog_nodes(
        self, pii_only: bool, lineage_tables: set[str]
    ) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
        """db_catalog 表/视图节点：id=``table:{entity_name}``（与血缘边格式对齐）。

        优先血缘边引用的表（``lineage_tables``）并排除已删除数据源，保证节点与边
        连通、图呈现真实血缘结构；域从 ``data_source.domain`` 继承（db_catalog 无
        域字段）；PII 由 ``sensitivity_level`` 含 "PII" 判定。
        """
        filters: list[Any] = [
            DBCatalog.deleted_at.is_(None),
            DBCatalog.entity_type.in_(["TABLE", "VIEW"]),
            DataSource.deleted_at.is_(None),
        ]
        if lineage_tables:
            filters.append(DBCatalog.entity_name.in_(lineage_tables))
        if pii_only:
            filters.append(DBCatalog.sensitivity_level.like("%PII%"))

        catalog_stmt = (
            select(
                DBCatalog.id,
                DBCatalog.entity_name,
                DBCatalog.entity_type,
                DBCatalog.sensitivity_level,
                DBCatalog.owner_id,
                DataSource.domain,
            )
            .join(DataSource, DataSource.source_id == DBCatalog.source_id)
            .where(*filters)
        )
        rows = (await self._session.execute(catalog_stmt.limit(self._GRAPH_CATALOG_LIMIT))).all()
        nodes: list[dict[str, Any]] = []
        domain_by_id: dict[str, str | None] = {}
        seen: set[str] = set()
        for row in rows:
            node_id = f"table:{row.entity_name}"
            if node_id in seen:
                continue
            seen.add(node_id)
            domain_by_id[node_id] = row.domain
            nodes.append(
                {
                    "id": node_id,
                    "type": "table",
                    "label": row.entity_name,
                    "entity_id": row.id,
                    "pii": bool(row.sensitivity_level and "PII" in row.sensitivity_level),
                    "domain": row.domain,
                    "owner": str(row.owner_id) if row.owner_id else None,
                }
            )
        return nodes, domain_by_id

    async def graph_from_mysql(
        self, domain: str | None, pii_only: bool, depth: int | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """从 MySQL lineage_edge + metric + db_catalog 拼接图谱数据。

        - 节点：metric（``metric:{code}``）+ db_catalog 表/视图（``table:{entity_name}``，
          与血缘边节点格式对齐）+ 血缘边引用到的字段（``field:{...}``，数量受控）。
        - 边：仅保留至少一端属于展示节点的边（**精确 IN 集合匹配**，消除 ``contains``
          子串误匹配）。
        - PII 视图：仅指标/表节点（字段级 PII 无法从血缘边判定，故不展示字段）。
        - ``depth``：从指标出发沿血缘边 BFS 收敛（None=全量不过滤）。值越小图越聚焦：
          depth=1 仅指标与其直连表，depth=2 展开一层中间表，以此类推——避免
          "节点很多时一团乱麻"，同时保留血缘语义（指标是下游汇聚点）。
        """
        metric_nodes, allowed = await self._graph_metric_nodes(domain, pii_only)
        lineage_tables = await self._graph_lineage_table_names()
        catalog_nodes, catalog_domain = await self._graph_catalog_nodes(pii_only, lineage_tables)
        allowed.update(catalog_domain)
        nodes: list[dict[str, Any]] = metric_nodes + catalog_nodes

        if not allowed:
            # 无展示节点则无有效边
            return nodes, []

        edge_stmt = select(
            LineageEdge.source_node,
            LineageEdge.target_node,
            LineageEdge.edge_type,
        ).where(
            LineageEdge.deleted_at.is_(None),
            or_(
                LineageEdge.source_node.in_(allowed),
                LineageEdge.target_node.in_(allowed),
            ),
        )
        edge_rows = (await self._session.execute(edge_stmt.limit(1000))).all()

        # 血缘边引用的字段节点（数量受控）：域继承对端表/视图
        field_seen: set[str] = set()
        field_nodes: list[dict[str, Any]] = []
        for row in edge_rows:
            for node_id in (row.source_node, row.target_node):
                if not node_id.startswith("field:") or node_id in field_seen:
                    continue
                if pii_only:
                    # 字段级 PII 无法从血缘边判定，PII 视图不展示字段节点
                    continue
                field_seen.add(node_id)
                other = row.target_node if node_id == row.source_node else row.source_node
                field_nodes.append(
                    {
                        "id": node_id,
                        "type": "field",
                        "label": node_id.split(":", 1)[1],
                        "pii": False,
                        "domain": catalog_domain.get(other),
                        "owner": None,
                    }
                )

        edges = [
            {
                "source": str(row.source_node),
                "target": str(row.target_node),
                "type": str(row.edge_type),
            }
            for row in edge_rows
        ]
        if depth is not None and depth > 0:
            nodes, edges = _prune_graph_by_depth(
                nodes + field_nodes, edges, depth, metric_nodes
            )
            return nodes, edges
        return nodes + field_nodes, edges

    async def heatmap_matrix(self, asset_type: str = "catalog") -> dict[str, Any]:
        """二维热力矩阵：业务域 × 敏感级别的资产分布。

        Args:
            asset_type: 资产视角。``catalog``=目录资产（db_catalog 表/视图/字段，
                域从 ``data_source.domain`` 继承）；``metric``=指标资产
                （metric.pii_flag → PII / 内部 两列）。

        ``columns`` 固定为完整敏感级枚举（catalog）或 PII/内部（metric），
        保证前端坐标轴稳定（空矩阵也返回全轴）。
        """
        if asset_type == "metric":
            rows = (
                await self._session.execute(
                    select(
                        Metric.domain,
                        Metric.pii_flag,
                        func.count().label("total"),
                    )
                    .where(Metric.deleted_at.is_(None))
                    .group_by(Metric.domain, Metric.pii_flag)
                )
            ).all()
            cells = [
                {
                    "domain": r[0],
                    "sensitivity": "PII" if r[1] else "INTERNAL",
                    "count": r[2],
                    "pii_count": r[2] if r[1] else 0,
                }
                for r in rows
            ]
            return {"cells": cells, "columns": ["INTERNAL", "PII"]}
        catalog_rows = (
            await self._session.execute(
                select(
                    DataSource.domain,
                    DBCatalog.sensitivity_level,
                    func.count().label("total"),
                )
                .join(DataSource, DataSource.source_id == DBCatalog.source_id)
                .where(DBCatalog.deleted_at.is_(None))
                .group_by(DataSource.domain, DBCatalog.sensitivity_level)
            )
        ).all()
        cells = [
            {
                "domain": r[0],
                "sensitivity": r[1],
                "count": r[2],
                "pii_count": r[2] if (r[1] and "PII" in r[1]) else 0,
            }
            for r in catalog_rows
        ]
        return {
            "cells": cells,
            "columns": [e.value for e in SensitivityLevelEnum],
        }

    async def heatmap_aggregation(self, dimension: str) -> dict[str, Any]:
        """按维度聚合返回热力桶数据。

        Args:
            dimension: 聚合维度 domain / sensitivity / owner / dw_layer。
        """
        if dimension == "sensitivity":
            rows = (
                await self._session.execute(
                    select(DBCatalog.sensitivity_level, func.count())
                    .where(DBCatalog.deleted_at.is_(None))
                    .group_by(DBCatalog.sensitivity_level)
                )
            ).all()
            buckets = [{"key": r[0], "count": r[1]} for r in rows]
        elif dimension == "owner":
            rows = (
                await self._session.execute(
                    select(
                        Metric.owner_id,
                        func.count().label("total"),
                        func.sum(case((Metric.pii_flag.is_(True), 1), else_=0)).label("pii_count"),
                    )
                    .where(Metric.deleted_at.is_(None))
                    .group_by(Metric.owner_id)
                )
            ).all()
            buckets = [{"key": str(r[0]), "total": r[1], "pii_count": int(r[2] or 0)} for r in rows]
        elif dimension == "dw_layer":
            rows = (
                await self._session.execute(
                    select(Metric.dw_layer, func.count())
                    .where(Metric.deleted_at.is_(None))
                    .group_by(Metric.dw_layer)
                )
            ).all()
            buckets = [{"key": r[0], "count": r[1]} for r in rows]
        else:
            # 默认按 domain 聚合
            rows = (
                await self._session.execute(
                    select(
                        Metric.domain,
                        func.count().label("total"),
                        func.sum(case((Metric.pii_flag.is_(True), 1), else_=0)).label("pii_count"),
                    )
                    .where(Metric.deleted_at.is_(None))
                    .group_by(Metric.domain)
                )
            ).all()
            buckets = [{"key": r[0], "total": r[1], "pii_count": int(r[2] or 0)} for r in rows]

        return {"dimension": dimension, "buckets": buckets}

    async def owner_aggregation(self, owner_id: int) -> dict[str, Any]:
        """按责任人聚合资产统计（指标多维度分布 + 目录明细 + 待办）。

        Returns:
            ``{owner_id, metrics:{total,published,draft,pii_count,by_domain,
            by_type,by_metric_tier,snapshot_covered,todo}, catalogs:{total,
            items:[...]}}``。``catalogs.items`` 为目录明细（可下钻），替代纯数字。
        """
        metric_stats = await self._owner_metric_stats(owner_id)
        by_domain = await self._owner_distribution(owner_id, Metric.domain)
        by_type = await self._owner_distribution(owner_id, Metric.type)
        by_tier = await self._owner_distribution(owner_id, Metric.metric_tier)
        todo = await self._owner_todo(owner_id)
        catalogs = await self._owner_catalog_items(owner_id)
        snapshot_covered = await self._owner_snapshot_covered(owner_id)
        # 责任人档案：姓名/角色/所属域（真实姓名优先，回退 username）
        owner_profile = await self._owner_profile(owner_id)

        return {
            "owner_id": owner_id,
            "owner_name": owner_profile[0],
            "role": owner_profile[1],
            "domain": owner_profile[2],
            "metrics": {
                "total": metric_stats.total or 0,
                "published": int(metric_stats.published or 0),
                "draft": int(metric_stats.draft or 0),
                "pii_count": int(metric_stats.pii_count or 0),
                "by_domain": dict(cast("Sequence[tuple[Any, Any]]", by_domain)),
                "by_type": dict(cast("Sequence[tuple[Any, Any]]", by_type)),
                "by_metric_tier": dict(cast("Sequence[tuple[Any, Any]]", by_tier)),
                "snapshot_covered": snapshot_covered,
                "todo": todo,
            },
            "catalogs": {"total": len(catalogs), "items": catalogs},
        }

    async def _owner_profile(self, owner_id: int) -> tuple[str | None, str | None, str | None]:
        """责任人档案：``(display_name|username, role, domain)``；
        用户不存在返回 ``(None, None, None)``。"""
        row = (
            await self._session.execute(
                select(User.display_name, User.username, User.role, User.domain).where(
                    User.id == owner_id
                )
            )
        ).first()
        if row is None:
            return None, None, None
        name = row[0] or row[1] or None
        return name, row[2], row[3]

    async def _owner_metric_stats(self, owner_id: int) -> Any:
        """责任人指标核心统计（总量/发布/草稿/PII）。"""
        return (
            await self._session.execute(
                select(
                    func.count().label("total"),
                    func.sum(case((Metric.status == "PUBLISHED", 1), else_=0)).label("published"),
                    func.sum(case((Metric.status == "DRAFT", 1), else_=0)).label("draft"),
                    func.sum(case((Metric.pii_flag.is_(True), 1), else_=0)).label("pii_count"),
                ).where(Metric.owner_id == owner_id, Metric.deleted_at.is_(None))
            )
        ).one()

    async def _owner_distribution(self, owner_id: int, column: Any) -> list[Any]:
        """责任人指标按列分布（域/类型/分级）。"""
        rows = (
            await self._session.execute(
                select(column, func.count())
                .where(Metric.owner_id == owner_id, Metric.deleted_at.is_(None))
                .group_by(column)
            )
        ).all()
        return list(rows)

    async def _owner_todo(self, owner_id: int) -> dict[str, Any]:
        """责任人待办：PII 未复核、废弃未替换、无快照指标数。"""
        unreviewed = (
            await self._session.execute(
                select(func.count())
                .select_from(Metric)
                .where(
                    Metric.owner_id == owner_id,
                    Metric.deleted_at.is_(None),
                    Metric.pii_flag.is_(True),
                    Metric.compliance_reviewed.is_(False),
                )
            )
        ).scalar() or 0
        deprecated_orphan = (
            await self._session.execute(
                select(func.count())
                .select_from(Metric)
                .where(
                    Metric.owner_id == owner_id,
                    Metric.deleted_at.is_(None),
                    Metric.status == "DEPRECATED",
                    Metric.successor_code.is_(None),
                )
            )
        ).scalar() or 0
        return {
            "pii_unreviewed": int(unreviewed),
            "deprecated_without_successor": int(deprecated_orphan),
        }

    async def _owner_catalog_items(self, owner_id: int) -> list[dict[str, Any]]:
        """责任人目录明细（entity_name/类型/敏感度/源/更新时间，可下钻）。"""
        rows = (
            await self._session.execute(
                select(
                    DBCatalog.id,
                    DBCatalog.entity_name,
                    DBCatalog.entity_type,
                    DBCatalog.sensitivity_level,
                    DBCatalog.source_id,
                    DBCatalog.updated_at,
                )
                .where(DBCatalog.owner_id == owner_id, DBCatalog.deleted_at.is_(None))
                .limit(100)
            )
        ).all()
        items = [
            {
                "id": r.id,
                "entity_name": r.entity_name,
                "entity_type": r.entity_type,
                "sensitivity_level": r.sensitivity_level,
                "source_id": r.source_id,
                "owner_id": owner_id,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]
        return await self.enrich_catalog_items(items)

    async def _owner_snapshot_covered(self, owner_id: int) -> int:
        """责任人指标中有快照的数量（覆盖度分子）。"""
        codes = set(
            (
                await self._session.execute(
                    select(Metric.metric_code).where(
                        Metric.owner_id == owner_id, Metric.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        if not codes:
            return 0
        covered = set(
            (
                await self._session.execute(
                    select(MetricValueSnapshot.metric_code)
                    .where(MetricValueSnapshot.metric_code.in_(codes))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        return len(covered)

    # ----------------------------------------------------------------
    # 产品补充（FR-18 生产化）：全局搜索 / 健康 / PII / 变更 / 我的资产
    # ----------------------------------------------------------------

    @staticmethod
    def _escape_like(text: str) -> str:
        """转义 LIKE 通配符，防止用户输入 `%`/`_` 做全表模糊放大。

        修复前：转义为 \\% 但调用方 like() 无 escape 参数不生成 ESCAPE 子句，
        MySQL 默认把 \\ 当普通字符、%/_ 仍当通配符 → 转义实际失效。
        现用 / 作转义符（转义 //、/% 和 /_），配合 like(..., escape="/")。
        """
        return text.replace("/", "//").replace("%", "/%").replace("_", "/_")

    async def search_assets(
        self, q: str, entity_type: str | None, limit: int, org_id: int | None = None
    ) -> list[dict[str, Any]]:
        """全局资产搜索：表/字段/指标三级，返回完整信息（源/责任人/口径/描述）。

        Returns:
            统一 ``{type, id, name, entity_type, sensitivity_level, domain, owner_id,
            owner_name, source_id, source_name, description, updated_at, status}``
            结构；metric 额外带 type/granularity/unit/aggregation/freshness/
            metric_tier/dw_layer。LIKE 通配符已转义（防模糊放大）。
        """
        if not q.strip():
            return []
        needle = f"%{self._escape_like(q.strip())}%"
        results: list[dict[str, Any]] = []
        want_table = entity_type is None or entity_type in ("table", "view")
        want_field = entity_type is None or entity_type == "field"
        want_metric = entity_type is None or entity_type == "metric"

        # 表级（entity_name/source_id 模糊，FULLTEXT 加速）
        if want_table:
            results.extend(
                await self._search_catalog_tables(q, entity_type, limit, org_id=org_id)
            )
        # 字段级（schema_json 字段名模糊）
        if want_field:
            results.extend(await self._search_fields(q, limit, org_id=org_id))
        # 指标级（metric_code / name 模糊）
        if want_metric:
            results.extend(await self._search_metrics(needle, limit))
        return results

    async def _search_catalog_tables(
        self, keyword: str, entity_type: str | None, limit: int, org_id: int | None = None
    ) -> list[dict[str, Any]]:
        """表/视图级搜索结果（含源/责任人/描述/字段数富集）。"""
        cond = await self._catalog_name_cond(keyword)
        stmt = select(DBCatalog).where(DBCatalog.deleted_at.is_(None))
        if cond is not None:
            stmt = stmt.where(cond)
        if entity_type:
            stmt = stmt.where(DBCatalog.entity_type == entity_type)
        # P1 多租户隔离：仅搜索本组织数据源资产
        if org_id is not None:
            stmt = (
                stmt.join(DataSource, DataSource.source_id == DBCatalog.source_id)
                .where(DataSource.deleted_at.is_(None), DataSource.org_id == org_id)
            )
        rows = (await self._session.execute(stmt.limit(limit))).scalars().all()
        items: list[dict[str, Any]] = []
        for r in rows:
            schema = r.schema_json if isinstance(r.schema_json, dict) else {}
            fields = schema.get("fields") or schema.get("columns") or []
            items.append(
                {
                    "type": "catalog",
                    "id": r.id,
                    "name": r.entity_name,
                    "entity_type": r.entity_type,
                    "sensitivity_level": r.sensitivity_level,
                    "domain": None,
                    "owner_id": r.owner_id,
                    "source_id": r.source_id,
                    "description": r.description,
                    "column_count": len(fields) if isinstance(fields, list) else None,
                    "updated_at": r.updated_at,
                    "status": None,
                }
            )
        return await self.enrich_catalog_items(items)

    async def _search_fields(
        self, q: str, limit: int, org_id: int | None = None
    ) -> list[dict[str, Any]]:
        """字段级搜索结果：扫 schema_json 字段名，返回 ``{table}.{field}`` 项。

        字段名匹配用原始关键词（不转义 LIKE 通配符），因为这里走内存包含判断
        而非 SQL LIKE——``_escape_like`` 会把 `_` 转成 `\\_` 导致匹配失败。
        """
        q_lower = q.strip().lower()
        if not q_lower:
            return []
        stmt = select(DBCatalog).where(DBCatalog.deleted_at.is_(None)).limit(1000)
        # P1 多租户隔离：仅搜索本组织数据源资产
        if org_id is not None:
            stmt = (
                select(DBCatalog)
                .join(DataSource, DataSource.source_id == DBCatalog.source_id)
                .where(
                    DBCatalog.deleted_at.is_(None),
                    DataSource.deleted_at.is_(None),
                    DataSource.org_id == org_id,
                )
                .limit(1000)
            )
        rows = (await self._session.execute(stmt)).scalars().all()
        results: list[dict[str, Any]] = []
        for r in rows:
            schema = r.schema_json if isinstance(r.schema_json, dict) else {}
            fields = schema.get("fields") or schema.get("columns") or []
            if not isinstance(fields, list):
                continue
            for f in fields:
                if not isinstance(f, dict):
                    continue
                col = str(f.get("name") or f.get("column") or "")
                if col and q_lower in col.lower():
                    results.append(
                        {
                            "type": "field",
                            "id": r.id,
                            "name": f"{r.entity_name}.{col}",
                            "entity_type": "field",
                            "sensitivity_level": r.sensitivity_level,
                            "domain": None,
                            "owner_id": r.owner_id,
                            "source_id": r.source_id,
                            "description": f.get("comment"),
                            "column_count": None,
                            "updated_at": r.updated_at,
                            "status": None,
                        }
                    )
                    if len(results) >= limit:
                        return await self.enrich_catalog_items(results)
        return await self.enrich_catalog_items(results)

    async def _search_metrics(self, needle: str, limit: int) -> list[dict[str, Any]]:
        """指标级搜索结果（含治理一等字段：类型/粒度/单位/聚合/新鲜度/分级/分层）。"""
        stmt = select(Metric).where(
            Metric.deleted_at.is_(None),
            or_(Metric.metric_code.like(needle, escape="/"), Metric.name.like(needle, escape="/")),
        )
        rows = (await self._session.execute(stmt.limit(limit))).scalars().all()
        owner_ids = {m.owner_id for m in rows if m.owner_id is not None}
        usr_map: dict[int, str] = {}
        if owner_ids:
            usr_rows = (
                await self._session.execute(
                    select(User.id, User.display_name, User.username).where(
                        User.id.in_(owner_ids)
                    )
                )
            ).all()
            usr_map = {r[0]: (r[1] or r[2]) for r in usr_rows}
        return [
            {
                "type": "metric",
                "id": m.id,
                "name": m.metric_code,
                "entity_type": "metric",
                "sensitivity_level": "PII" if m.pii_flag else "INTERNAL",
                "domain": m.domain,
                "owner_id": m.owner_id,
                "owner_name": usr_map.get(m.owner_id),
                "status": m.status,
                "metric_type": m.type,
                "granularity": m.granularity,
                "unit": m.unit,
                "aggregation": m.aggregation,
                "time_semantics": m.time_semantics,
                "freshness": m.freshness,
                "dw_layer": m.dw_layer,
                "metric_tier": m.metric_tier,
                "additivity": m.additivity,
                "serving_mode": m.serving_mode,
                "description": m.description,
                "updated_at": m.updated_at,
            }
            for m in rows
        ]

    async def health_summary(self, org_id: int | None = None) -> dict[str, Any]:
        """资产健康视图：9 项体检 + 健康评分。

        Returns:
            ``{score, level, checks, unhealthy_sources, schema_incomplete,
            orphan_assets, stale_assets, stale_days}``。
            ``checks`` 为逐项体检明细（name/count/deduct/details），前端据此渲染
            健康报告与下钻。评分规则见 ``_health_level``。
        """
        checks: list[dict[str, Any]] = []
        score = 100

        # 体检 1：不健康数据源
        unhealthy = await self._health_unhealthy_sources(org_id)
        score -= min(len(unhealthy) * 5, 15)
        checks.append({"key": "unhealthy_sources", "count": len(unhealthy), "deduct": 0})

        # 体检 2：schema 不完整目录
        incomplete = await self._health_schema_incomplete(org_id)
        score -= min(len(incomplete) * 2, 10)
        checks.append({"key": "schema_incomplete", "count": len(incomplete), "deduct": 0})

        # 体检 3：孤儿资产
        orphan_stmt = (
            select(func.count())
            .select_from(DBCatalog)
            .where(DBCatalog.owner_id.is_(None), DBCatalog.deleted_at.is_(None))
        )
        if org_id is not None:
            orphan_stmt = orphan_stmt.join(
                DataSource, DataSource.source_id == DBCatalog.source_id
            ).where(DataSource.deleted_at.is_(None), DataSource.org_id == org_id)
        orphan_count = (await self._session.execute(orphan_stmt)).scalar() or 0
        score -= min(int(orphan_count) // 10, 10)
        checks.append({"key": "orphan_assets", "count": int(orphan_count), "deduct": 0})

        # 体检 4：陈旧资产（7 天未更新）
        stale_days = 7
        stale = await self._health_stale_assets(stale_days, org_id)
        score -= min(len(stale), 10)
        checks.append({"key": "stale_assets", "count": len(stale), "deduct": 0})

        # 体检 5/6：表描述缺失 / 字段描述缺失（按组织隔离）
        desc_missing, field_missing, field_total = await self._health_descriptions(org_id)
        score -= min(desc_missing // 10, 10)
        checks.append({"key": "tables_missing_desc", "count": desc_missing, "deduct": 0})
        score -= min(field_missing // 100, 10)
        checks.append(
            {
                "key": "fields_missing_desc",
                "count": field_missing,
                "field_total": field_total,
                "deduct": 0,
            }
        )

        # 体检 7/8/9：PII 未复核 / 无快照 / 废弃未替换
        pii_unreviewed, no_snapshot, deprecated_orphan = await self._health_metric_checks()
        score -= min(len(pii_unreviewed) * 5, 15)
        checks.append({"key": "pii_unreviewed", "count": len(pii_unreviewed), "deduct": 0})
        score -= min(len(no_snapshot) * 2, 10)
        checks.append(
            {"key": "metrics_without_snapshot", "count": len(no_snapshot), "deduct": 0}
        )
        score -= min(len(deprecated_orphan) * 3, 10)
        checks.append(
            {
                "key": "deprecated_without_successor",
                "count": len(deprecated_orphan),
                "deduct": 0,
            }
        )

        return {
            "score": max(score, 0),
            "level": self._health_level(score),
            "checks": checks,
            "unhealthy_sources": unhealthy,
            "schema_incomplete": incomplete,
            "orphan_assets": int(orphan_count),
            "stale_assets": stale,
            "stale_days": stale_days,
            "pii_unreviewed": pii_unreviewed,
            "metrics_without_snapshot": no_snapshot,
            "deprecated_without_successor": deprecated_orphan,
        }

    @staticmethod
    def _health_level(score: int) -> str:
        """健康评分分档：>=90 优 / >=75 良 / >=60 中 / <60 差。"""
        if score >= 90:
            return "excellent"
        if score >= 75:
            return "good"
        if score >= 60:
            return "fair"
        return "poor"

    async def _health_unhealthy_sources(
        self, org_id: int | None = None
    ) -> list[dict[str, Any]]:
        """体检 1：健康状态为 unhealthy 的数据源列表（P1：按组织隔离）。"""
        stmt = select(
            DataSource.source_id, DataSource.name, DataSource.health_status
        ).where(DataSource.health_status == "unhealthy", DataSource.deleted_at.is_(None))
        if org_id is not None:
            stmt = stmt.where(DataSource.org_id == org_id)
        rows = (await self._session.execute(stmt)).all()
        return [
            {"source_id": r.source_id, "name": r.name, "health_status": r.health_status}
            for r in rows
        ]

    async def _health_schema_incomplete(
        self, org_id: int | None = None
    ) -> list[dict[str, Any]]:
        """体检 2：schema 不完整（缺列元数据）的目录列表（P1：按组织隔离）。"""
        stmt = (
            select(DBCatalog.id, DBCatalog.entity_name, DBCatalog.source_id)
            .where(
                DBCatalog.deleted_at.is_(None),
                DBCatalog.schema_incomplete.is_(True),
            )
            .limit(100)
        )
        if org_id is not None:
            stmt = (
                stmt.join(DataSource, DataSource.source_id == DBCatalog.source_id)
                .where(DataSource.deleted_at.is_(None), DataSource.org_id == org_id)
            )
        rows = (await self._session.execute(stmt)).all()
        return [
            {"id": r.id, "entity_name": r.entity_name, "source_id": r.source_id}
            for r in rows
        ]

    async def _health_stale_assets(
        self, days: int, org_id: int | None = None
    ) -> list[dict[str, Any]]:
        """体检 4：N 天未更新的陈旧目录资产（数据源采集停滞信号，P1：按组织隔离）。"""
        stale_cutoff = datetime.now(UTC) - timedelta(days=days)
        stmt = (
            select(DBCatalog.id, DBCatalog.entity_name, DBCatalog.updated_at)
            .where(
                DBCatalog.deleted_at.is_(None),
                DBCatalog.updated_at < stale_cutoff,
            )
            .limit(100)
        )
        if org_id is not None:
            stmt = (
                stmt.join(DataSource, DataSource.source_id == DBCatalog.source_id)
                .where(DataSource.deleted_at.is_(None), DataSource.org_id == org_id)
            )
        rows = (await self._session.execute(stmt)).all()
        return [
            {"id": r.id, "entity_name": r.entity_name, "updated_at": r.updated_at}
            for r in rows
        ]

    async def _health_descriptions(self, org_id: int | None = None) -> tuple[int, int, int]:
        """体检 5/6：表描述缺失数 / 字段描述缺失数 / 字段总数。

        表级：``db_catalog.description`` 为空；字段级：schema_json 字段总数减去
        column_descriptions 已覆盖数（一次全表扫描，30s 缓存兜底性能）。
        ``org_id`` 非 None 时经 ``db_catalog → data_source.org_id`` 按组织隔离
        （数据源资产描述治理是组织内事务，防跨组织混入健康评分）。
        """
        catalog_org_filter = None
        if org_id is not None:
            catalog_org_filter = (
                DBCatalog.source_id == DataSource.source_id,
                DataSource.deleted_at.is_(None),
                DataSource.org_id == org_id,
            )
        stmt = select(DBCatalog.description, DBCatalog.schema_json).where(
            DBCatalog.deleted_at.is_(None)
        )
        if catalog_org_filter is not None:
            stmt = stmt.where(*catalog_org_filter)
        rows = (await self._session.execute(stmt)).all()
        tables_missing = sum(1 for r in rows if not r.description)
        field_total = 0
        for r in rows:
            schema = r.schema_json if isinstance(r.schema_json, dict) else {}
            fields = schema.get("fields") or schema.get("columns") or []
            if isinstance(fields, list):
                field_total += len(fields)
        covered_stmt = (
            select(func.count()).select_from(ColumnDescription).where(
                ColumnDescription.deleted_at.is_(None)
            )
        )
        if org_id is not None:
            covered_stmt = (
                covered_stmt.join(DBCatalog, DBCatalog.id == ColumnDescription.catalog_id)
                .join(DataSource, DataSource.source_id == DBCatalog.source_id)
                .where(DataSource.deleted_at.is_(None), DataSource.org_id == org_id)
            )
        covered = (await self._session.execute(covered_stmt)).scalar() or 0
        return tables_missing, max(field_total - int(covered), 0), int(field_total)

    async def _health_metric_checks(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """体检 7/8/9：PII 未复核 / 无快照 / 废弃未替换指标列表。

        无快照判断：以存在任何快照记录的指标码集合为基准，未命中的视为无快照。
        平台级合规项：指标（Metric）无组织归属维度（血缘可达多组织表），
        PII 复核/快照/废弃治理本为平台统一口径，按全量统计——
        组织视角的指标隔离由指标目录行级可见性（visible_actor_id）承担。
        """
        pii_unreviewed_rows = (
            await self._session.execute(
                select(Metric.metric_code, Metric.name, Metric.owner_id)
                .where(
                    Metric.deleted_at.is_(None),
                    Metric.pii_flag.is_(True),
                    Metric.compliance_reviewed.is_(False),
                )
                .limit(100)
            )
        ).all()
        pii_unreviewed = [
            {"metric_code": r.metric_code, "name": r.name, "owner_id": r.owner_id}
            for r in pii_unreviewed_rows
        ]

        snapshot_codes = set(
            (
                await self._session.execute(
                    select(MetricValueSnapshot.metric_code).distinct()
                )
            )
            .scalars()
            .all()
        )
        no_snapshot_rows = (
            await self._session.execute(
                select(Metric.metric_code, Metric.name)
                .where(Metric.deleted_at.is_(None))
                .limit(500)
            )
        ).all()
        no_snapshot = [
            {"metric_code": r.metric_code, "name": r.name}
            for r in no_snapshot_rows
            if r.metric_code not in snapshot_codes
        ]

        deprecated_rows = (
            await self._session.execute(
                select(Metric.metric_code, Metric.name)
                .where(
                    Metric.deleted_at.is_(None),
                    Metric.status == "DEPRECATED",
                    Metric.successor_code.is_(None),
                )
                .limit(100)
            )
        ).all()
        deprecated_orphan = [
            {"metric_code": r.metric_code, "name": r.name} for r in deprecated_rows
        ]
        return pii_unreviewed, no_snapshot, deprecated_orphan


    async def pii_overview(self, org_id: int | None = None) -> dict[str, Any]:
        """PII 合规资产视图：按敏感级/域/类别聚合 + 风险计数。

        PII 合规增强：新增 ``by_category``（字段类别分布）、``unowned_pii``
        （无主 PII 目录）、``unreviewed_pii``（待复核目录+指标）、
        ``reviewed_pii``（已复核目录）。P1：目录口径按组织隔离。

        Returns:
            ``{by_sensitivity, by_domain, pii_metric_count, pii_catalog_count,
            by_category, unowned_pii, unreviewed_pii, reviewed_pii}``
        """
        # 目录 PII 分布（敏感级含 PII）——P1：按组织隔离
        sens_stmt = (
            select(DBCatalog.sensitivity_level, func.count())
            .where(DBCatalog.deleted_at.is_(None), DBCatalog.sensitivity_level.like("%PII%"))
            .group_by(DBCatalog.sensitivity_level)
        )
        if org_id is not None:
            sens_stmt = sens_stmt.join(
                DataSource, DataSource.source_id == DBCatalog.source_id
            ).where(DataSource.deleted_at.is_(None), DataSource.org_id == org_id)
        sens_rows = (await self._session.execute(sens_stmt)).all()
        pii_catalog_count = sum(int(r[1] or 0) for r in sens_rows)

        # 指标 PII 按域分布
        domain_rows = (
            await self._session.execute(
                select(Metric.domain, func.count())
                .where(Metric.deleted_at.is_(None), Metric.pii_flag.is_(True))
                .group_by(Metric.domain)
            )
        ).all()
        pii_metric_count = sum(int(r[1] or 0) for r in domain_rows)

        # 风险计数：无主 PII / 待复核 PII（目录）/ 已复核 PII（目录）——P1：按组织隔离
        unowned_stmt = (
            select(func.count())
            .select_from(DBCatalog)
            .where(
                DBCatalog.deleted_at.is_(None),
                DBCatalog.owner_id.is_(None),
                DBCatalog.sensitivity_level.like("%PII%"),
            )
        )
        if org_id is not None:
            unowned_stmt = unowned_stmt.join(
                DataSource, DataSource.source_id == DBCatalog.source_id
            ).where(DataSource.deleted_at.is_(None), DataSource.org_id == org_id)
        unowned_pii = (await self._session.execute(unowned_stmt)).scalar() or 0

        unreviewed_stmt = (
            select(func.count())
            .select_from(DBCatalog)
            .where(
                DBCatalog.deleted_at.is_(None),
                DBCatalog.sensitivity_level.like("%PII%"),
                DBCatalog.compliance_reviewed.is_(False),
            )
        )
        if org_id is not None:
            unreviewed_stmt = unreviewed_stmt.join(
                DataSource, DataSource.source_id == DBCatalog.source_id
            ).where(DataSource.deleted_at.is_(None), DataSource.org_id == org_id)
        unreviewed_catalog = (await self._session.execute(unreviewed_stmt)).scalar() or 0
        reviewed_catalog = max(pii_catalog_count - int(unreviewed_catalog), 0)
        # 待复核指标（复用指标合规复核字段）
        unreviewed_metric = (
            await self._session.execute(
                select(func.count())
                .select_from(Metric)
                .where(
                    Metric.deleted_at.is_(None),
                    Metric.pii_flag.is_(True),
                    Metric.compliance_reviewed.is_(False),
                )
            )
        ).scalar() or 0

        # 字段类别分布：展开 classification.pii_columns（PII 目录各字段类别）
        by_category = await self._pii_category_stats(org_id)

        return {
            "by_sensitivity": dict(cast("Sequence[tuple[Any, Any]]", sens_rows)),
            "by_domain": dict(cast("Sequence[tuple[Any, Any]]", domain_rows)),
            "pii_metric_count": int(pii_metric_count),
            "pii_catalog_count": int(pii_catalog_count),
            "by_category": by_category,
            "unowned_pii": int(unowned_pii),
            "unreviewed_pii": int(unreviewed_catalog) + int(unreviewed_metric),
            "unreviewed_catalog": int(unreviewed_catalog),
            "unreviewed_metric": int(unreviewed_metric),
            "reviewed_pii": int(reviewed_catalog),
        }

    async def _pii_category_stats(self, org_id: int | None = None) -> dict[str, int]:
        """字段级 PII 类别分布（展开 classification.pii_columns，含人工标注过滤）。

        返回 ``{类别: 命中字段数}``；classification 缺失或损坏时 best-effort 跳过，
        不阻断 PII 概览。P1：按组织隔离（join data_source 过滤 org）。
        """
        stmt = select(Classification.pii_columns).where(
            Classification.deleted_at.is_(None),
            Classification.pii_columns.isnot(None),
        )
        if org_id is not None:
            stmt = (
                stmt.join(DBCatalog, DBCatalog.id == Classification.catalog_id)
                .join(DataSource, DataSource.source_id == DBCatalog.source_id)
                .where(DataSource.deleted_at.is_(None), DataSource.org_id == org_id)
            )
        rows = (await self._session.execute(stmt)).scalars().all()
        stats: dict[str, int] = {}
        for cols in rows:
            if not isinstance(cols, list):
                continue
            for item in cols:
                if isinstance(item, dict) and item.get("category"):
                    cat = str(item["category"]).upper()
                    stats[cat] = stats.get(cat, 0) + 1
        return stats

    async def _catalog_ids_with_category(
        self, category: str, org_id: int | None = None
    ) -> set[int]:
        """返回含指定 PII 类别的目录 id 集合（类别过滤移到分页前，P1 修复）。

        classification.pii_columns 为 JSON 列，无法直接 SQL 过滤，故展开内存匹配
        得出 id 集合后用于主查询 ``id.in_``（配合 org 过滤保持隔离）。
        """
        stmt = select(Classification.catalog_id, Classification.pii_columns).where(
            Classification.deleted_at.is_(None),
            Classification.pii_columns.isnot(None),
        )
        if org_id is not None:
            stmt = (
                stmt.join(DBCatalog, DBCatalog.id == Classification.catalog_id)
                .join(DataSource, DataSource.source_id == DBCatalog.source_id)
                .where(DataSource.deleted_at.is_(None), DataSource.org_id == org_id)
            )
        rows = (await self._session.execute(stmt)).all()
        cat = category.upper()
        result: set[int] = set()
        for cid, cols in rows:
            if not isinstance(cols, list):
                continue
            for item in cols:
                if isinstance(item, dict) and str(item.get("category") or "").upper() == cat:
                    result.add(int(cid))
                    break
        return result

    async def list_pii_assets(
        self,
        *,
        keyword: str | None = None,
        source_id: str | None = None,
        domain: str | None = None,
        owner_id: int | None = None,
        review_status: str | None = None,
        category: str | None = None,
        page: int = 1,
        page_size: int = 20,
        org_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """PII 资产明细列表（分页 + 多维度筛选），供 PII 合规 Tab 可下钻。

        筛选：关键字 / 数据源 / 业务域（经 data_source 继承）/ 责任人
        （0=无主）/ 复核状态（unreviewed|reviewed）/ PII 类别（经 classification）。

        Returns:
            ``(items, total)``；items 含源名/域/责任人/命中字段数/类别/合规状态。
        """
        base = select(DBCatalog).where(
            DBCatalog.deleted_at.is_(None), DBCatalog.sensitivity_level.like("%PII%")
        )
        count_base = select(func.count()).select_from(DBCatalog).where(
            DBCatalog.deleted_at.is_(None), DBCatalog.sensitivity_level.like("%PII%")
        )
        if source_id:
            base = base.where(DBCatalog.source_id == source_id)
            count_base = count_base.where(DBCatalog.source_id == source_id)
        if owner_id == 0:
            base = base.where(DBCatalog.owner_id.is_(None))
            count_base = count_base.where(DBCatalog.owner_id.is_(None))
        elif owner_id is not None:
            base = base.where(DBCatalog.owner_id == owner_id)
            count_base = count_base.where(DBCatalog.owner_id == owner_id)
        if review_status == "unreviewed":
            base = base.where(DBCatalog.compliance_reviewed.is_(False))
            count_base = count_base.where(DBCatalog.compliance_reviewed.is_(False))
        elif review_status == "reviewed":
            base = base.where(DBCatalog.compliance_reviewed.is_(True))
            count_base = count_base.where(DBCatalog.compliance_reviewed.is_(True))
        if domain or org_id is not None:
            base = base.join(DataSource, DataSource.source_id == DBCatalog.source_id).where(
                DataSource.deleted_at.is_(None),
            )
            count_base = count_base.join(
                DataSource, DataSource.source_id == DBCatalog.source_id
            ).where(DataSource.deleted_at.is_(None))
            if domain:
                base = base.where(DataSource.domain == domain)
                count_base = count_base.where(DataSource.domain == domain)
            # P1 多租户隔离：仅本组织数据源 PII 资产
            if org_id is not None:
                base = base.where(DataSource.org_id == org_id)
                count_base = count_base.where(DataSource.org_id == org_id)
        if keyword:
            escaped = keyword.replace("/", "//").replace("%", "/%").replace("_", "/_")
            base = base.where(
                or_(
                    DBCatalog.entity_name.ilike(f"%{escaped}%", escape="/"),
                    DBCatalog.source_id.ilike(f"%{escaped}%", escape="/"),
                )
            )
            count_base = count_base.where(
                or_(
                    DBCatalog.entity_name.ilike(f"%{escaped}%", escape="/"),
                    DBCatalog.source_id.ilike(f"%{escaped}%", escape="/"),
                )
            )
        # P1 修复：类别过滤移到分页前（此前在分页后内存过滤，total 不含类别条件，
        # 导致翻页漏数据/页内不足）
        if category:
            cat_ids = await self._catalog_ids_with_category(category, org_id)
            base = base.where(DBCatalog.id.in_(cat_ids))
            count_base = count_base.where(DBCatalog.id.in_(cat_ids))
        total = int((await self._session.execute(count_base)).scalar() or 0)
        rows = (
            (
                await self._session.execute(
                    base.order_by(DBCatalog.updated_at.desc())
                    .offset(max(page - 1, 0) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        # P1 优化：批量构造列表项（一次取整页 classification/override，消除逐行 N+1）
        return await self._batch_pii_items(rows), total

    async def _pii_asset_item(self, row: DBCatalog) -> dict[str, Any]:
        """单条 PII 资产列表项：元信息 + 命中字段数/类别 + 合规状态。"""
        base = {
            "id": row.id,
            "entity_name": row.entity_name,
            "entity_type": row.entity_type,
            "source_id": row.source_id,
            "sensitivity_level": row.sensitivity_level,
            "owner_id": row.owner_id,
            "compliance_reviewed": bool(row.compliance_reviewed),
            "masking_policy": row.masking_policy,
            "updated_at": row.updated_at,
            "pii_field_count": 0,
            "categories": [],
        }
        fields = await self._entity_pii_fields(row)
        active = [f for f in fields if not f.get("suppressed")]
        base["pii_field_count"] = len(active)
        base["categories"] = sorted({str(f["category"]) for f in active})
        base["pii_fields"] = fields
        enriched = await self.enrich_catalog_items([base])
        return enriched[0]

    async def _batch_pii_items(self, rows: list[DBCatalog]) -> list[dict[str, Any]]:
        """批量构造 PII 资产列表项（消除逐行 N+1，P1 性能优化）。

        原实现 ``[await _pii_asset_item(r) for r in rows]`` 每行 2 次查询
        （classification + override），20 行/页即 40 次往返。此处一次取出整页
        classification 与 override，内存组装；无明细的旧数据行实时检测补齐
        （CPU，无额外查询）。语义与 ``_pii_asset_item`` 完全一致。
        """
        if not rows:
            return []
        ids = [r.id for r in rows]
        # 1) 批量取 classification（每行取最新一条，对齐单条按 created_at desc）
        class_rows = (
            await self._session.execute(
                select(Classification).where(
                    Classification.catalog_id.in_(ids),
                    Classification.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        class_map: dict[int, Classification] = {}
        for c in sorted(class_rows, key=lambda x: x.created_at or datetime.min):
            class_map[c.catalog_id] = c  # 后者覆盖 = 最新
        # 2) 批量取 override
        ov_rows = (
            await self._session.execute(
                select(PiiFieldOverride).where(
                    PiiFieldOverride.catalog_id.in_(ids),
                    PiiFieldOverride.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        ov_map: dict[int, list[dict[str, Any]]] = {}
        for r in ov_rows:
            ov_map.setdefault(r.catalog_id, []).append(
                {
                    "id": r.id,
                    "column": r.column_name,
                    "suppressed": bool(r.suppressed),
                    "reason": r.reason,
                    "created_by": r.created_by,
                    "created_at": r.created_at,
                }
            )
        from app.services.collector.classifier import SensitivityClassifier

        classifier = SensitivityClassifier()
        items: list[dict[str, Any]] = []
        for row in rows:
            fields: list[dict[str, Any]] = []
            row_data = row.schema_json if isinstance(row.schema_json, dict) else {}
            if row.sensitivity_level and "PII" in row.sensitivity_level.upper():
                classification = class_map.get(row.id)
                if classification is not None and isinstance(
                    classification.pii_columns, list
                ):
                    for item in classification.pii_columns:
                        if isinstance(item, dict) and item.get("column"):
                            fields.append(
                                {
                                    "column": str(item["column"]),
                                    "category": str(item.get("category") or "PII"),
                                    "rule": str(item.get("rule") or ""),
                                    "confidence": float(item.get("confidence") or 0),
                                    "matched_by": str(item.get("matched_by") or "name"),
                                }
                            )
                if not fields:
                    for hit in classifier.detect_pii_fields(row.entity_name, row_data):
                        fields.append(
                            {
                                "column": hit.column,
                                "category": hit.category,
                                "rule": hit.rule,
                                "confidence": hit.confidence,
                                "matched_by": hit.matched_by,
                            }
                        )
            merged = self._merge_pii_fields(fields, ov_map.get(row.id, []))
            active = [f for f in merged if not f.get("suppressed")]
            items.append(
                {
                    "id": row.id,
                    "entity_name": row.entity_name,
                    "entity_type": row.entity_type,
                    "source_id": row.source_id,
                    "sensitivity_level": row.sensitivity_level,
                    "owner_id": row.owner_id,
                    "compliance_reviewed": bool(row.compliance_reviewed),
                    "masking_policy": row.masking_policy,
                    "updated_at": row.updated_at,
                    "pii_field_count": len(active),
                    "categories": sorted({str(f["category"]) for f in active}),
                    "pii_fields": merged,
                }
            )
        return await self.enrich_catalog_items(items)

    # ----------------------------------------------------------------
    # 写能力（PII 合规增强）：表级复核 / 脱敏策略 / 字段误报标注 / 保留期
    # 全部写操作仅由治理角色触发（API 层 RBAC），此处只做数据变更与 flush。
    # ----------------------------------------------------------------

    async def review_catalog(
        self, entity: DBCatalog, decision: str, reviewer_id: int
    ) -> DBCatalog:
        """表级 PII 合规复核（APPROVE 置已复核；REJECT 保持未复核并记录理由）。

        禁自审由 service 层校验（owner 不得复核本人资产）。复核通过后按敏感级
        推导默认脱敏策略（缺省）。
        """
        entity.compliance_reviewed = decision == "APPROVE"
        entity.compliance_reviewed_by = reviewer_id
        entity.compliance_reviewed_at = datetime.now(UTC)
        if entity.masking_policy is None:
            from app.services.governance.policy import masking_for

            entity.masking_policy = masking_for(entity.sensitivity_level or "PII")
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def set_masking_policy(self, entity: DBCatalog, policy: str) -> DBCatalog:
        """设置资产脱敏策略（none/mask/hash/deny，合法值由 schema 校验）。"""
        entity.masking_policy = policy
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def upsert_pii_override(
        self,
        catalog_id: int,
        column: str,
        suppressed: bool,
        reason: str | None,
        actor_id: int,
    ) -> PiiFieldOverride:
        """字段级人工标注 upsert（同列重复标注覆盖，保留最新理由）。"""
        existing = (
            await self._session.execute(
                select(PiiFieldOverride).where(
                    PiiFieldOverride.catalog_id == catalog_id,
                    PiiFieldOverride.column_name == column,
                    PiiFieldOverride.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.suppressed = suppressed
            existing.reason = reason
            existing.created_by = actor_id
            row = existing
        else:
            row = PiiFieldOverride(
                catalog_id=catalog_id,
                column_name=column,
                suppressed=suppressed,
                reason=reason,
                created_by=actor_id,
            )
            self._session.add(row)
        await self._session.flush()
        return row

    async def delete_pii_override(self, catalog_id: int, column: str) -> bool:
        """撤销字段级人工标注（软删），恢复规则引擎判定。"""
        existing = (
            await self._session.execute(
                select(PiiFieldOverride).where(
                    PiiFieldOverride.catalog_id == catalog_id,
                    PiiFieldOverride.column_name == column,
                    PiiFieldOverride.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            return False
        existing.deleted_at = datetime.now(UTC)
        self._session.add(existing)
        await self._session.flush()
        return True

    async def set_retention(
        self, entity: DBCatalog, retention_days: int | None, legal_basis: str | None
    ) -> DBCatalog:
        """设置保留期与合法性基础；到期时间按「最近更新时间 + 保留期」推算。"""
        entity.retention_days = retention_days
        entity.legal_basis = legal_basis
        if retention_days is not None:
            entity.retention_expires_at = (entity.updated_at or datetime.now(UTC)) + timedelta(
                days=retention_days
            )
            entity.retention_notified_at = None
        else:
            entity.retention_expires_at = None
            entity.retention_notified_at = None
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def apply_sensitivity_template(
        self, entity: DBCatalog, template: dict[str, Any]
    ) -> dict[str, Any]:
        """按行业分级模板对单资产升级敏感级（命中模板敏感类别字段 → PII）。

        模板升级基于**实时字段类别检测**（不依赖当前敏感级标记），并合并人工标注
        （标注误报非 PII 的字段不计入升级类别）。

        Args:
            entity: 目录资产。
            template: 模板字典（含 ``sensitive_categories`` 列表）。

        Returns:
            ``{entity_id, entity_name, changed, applied_categories}``。
        """
        sensitive_categories = set(template.get("sensitive_categories") or [])
        from app.services.collector.classifier import SensitivityClassifier

        overrides = await self._entity_pii_overrides(entity.id)
        suppressed_cols = {o["column"] for o in overrides if o["suppressed"]}
        schema = entity.schema_json if isinstance(entity.schema_json, dict) else {}
        hits = SensitivityClassifier().detect_pii_fields(entity.entity_name, schema)
        applied = sorted(
            {str(h.category).upper() for h in hits if h.column not in suppressed_cols}
            & sensitive_categories
        )
        changed = False
        if applied and not (entity.sensitivity_level and "PII" in entity.sensitivity_level.upper()):
            entity.sensitivity_level = "PII"
            self._session.add(entity)
            changed = True
        await self._session.flush()
        return {
            "entity_id": entity.id,
            "entity_name": entity.entity_name,
            "changed": changed,
            "applied_categories": applied,
        }

    async def list_catalog_ids_for_scope(
        self,
        *,
        catalog_ids: list[int] | None,
        source_id: str | None,
        all_pii: bool,
    ) -> list[DBCatalog]:
        """按作用域解析待应用模板的资产列表（模板应用范围收敛）。

        上限与平台批量标准一致（5000）：此前 ``limit(500)`` 会静默截断超量资产，
        返回的 applied 计数误导（P1 修复）。超量场景由调用方（模板应用）以
        ``truncated`` 提示用户分批。
        """
        stmt = select(DBCatalog).where(DBCatalog.deleted_at.is_(None))
        if catalog_ids:
            stmt = stmt.where(DBCatalog.id.in_(catalog_ids))
        elif source_id:
            stmt = stmt.where(DBCatalog.source_id == source_id)
        elif all_pii:
            stmt = stmt.where(DBCatalog.sensitivity_level.like("%PII%"))
        else:
            return []
        rows = (await self._session.execute(stmt.limit(5000))).scalars().all()
        return list(rows)

    async def recent_changes(self, days: int, limit: int) -> dict[str, Any]:
        """变更追踪流：最近 N 天新增/变更的目录与指标。

        富化：目录带 created_at 推断 ``change_type``（created/updated）+ 源/责任人名；
        指标带版本号/描述/状态推断变更类型；接入 ``schema_drift_log`` 变更内容
        （列增删/类型变更 diff）。

        Returns:
            ``{catalogs, metrics, drift, days}``
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        catalogs = await self._recent_catalog_changes(cutoff, limit)
        metrics = await self._recent_metric_changes(cutoff, limit)
        drift = await self._recent_drift(cutoff, limit)
        return {"catalogs": catalogs, "metrics": metrics, "drift": drift, "days": days}

    async def _recent_catalog_changes(
        self, cutoff: datetime, limit: int
    ) -> list[dict[str, Any]]:
        """最近变更的目录资产（created/updated 由 created_at vs updated_at 推断）。"""
        rows = (
            await self._session.execute(
                select(
                    DBCatalog.id,
                    DBCatalog.entity_name,
                    DBCatalog.entity_type,
                    DBCatalog.sensitivity_level,
                    DBCatalog.owner_id,
                    DBCatalog.source_id,
                    DBCatalog.created_at,
                    DBCatalog.updated_at,
                )
                .where(
                    DBCatalog.deleted_at.is_(None),
                    DBCatalog.updated_at >= cutoff,
                )
                .order_by(DBCatalog.updated_at.desc())
                .limit(limit)
            )
        ).all()
        items = [
            {
                "id": r.id,
                "entity_name": r.entity_name,
                "entity_type": r.entity_type,
                "sensitivity_level": r.sensitivity_level,
                "owner_id": r.owner_id,
                "source_id": r.source_id,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
                # 变更类型：创建时间接近更新时间（3s 内）视为新增，否则为更新
                "change_type": (
                    "created"
                    if r.created_at
                    and r.updated_at
                    and abs((r.updated_at - r.created_at).total_seconds()) < 3
                    else "updated"
                ),
            }
            for r in rows
        ]
        return await self.enrich_catalog_items(items)

    async def _recent_metric_changes(
        self, cutoff: datetime, limit: int
    ) -> list[dict[str, Any]]:
        """最近变更的指标（change_type 由状态机推断：废弃/新增/更新）。"""
        rows = (
            await self._session.execute(
                select(
                    Metric.metric_code,
                    Metric.name,
                    Metric.status,
                    Metric.domain,
                    Metric.pii_flag,
                    Metric.version,
                    Metric.description,
                    Metric.owner_id,
                    Metric.updated_at,
                )
                .where(Metric.deleted_at.is_(None), Metric.updated_at >= cutoff)
                .order_by(Metric.updated_at.desc())
                .limit(limit)
            )
        ).all()
        owner_ids = {r.owner_id for r in rows if r.owner_id is not None}
        usr_map: dict[int, str] = {}
        if owner_ids:
            usr_rows = (
                await self._session.execute(
                    select(User.id, User.display_name, User.username).where(
                        User.id.in_(owner_ids)
                    )
                )
            ).all()
            usr_map = {r[0]: (r[1] or r[2]) for r in usr_rows}
        return [
            {
                "metric_code": r.metric_code,
                "name": r.name,
                "status": r.status,
                "domain": r.domain,
                "pii_flag": bool(r.pii_flag),
                "version": r.version,
                "description": r.description,
                "owner_id": r.owner_id,
                "owner_name": usr_map.get(r.owner_id),
                "change_type": (
                    "deprecated"
                    if r.status == "DEPRECATED"
                    else "created"
                    if r.version == 1
                    else "updated"
                ),
                "updated_at": r.updated_at,
            }
            for r in rows
        ]

    async def _recent_drift(self, cutoff: datetime, limit: int) -> list[dict[str, Any]]:
        """最近 schema 漂移记录（列增删/类型变更 diff，TD §12.1 变更审计）。"""
        rows = (
            await self._session.execute(
                select(
                    SchemaDriftLog.id,
                    SchemaDriftLog.source_id,
                    SchemaDriftLog.entity_name,
                    SchemaDriftLog.change_type,
                    SchemaDriftLog.diff_json,
                    SchemaDriftLog.created_at,
                )
                .where(SchemaDriftLog.created_at >= cutoff)
                .order_by(SchemaDriftLog.created_at.desc())
                .limit(limit)
            )
        ).all()
        return [
            {
                "id": r.id,
                "source_id": r.source_id,
                "entity_name": r.entity_name,
                "change_type": r.change_type,
                "diff_json": r.diff_json,
                "created_at": r.created_at,
            }
            for r in rows
        ]

    async def my_assets(self, owner_id: int, limit: int) -> dict[str, Any]:
        """我的资产：当前用户负责的目录与指标（个人工作台视角）。

        Returns:
            ``{owner_id, catalogs, metrics, summary, claimable_orphans}``。
            ``summary`` 含目录/指标/草稿/PII/快照覆盖统计；``claimable_orphans``
            为全局待认领孤儿数（无主资产归属引导）。
        """
        catalogs = await self._my_catalog_items(owner_id, limit)
        metrics = await self._my_metric_items(owner_id, limit)

        draft_count = sum(1 for m in metrics if m["status"] == "DRAFT")
        pii_count = sum(1 for m in metrics if m["pii_flag"])
        snapshot_count = await self._my_snapshot_count(metrics)
        claimable = (
            await self._session.execute(
                select(func.count())
                .select_from(DBCatalog)
                .where(DBCatalog.owner_id.is_(None), DBCatalog.deleted_at.is_(None))
            )
        ).scalar() or 0

        return {
            "owner_id": owner_id,
            "catalogs": catalogs,
            "metrics": metrics,
            "summary": {
                "catalog_count": len(catalogs),
                "metric_count": len(metrics),
                "draft_count": draft_count,
                "pii_count": pii_count,
                "snapshot_covered": snapshot_count,
                "snapshot_total": len(metrics),
            },
            "claimable_orphans": int(claimable),
        }

    async def _my_catalog_items(
        self, owner_id: int, limit: int
    ) -> list[dict[str, Any]]:
        """我的目录资产（含描述/字段数/更新时间 + 源/责任人名）。"""
        rows = (
            await self._session.execute(
                select(
                    DBCatalog.id,
                    DBCatalog.entity_name,
                    DBCatalog.entity_type,
                    DBCatalog.sensitivity_level,
                    DBCatalog.source_id,
                    DBCatalog.owner_id,
                    DBCatalog.description,
                    DBCatalog.schema_json,
                    DBCatalog.updated_at,
                )
                .where(DBCatalog.deleted_at.is_(None), DBCatalog.owner_id == owner_id)
                .limit(limit)
            )
        ).all()
        items = []
        for r in rows:
            schema = r.schema_json if isinstance(r.schema_json, dict) else {}
            fields = schema.get("fields") or schema.get("columns") or []
            items.append(
                {
                    "id": r.id,
                    "entity_name": r.entity_name,
                    "entity_type": r.entity_type,
                    "sensitivity_level": r.sensitivity_level,
                    "source_id": r.source_id,
                    "owner_id": r.owner_id,
                    "description": r.description,
                    "column_count": len(fields) if isinstance(fields, list) else None,
                    "updated_at": r.updated_at,
                }
            )
        return await self.enrich_catalog_items(items)

    async def _my_metric_items(
        self, owner_id: int, limit: int
    ) -> list[dict[str, Any]]:
        """我的指标资产（含治理一等字段 + 描述 + 快照覆盖标记）。"""
        rows = (
            await self._session.execute(
                select(
                    Metric.metric_code,
                    Metric.name,
                    Metric.status,
                    Metric.domain,
                    Metric.pii_flag,
                    Metric.type,
                    Metric.granularity,
                    Metric.unit,
                    Metric.metric_tier,
                    Metric.description,
                    Metric.updated_at,
                )
                .where(Metric.deleted_at.is_(None), Metric.owner_id == owner_id)
                .limit(limit)
            )
        ).all()
        return [
            {
                "metric_code": r.metric_code,
                "name": r.name,
                "status": r.status,
                "domain": r.domain,
                "pii_flag": bool(r.pii_flag),
                "type": r.type,
                "granularity": r.granularity,
                "unit": r.unit,
                "metric_tier": r.metric_tier,
                "description": r.description,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]

    async def _my_snapshot_count(self, metrics: list[dict[str, Any]]) -> int:
        """我的指标中有快照记录的数量（快照覆盖度）。"""
        codes = [m["metric_code"] for m in metrics]
        if not codes:
            return 0
        covered = set(
            (
                await self._session.execute(
                    select(MetricValueSnapshot.metric_code)
                    .where(MetricValueSnapshot.metric_code.in_(codes))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        return len(covered)

    # ----------------------------------------------------------------
    # 写能力（FR-18 资产工作台）：认领/转让归属、敏感级重分类、批量操作
    # 全部写操作仅由 platform_admin/domain_admin 触发（API 层 RBAC），
    # 且落审计（API 层 write_audit），此处只做数据变更与 flush。
    # ----------------------------------------------------------------

    async def get_catalog_entity(self, entity_id: int) -> DBCatalog | None:
        """按 id 获取未删除的目录资产。"""
        return (
            await self._session.execute(
                select(DBCatalog).where(DBCatalog.id == entity_id, DBCatalog.deleted_at.is_(None))
            )
        ).scalar_one_or_none()

    async def catalog_id_by_names(self, names: list[str]) -> dict[str, int]:
        """按 entity_name 批量查未删除的表/视图主键（图谱表节点 entity_id 富集用）。

        返回 ``{entity_name: id}``；不在目录中（未采集/已删除）的名称不出现在结果里。
        """
        if not names:
            return {}
        rows = (
            await self._session.execute(
                select(DBCatalog.id, DBCatalog.entity_name).where(
                    DBCatalog.entity_name.in_(names),
                    DBCatalog.deleted_at.is_(None),
                    DBCatalog.entity_type.in_(["TABLE", "VIEW"]),
                )
            )
        ).all()
        return {row.entity_name: row.id for row in rows}

    async def list_catalog_entities(self, entity_ids: list[int]) -> list[DBCatalog]:
        """按 id 批量获取未删除的目录资产（保持入参顺序，供批量操作）。"""
        if not entity_ids:
            return []
        rows = (
            (
                await self._session.execute(
                    select(DBCatalog).where(
                        DBCatalog.id.in_(entity_ids), DBCatalog.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        order = {eid: idx for idx, eid in enumerate(entity_ids)}
        return sorted(rows, key=lambda r: order.get(r.id, len(order)))

    async def user_exists(self, user_id: int) -> bool:
        """校验用户存在且未删除（owner 指派目标）。"""
        return (
            await self._session.execute(
                select(User.id).where(User.id == user_id, User.deleted_at.is_(None))
            )
        ).first() is not None

    async def assign_owner(self, entity: DBCatalog, owner_id: int | None) -> DBCatalog:
        """认领/转让归属（owner_id=None 表示解除归属回到孤儿池）。

        P1 乐观锁：条件 UPDATE（``WHERE id=? AND row_version=?``），并发用户
        同时认领同一资产时后写方版本不匹配 → 409，避免 last-write-wins 静默覆盖。
        """
        result = await self._session.execute(
            update(DBCatalog)
            .where(DBCatalog.id == entity.id, DBCatalog.row_version == entity.row_version)
            .values(owner_id=owner_id, row_version=DBCatalog.row_version + 1)
        )
        if int(getattr(result, "rowcount", 0) or 0) == 0:
            raise ConflictError(
                "资产归属已被其他用户修改，请刷新后重试",
                error_code="OPTIMISTIC_LOCK_CONFLICT",
                ctx={"entity_id": entity.id},
            )
        entity.owner_id = owner_id
        entity.row_version += 1
        await self._session.flush()
        return entity

    async def reclassify_sensitivity(self, entity: DBCatalog, level: str) -> DBCatalog:
        """重分类敏感级（仅允许枚举值，校验在 service/API 层）。

        P1 乐观锁：同 ``assign_owner``，防并发重分类互相覆盖。
        """
        result = await self._session.execute(
            update(DBCatalog)
            .where(DBCatalog.id == entity.id, DBCatalog.row_version == entity.row_version)
            .values(sensitivity_level=level, row_version=DBCatalog.row_version + 1)
        )
        if int(getattr(result, "rowcount", 0) or 0) == 0:
            raise ConflictError(
                "资产敏感级已被其他用户修改，请刷新后重试",
                error_code="OPTIMISTIC_LOCK_CONFLICT",
                ctx={"entity_id": entity.id},
            )
        entity.sensitivity_level = level
        entity.row_version += 1
        await self._session.flush()
        return entity

    async def batch_assign_owner(self, entities: Sequence[DBCatalog], owner_id: int | None) -> int:
        """批量认领/转让归属，返回受影响数量（逐条乐观锁，单条冲突即中止并抛 409）。"""
        for e in entities:
            result = await self._session.execute(
                update(DBCatalog)
                .where(DBCatalog.id == e.id, DBCatalog.row_version == e.row_version)
                .values(owner_id=owner_id, row_version=DBCatalog.row_version + 1)
            )
            if int(getattr(result, "rowcount", 0) or 0) == 0:
                raise ConflictError(
                    f"资产 {e.entity_name} 已被其他用户修改，请刷新后重试",
                    error_code="OPTIMISTIC_LOCK_CONFLICT",
                    ctx={"entity_id": e.id},
                )
            e.owner_id = owner_id
            e.row_version += 1
        await self._session.flush()
        return len(entities)

    async def batch_reclassify(self, entities: Sequence[DBCatalog], level: str) -> int:
        """批量重分类敏感级，返回受影响数量（逐条乐观锁，单条冲突即中止并抛 409）。"""
        for e in entities:
            result = await self._session.execute(
                update(DBCatalog)
                .where(DBCatalog.id == e.id, DBCatalog.row_version == e.row_version)
                .values(sensitivity_level=level, row_version=DBCatalog.row_version + 1)
            )
            if int(getattr(result, "rowcount", 0) or 0) == 0:
                raise ConflictError(
                    f"资产 {e.entity_name} 敏感级已被其他用户修改，请刷新后重试",
                    error_code="OPTIMISTIC_LOCK_CONFLICT",
                    ctx={"entity_id": e.id},
                )
            e.sensitivity_level = level
            e.row_version += 1
        await self._session.flush()
        return len(entities)
