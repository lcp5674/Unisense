"""血缘 Repository（MySQL 权威存储）。

对齐 DEV_GUIDE §9a：仅承载数据访问，不含业务规则；所有查询软删过滤。
历史快照 / 环检测 / 断链登记 / 指标级边均为数据访问层能力，策略由上层编排。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consume import ApiClient, ApiClientStatus
from app.models.data_source import DataSource, DBCatalog
from app.models.dp_sync import LineageFieldMapping
from app.models.lineage import LineageEdge, LineageEdgeHistory, LineageIngestRun
from app.models.metric import Metric
from app.services.lineage.parser import node_column, node_dimension, node_table
from app.services.system_dict.layers import (
    derive_dw_layer_from_catalog_name,
    load_active_dw_layer_codes,
)


def merge_provenances(existing: str | None, incoming: str) -> str:
    """多来源合并：同一条边被多个采集通道确认时 provenance 保留全部来源。

    以 ``+`` 连接去重（如 ``hive+dp_sql``）——边是结构事实，被任一通道持续
    确认即保持有效；通道失效（mark_missing）需全部来源通道都连续未确认才
    标 stale（P2-7：修复后写通道覆盖前通道 provenance 致治理归属漂移）。
    """
    tokens = []
    for chunk in (existing or "").split("+"):
        chunk = chunk.strip()
        if chunk and chunk not in tokens:
            tokens.append(chunk)
    if incoming and incoming not in tokens:
        tokens.append(incoming)
    return "+".join(tokens)


def _edge_unique_key(
    edge: LineageEdge,
) -> tuple[str, str, str, str]:
    """血缘边唯一键 ``(source, target, edge_type, granularity)``（批量方法分组用）。"""
    return (edge.source_node, edge.target_node, edge.edge_type, edge.granularity)


def provenance_contains(column: Any, source: str) -> Any:
    """SQLAlchemy 条件：``provenance`` 列含 ``source`` token（``+`` 分隔多来源）。

    写路径 provenance 会合并为 ``sqlglot+dp_sql``（见 ``merge_provenances``），
    读路径按通道过滤必须 token 匹配（等值 OR 前缀/中缀/后缀），否则合并边
    在前端按通道过滤时丢失（M4 图谱/边列表与 ``_source_l1_edges`` 口径一致）。
    """
    return or_(
        column == source,
        column.like(f"{source}+%"),
        column.like(f"%+{source}+%"),
        column.like(f"%+{source}"),
    )


def like_literal(value: str) -> str:
    """把字符串转义为 LIKE 模式字面量（``%``/``_``/``\\`` 前缀反斜杠）。

    列前缀反查（如 ``column:{表名}.%``）里表名常含下划线（``wedw_mid.xxx_df``），
    不转义会让 ``_`` 通配任意单字符导致误匹配同前缀表；转义后仅匹配字面。
    与 SQLAlchemy ``Column.like(..., escape="\\\\")`` 配对使用。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


#: 系统内置血缘采集通道：无论当前是否有边都展示，保证「采集通道」视图来源全景完整
#: （SQL 解析=sqlglot / DP 同步=dp_sql，dp_csv 为历史 CSV 导入通道保留展示；
#: 其余动态来源如 metric_definition 有边时自动出现）。
_KNOWN_CHANNELS = ("dp_csv", "dp_sql", "sqlglot")


class LineageRepository:
    """血缘边数据访问。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _upsert(
        self,
        *,
        source_node: str,
        target_node: str,
        edge_type: str,
        granularity: str = "L3",
        confidence: float = 1.0,
        provenance: str = "sqlglot",
        pii_inherited: bool = False,
        change_reason: str = "manual",
        owner: str | None = None,
    ) -> LineageEdge:
        """按唯一键（source/target/edge_type/granularity）幂等写入血缘边。

        既有边值有变化时，先落一条变更前快照（record_edge_history）再覆盖，
        值未变化时不重复写历史（幂等）。
        """
        edge, _ = await self._upsert_with_created(
            source_node=source_node,
            target_node=target_node,
            edge_type=edge_type,
            granularity=granularity,
            confidence=confidence,
            provenance=provenance,
            pii_inherited=pii_inherited,
            change_reason=change_reason,
            owner=owner,
        )
        return edge

    async def _upsert_with_created(
        self,
        *,
        source_node: str,
        target_node: str,
        edge_type: str,
        granularity: str = "L3",
        confidence: float = 1.0,
        provenance: str = "sqlglot",
        pii_inherited: bool = False,
        change_reason: str = "manual",
        owner: str | None = None,
    ) -> tuple[LineageEdge, bool]:
        """幂等写入血缘边，返回 ``(edge, created)``（created 标记本次是否新建）。

        created=True 表示新插入（用于增量采集的 added 计数）；created=False
        表示命中既有边并覆盖值（updated 计数）。既有边值有变化时先落快照。

        软删复活（P0-2）：唯一索引 ``uq_lineage_edge`` 不含 ``deleted_at``，
        软删行残留在索引中；若此处直接 INSERT 新行会撞 MySQL 1062 使整个
        parse_batch/ingest_batch 回滚。因此活跃行不存在时先查软删行，命中则
        **复活**（清 deleted_at/失效标记 + 应用新值 + 落复活快照），这正是
        「确认失效 → 重新采集」应恢复边的语义。
        """
        existing = (
            await self._db.execute(
                select(LineageEdge).where(
                    LineageEdge.source_node == source_node,
                    LineageEdge.target_node == target_node,
                    LineageEdge.edge_type == edge_type,
                    LineageEdge.granularity == granularity,
                    LineageEdge.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            edge, created = await self._revive_or_insert(
                source_node=source_node,
                target_node=target_node,
                edge_type=edge_type,
                granularity=granularity,
                confidence=confidence,
                provenance=provenance,
                pii_inherited=pii_inherited,
                change_reason=change_reason,
                owner=owner,
            )
        else:
            changes: dict[str, object] = {}
            if existing.confidence != confidence:
                changes["confidence"] = confidence
            if existing.provenance != provenance:
                # 多来源合并：不覆盖既有通道来源（P2-7 修复归属漂移）
                merged = merge_provenances(existing.provenance, provenance)
                if merged != existing.provenance:
                    changes["provenance"] = merged
            if existing.pii_inherited != pii_inherited:
                changes["pii_inherited"] = pii_inherited
            if owner is not None and existing.owner != owner:
                changes["owner"] = owner
            if changes:
                await self.record_edge_history(existing, change_reason)
                for column, value in changes.items():
                    setattr(existing, column, value)
            edge = existing
            created = False
        await self._db.flush()
        return edge, created

    async def _revive_or_insert(
        self,
        *,
        source_node: str,
        target_node: str,
        edge_type: str,
        granularity: str,
        confidence: float,
        provenance: str,
        pii_inherited: bool,
        change_reason: str,
        owner: str | None,
    ) -> tuple[LineageEdge, bool]:
        """活跃边不存在时：优先复活软删行，否则新建（P0-2 软删冲突根治）。"""
        tombstone = (
            await self._db.execute(
                select(LineageEdge).where(
                    LineageEdge.source_node == source_node,
                    LineageEdge.target_node == target_node,
                    LineageEdge.edge_type == edge_type,
                    LineageEdge.granularity == granularity,
                    LineageEdge.deleted_at.is_not(None),
                )
            )
        ).scalar_one_or_none()
        if tombstone is not None:
            # 复活：落复活快照，清除软删/失效标记，再应用新值。
            # provenance 须与既有来源合并（M9：原实现直接覆盖会丢 tombstone
            # 中其他通道 token——dp 复活被软删的 sqlglot+dp_sql 共享边后，
            # sqlglot 通道 mark_seen/mark_missing 将匹配不到该边失去保护）。
            await self.record_edge_history(tombstone, f"revive:{change_reason}")
            tombstone.deleted_at = None
            tombstone.stale = False
            tombstone.stale_since = None
            tombstone.missing_count = 0
            tombstone.confidence = confidence
            merged = merge_provenances(tombstone.provenance, provenance)
            if merged != tombstone.provenance:
                tombstone.provenance = merged
            tombstone.pii_inherited = pii_inherited
            if owner is not None:
                tombstone.owner = owner
            return tombstone, False
        edge = LineageEdge(
            source_node=source_node,
            target_node=target_node,
            edge_type=edge_type,
            granularity=granularity,
            confidence=confidence,
            provenance=provenance,
            pii_inherited=pii_inherited,
            owner=owner,
        )
        self._db.add(edge)
        return edge, True

    async def upsert_edge_with_status(
        self,
        *,
        source_node: str,
        target_node: str,
        edge_type: str,
        granularity: str,
        confidence: float = 1.0,
        provenance: str = "sqlglot",
        pii_inherited: bool = False,
        change_reason: str = "ingest",
    ) -> tuple[LineageEdge, bool]:
        """幂等写入血缘边，返回 ``(edge, created)``（增量采集变更计数用）。"""
        return await self._upsert_with_created(
            source_node=source_node,
            target_node=target_node,
            edge_type=edge_type,
            granularity=granularity,
            confidence=confidence,
            provenance=provenance,
            pii_inherited=pii_inherited,
            change_reason=change_reason,
        )

    async def upsert_edges_with_status_batch(
        self, requests: list[dict[str, Any]]
    ) -> dict[tuple[str, str, str, str], tuple[LineageEdge, bool]]:
        """批量幂等写入血缘边（语义同 ``upsert_edge_with_status``）。

        输入 ``requests``：每个元素即 ``upsert_edge_with_status`` 的关键字参数字典。
        返回 ``{(source,target,edge_type,granularity): (edge, created)}``。

        与单条路径的差异只在访问模式：现存活跃/墓碑各**一次批量查询**载入内存
        分类（新建/覆盖/复活），历史快照先 ``add`` 排队、末尾**一次 flush** 批量
        INSERT/UPDATE——避免每条边 2 次 SELECT + 1 次 flush（dp 同步每 step 表边
        逐条写是每轮数万次小查询的来源之一）。适合单批几十条以内；超大请分块。
        """
        if not requests:
            return {}
        # 同唯一键多次出现时以后一次参数为准（解析产物同表对一般已去重，防御）。
        params_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for r in requests:
            key = (
                r["source_node"],
                r["target_node"],
                r["edge_type"],
                r.get("granularity") or "L3",
            )
            params_by_key[key] = r
        keys = list(params_by_key)

        def _conds(ks: list[tuple[str, str, str, str]]) -> Any:
            return or_(
                *[
                    and_(
                        LineageEdge.source_node == s,
                        LineageEdge.target_node == t,
                        LineageEdge.edge_type == et,
                        LineageEdge.granularity == g,
                    )
                    for (s, t, et, g) in ks
                ]
            )

        active = (
            await self._db.execute(
                select(LineageEdge).where(_conds(keys), LineageEdge.deleted_at.is_(None))
            )
        ).scalars().all()
        existing_map = {_edge_unique_key(e): e for e in active}
        miss_keys = [k for k in keys if k not in existing_map]
        tomb_map: dict[tuple[str, str, str, str], LineageEdge] = {}
        if miss_keys:
            tombs = (
                await self._db.execute(
                    select(LineageEdge).where(
                        _conds(miss_keys), LineageEdge.deleted_at.is_not(None)
                    )
                )
            ).scalars().all()
            for e in tombs:
                # 唯一键不含 deleted_at → 同键理论至多一行；防御性保留最早复活候选
                tomb_map.setdefault(_edge_unique_key(e), e)
        result: dict[tuple[str, str, str, str], tuple[LineageEdge, bool]] = {}
        for key in keys:
            r = params_by_key[key]
            source_node = r["source_node"]
            target_node = r["target_node"]
            edge_type = r["edge_type"]
            granularity = r.get("granularity") or "L3"
            confidence = float(r.get("confidence", 1.0))
            provenance = str(r.get("provenance", "sqlglot"))
            pii_inherited = bool(r.get("pii_inherited", False))
            change_reason = str(r.get("change_reason", "ingest"))
            owner = r.get("owner")
            existing = existing_map.get(key)
            if existing is not None:
                changes: dict[str, object] = {}
                if existing.confidence != confidence:
                    changes["confidence"] = confidence
                if existing.provenance != provenance:
                    merged = merge_provenances(existing.provenance, provenance)
                    if merged != existing.provenance:
                        changes["provenance"] = merged
                if existing.pii_inherited != pii_inherited:
                    changes["pii_inherited"] = pii_inherited
                if owner is not None and existing.owner != owner:
                    changes["owner"] = owner
                if changes:
                    self._db.add(
                        LineageEdgeHistory(
                            source_node=existing.source_node,
                            target_node=existing.target_node,
                            edge_type=existing.edge_type,
                            granularity=existing.granularity,
                            confidence=existing.confidence,
                            provenance=existing.provenance,
                            pii_inherited=existing.pii_inherited,
                            change_reason=change_reason,
                        )
                    )
                    for column, value in changes.items():
                        setattr(existing, column, value)
                result[key] = (existing, False)
                continue
            tomb = tomb_map.get(key)
            if tomb is not None:
                # 复活（同单条 _revive_or_insert 墓碑分支）：清软删/失效标记 + 应用新值
                self._db.add(
                    LineageEdgeHistory(
                        source_node=tomb.source_node,
                        target_node=tomb.target_node,
                        edge_type=tomb.edge_type,
                        granularity=tomb.granularity,
                        confidence=tomb.confidence,
                        provenance=tomb.provenance,
                        pii_inherited=tomb.pii_inherited,
                        change_reason=f"revive:{change_reason}",
                    )
                )
                tomb.deleted_at = None
                tomb.stale = False
                tomb.stale_since = None
                tomb.missing_count = 0
                tomb.confidence = confidence
                merged = merge_provenances(tomb.provenance, provenance)
                if merged != tomb.provenance:
                    tomb.provenance = merged
                tomb.pii_inherited = pii_inherited
                if owner is not None:
                    tomb.owner = owner
                result[key] = (tomb, False)
                continue
            edge = LineageEdge(
                source_node=source_node,
                target_node=target_node,
                edge_type=edge_type,
                granularity=granularity,
                confidence=confidence,
                provenance=provenance,
                pii_inherited=pii_inherited,
                owner=owner,
            )
            self._db.add(edge)
            result[key] = (edge, True)
        # 末尾一次 flush：新建 INSERT / 覆盖 UPDATE / 历史快照批量落库（同事务）
        await self._db.flush()
        return result

    async def upsert_edge(
        self,
        *,
        source_node: str,
        target_node: str,
        edge_type: str,
        granularity: str,
        confidence: float = 1.0,
        provenance: str = "sqlglot",
        pii_inherited: bool = False,
        change_reason: str = "reparse",
    ) -> LineageEdge:
        """幂等写入血缘边（按唯一键更新或插入）。"""
        return await self._upsert(
            source_node=source_node,
            target_node=target_node,
            edge_type=edge_type,
            granularity=granularity,
            confidence=confidence,
            provenance=provenance,
            pii_inherited=pii_inherited,
            change_reason=change_reason,
        )

    async def record_edge_history(
        self, edge: LineageEdge, change_reason: str
    ) -> LineageEdgeHistory:
        """把边当前值写入历史快照（供覆盖既有边前调用）。"""
        history = LineageEdgeHistory(
            source_node=edge.source_node,
            target_node=edge.target_node,
            edge_type=edge.edge_type,
            granularity=edge.granularity,
            confidence=edge.confidence,
            provenance=edge.provenance,
            pii_inherited=edge.pii_inherited,
            change_reason=change_reason,
        )
        self._db.add(history)
        await self._db.flush()
        return history

    async def would_create_cycle(self, edge: LineageEdge) -> bool:
        """检测新增 ``edge``（DERIVED_FROM）是否成环：source 已在 target 下游。

        BFS 沿 DERIVED_FROM 边从 target 向下游展开，可达 source 即视为成环；
        visited 集合防止环上的无限遍历。非 DERIVED_FROM 边不参与环检测。
        """
        if edge.edge_type != "DERIVED_FROM":
            return False
        if edge.source_node == edge.target_node:
            return True
        source_node = edge.source_node
        visited: set[str] = {edge.target_node}
        frontier: list[str] = [edge.target_node]
        while frontier:
            current = frontier.pop()
            for e in await self._edges_from(current):
                if e.edge_type != "DERIVED_FROM":
                    continue
                if e.target_node == source_node:
                    return True
                if e.target_node not in visited:
                    visited.add(e.target_node)
                    frontier.append(e.target_node)
        return False

    async def would_create_cycle_many(
        self, probes: list[LineageEdge]
    ) -> set[tuple[str, str]]:
        """批量环检测：返回 ``probes`` 中会成环的 ``(source, target)`` 集合。

        与 ``would_create_cycle`` 同语义（source 已在其 target 的下游闭包即成环），
        但按 target 分组、每层用 ``_edges_from_many`` 一次批量拉取下游边——同一
        target 的多条候选 source 共享一次 BFS，查询次数从「每条边每个节点 1 次
        SELECT」降到「每个 target 每层 1 次」。dp 同步每 step 的表边候选经此
        批量判环，消除逐条 N+1（P2 阶段 2）。

        F2（probe 间成环预检）：闭包传播除已落库边外，**同时沿本批 probes 的
        出边扩展**——若同一批候选含互逆/连环（如 ``(A→B, B→A)`` 且两者当前都
        不在图中），仅按落库图 BFS 会双双放行、批量 upsert 后图产生持久环且无
        自愈（dagre 永久 acyclic 翻转显示）。沿 probe 边传播后，环上每条边的
        target 闭包必含其 source → 全部标记 cyclic 拒绝，绝不落环（与单条顺序
        语义在「环最终不产生」上一致；代价是罕见同批互逆场景可能多拒几条可疑
        边——它们本身即解析异常信号，log 可见）。
        """
        if not probes:
            return set()
        cyclic: set[tuple[str, str]] = set()
        by_target: dict[str, list[str]] = {}
        # probe 出边邻接表：BFS 传播时把「同批将插入的候选边」纳入闭包
        probe_out: dict[str, list[str]] = {}
        for p in probes:
            if p.edge_type != "DERIVED_FROM":
                continue
            if p.source_node == p.target_node:
                cyclic.add((p.source_node, p.target_node))
                continue
            by_target.setdefault(p.target_node, []).append(p.source_node)
            probe_out.setdefault(p.source_node, []).append(p.target_node)
        if not by_target:
            return cyclic
        for target, sources in by_target.items():
            # 单 target 下游闭包：逐层批量拉取（落库边 + probe 出边混合传播）。
            visited: set[str] = {target}
            frontier: list[str] = [target]
            while frontier:
                edges = await self._edges_from_many(frontier)
                next_frontier: list[str] = []
                for e in edges:
                    if e.edge_type != "DERIVED_FROM":
                        continue
                    if e.target_node not in visited:
                        visited.add(e.target_node)
                        next_frontier.append(e.target_node)
                for node in frontier:
                    for tgt in probe_out.get(node, []):
                        if tgt not in visited:
                            visited.add(tgt)
                            next_frontier.append(tgt)
                frontier = next_frontier
            for s in sources:
                if s in visited:
                    cyclic.add((s, target))
        return cyclic

    async def upsert_metric_edge(
        self,
        *,
        from_metric: str,
        to_metric: str,
        edge_type: str = "DERIVED_FROM",
        confidence: float = 1.0,
        provenance: str = "sqlglot",
        change_reason: str = "reparse",
    ) -> LineageEdge:
        """写入指标级血缘边（节点 id 用 ``metric:{code}`` 前缀，粒度 L3）。"""
        return await self._upsert(
            source_node=f"metric:{from_metric}",
            target_node=f"metric:{to_metric}",
            edge_type=edge_type,
            granularity="L3",
            confidence=confidence,
            provenance=provenance,
            change_reason=change_reason,
        )

    async def upsert_metric_table_edge(
        self,
        *,
        metric_code: str,
        table_node: str,
        direction: str = "downstream",
        edge_type: str = "DERIVED_FROM",
        confidence: float = 1.0,
        provenance: str = "metric_definition",
        change_reason: str = "metric_definition",
    ) -> LineageEdge:
        """写入「指标↔表」血缘边（粒度 L3，幂等）。

        direction=downstream 时 ``metric:{code}`` → ``table:{tbl}``（指标产出/物化表）；
        direction=upstream 时 ``table:{tbl}`` → ``metric:{code}``（指标源表）。

        方向约定与 ``scripts.sync_neo4j_assets.parse_metric_edges`` 一致；幂等性由
        ``_upsert`` 的唯一键（source/target/edge_type/granularity）保证，重复注册
        不产生重复边。
        """
        metric_node = f"metric:{metric_code}"
        if direction == "upstream":
            source_node, target_node = table_node, metric_node
        else:
            source_node, target_node = metric_node, table_node
        return await self._upsert(
            source_node=source_node,
            target_node=target_node,
            edge_type=edge_type,
            granularity="L3",
            confidence=confidence,
            provenance=provenance,
            change_reason=change_reason,
        )

    async def upsert_metric_dimension_edge(
        self,
        *,
        metric_code: str,
        dim_node: str,
        edge_type: str = "USES_DIMENSION",
        confidence: float = 1.0,
        provenance: str = "metric_definition",
        change_reason: str = "metric_definition",
    ) -> LineageEdge:
        """写入「指标↔维度」血缘边（粒度 L3，幂等）。

        ``metric:{code}`` → ``dimension:{dim_code}``（USES_DIMENSION），表示该指标
        基于此维度进行分析/下钻。幂等性由 ``_upsert`` 唯一键保证。

        Args:
            metric_code: 指标编码。
            dim_node: 维度节点（``dimension:{code}``，由调用方经 ``node_dimension`` 构造）。
            edge_type: 默认 ``USES_DIMENSION``（保留参数供未来扩展）。
        """
        return await self._upsert(
            source_node=f"metric:{metric_code}",
            target_node=dim_node,
            edge_type=edge_type,
            granularity="L3",
            confidence=confidence,
            provenance=provenance,
            change_reason=change_reason,
        )

    async def sync_metric_dimension_edges(
        self, metric_code: str, current_dim_codes: list[str]
    ) -> tuple[int, int]:
        """差异同步「指标↔维度」血缘边（软删缺失 + 注册新增，返回 (删除数, 新增数)）。

        ``register_metric_dimension_edges`` 是纯追加语义（upsert 不删缺失边），
        指标编辑时维度从多变少（或清空）会导致已解绑维度的 USES_DIMENSION 边残留，
        血缘图仍显示指标在使用已解绑维度。本方法以 ``definition_json.dimensions``
        为唯一事实源：软删不再声明的维度边、注册新增维度边，保证血缘与声明集一致。

        Args:
            metric_code: 指标编码。
            current_dim_codes: 当前声明的维度编码列表（definition_json.dimensions）。

        Returns:
            ``(deleted_count, added_count)``。
        """
        current = {c for c in current_dim_codes if isinstance(c, str) and c}
        node = f"metric:{metric_code}"
        deleted = 0
        # 1) 软删不再声明的维度边
        for edge in await self.edges_for_node(node, direction="downstream"):
            if edge.edge_type != "USES_DIMENSION" or not edge.target_node.startswith("dimension:"):
                continue
            # 仅清理自动注册边（provenance=metric_definition），保留手动/导入边
            if edge.provenance != "metric_definition":
                continue
            dim_code = edge.target_node[len("dimension:") :]
            if dim_code not in current:
                await self.soft_delete_edge_by_key(node, edge.target_node, edge.edge_type)
                deleted += 1
        # 2) 注册新增的维度边（created 标记真正新增，已存在不计）
        added = 0
        for code in current:
            _, created = await self._upsert_with_created(
                source_node=f"metric:{metric_code}",
                target_node=node_dimension(code),
                edge_type="USES_DIMENSION",
                granularity="L3",
                confidence=1.0,
                provenance="metric_definition",
                change_reason="metric_definition",
            )
            if created:
                added += 1
        return deleted, added

    async def sync_metric_column_edges(
        self, metric_code: str, current_fields: list[tuple[str, str]]
    ) -> tuple[int, int]:
        """差异同步「指标↔字段」血缘边（软删缺失 + 注册新增，返回 (删除数, 新增数)）。

        与 ``sync_metric_dimension_edges`` 同款：``register_metric_from_definition``
        的字段边是纯追加语义，指标编辑改度量列/源表后，旧 READS_COLUMN 边会残留，
        血缘图仍显示指标来源于已不使用的字段。本方法以当前声明字段集为唯一事实源：
        软删不再声明的字段边、注册新增字段边。

        Args:
            metric_code: 指标编码。
            current_fields: 当前声明的 ``(source_table, column)`` 列表
                （来自 definition_json.measure_column 与 measures）。

        Returns:
            ``(deleted_count, added_count)``。
        """
        current = {
            (t, c)
            for t, c in current_fields
            if isinstance(t, str) and t and isinstance(c, str) and c
        }
        node = f"metric:{metric_code}"
        deleted = 0
        # 1) 软删不再声明的字段边（column:{table}.{col} → metric:{code}）
        for edge in await self.edges_for_node(node, direction="upstream"):
            if edge.edge_type != "READS_COLUMN" or not edge.source_node.startswith("column:"):
                continue
            # 仅清理自动注册边，保留手动/导入边
            if edge.provenance != "metric_definition":
                continue
            # node_column 构造为 column:{table}.{column}（table 可能含点如 db.table）
            parts = edge.source_node[len("column:") :].rsplit(".", 1)
            if len(parts) != 2 or not parts[0] or not parts[1]:
                continue
            if (parts[0], parts[1]) not in current:
                await self.soft_delete_edge_by_key(edge.source_node, node, edge.edge_type)
                deleted += 1
        # 2) 注册新增的字段边（created 标记真正新增，已存在不计）
        added = 0
        for table, col in current:
            _, created = await self._upsert_with_created(
                source_node=node_column(table, col),
                target_node=node,
                edge_type="READS_COLUMN",
                granularity="L3",
                confidence=1.0,
                provenance="metric_definition",
                change_reason="metric_definition",
            )
            if created:
                added += 1
        return deleted, added

    async def sync_metric_table_edges(
        self,
        metric_code: str,
        downstream_table: str | None,
        upstream_tables: list[str],
        downstream_tables: list[str] | None = None,
    ) -> tuple[int, int]:
        """差异同步「指标↔表」血缘边（软删缺失 + 注册新增，返回 (删除数, 新增数)）。

        与 ``sync_metric_dimension_edges``/``sync_metric_column_edges`` 同款：
        ``register_metric_from_definition`` 的表边是纯追加语义，指标编辑改
        ``source_table``（落地表）/``source_tables``（源表集）/``downstream_tables``
        （下游使用表）后，旧表边会残留，血缘图仍显示指标产出/来源于已不使用的表。
        以当前声明为唯一事实源：软删不再声明的表边、注册新增表边。

        下游方向（``metric:{code}`` → ``table:{tbl}``）同时容纳落地表
        （``definition_json.source_table``，指标物化所在）与下游使用表
        （``definition_json.downstream_tables``，消费该指标的表）——二者同向，
        合并进 ``current_down`` 参与差异同步，语义区别由口径 JSON 承载。

        Args:
            metric_code: 指标编码。
            downstream_table: 当前落地表（``definition_json.source_table``，可为 None）。
            upstream_tables: 当前源表列表（``definition_json.source_tables``）。
            downstream_tables: 当前下游使用表列表（``definition_json.downstream_tables``，
                消费该指标的表；缺省空列表）。

        Returns:
            ``(deleted_count, added_count)``。
        """
        node = f"metric:{metric_code}"
        current_down = {node_table(downstream_table)} if downstream_table else set()
        current_down.update(
            node_table(t) for t in (downstream_tables or []) if isinstance(t, str) and t
        )
        current_up = {node_table(t) for t in upstream_tables if isinstance(t, str) and t}
        deleted = 0
        # 1a) 软删不再声明的下游表边（metric:{code} → table:{tbl}，DERIVED_FROM）
        #     —— 含落地表（物化）与下游使用表（消费方），同向合并进 current_down
        for edge in await self.edges_for_node(node, direction="downstream"):
            if edge.edge_type != "DERIVED_FROM" or not edge.target_node.startswith("table:"):
                continue  # 仅处理 table 节点，跳过指标依赖边（metric:*）
            # 仅清理自动注册边，保留手动/导入边
            if edge.provenance != "metric_definition":
                continue
            if edge.target_node not in current_down:
                await self.soft_delete_edge_by_key(node, edge.target_node, edge.edge_type)
                deleted += 1
        # 1b) 软删不再声明的源表边（table:{tbl} → metric:{code}，DERIVED_FROM）
        for edge in await self.edges_for_node(node, direction="upstream"):
            if edge.edge_type != "DERIVED_FROM" or not edge.source_node.startswith("table:"):
                continue
            if edge.provenance != "metric_definition":
                continue
            if edge.source_node not in current_up:
                await self.soft_delete_edge_by_key(edge.source_node, node, edge.edge_type)
                deleted += 1
        # 2) 注册新增的表边（created 标记真正新增，已存在不计）
        added = 0
        for tn in current_down:
            _, created = await self._upsert_with_created(
                source_node=node,
                target_node=tn,
                edge_type="DERIVED_FROM",
                granularity="L3",
                confidence=1.0,
                provenance="metric_definition",
                change_reason="metric_definition",
            )
            if created:
                added += 1
        for sn in current_up:
            _, created = await self._upsert_with_created(
                source_node=sn,
                target_node=node,
                edge_type="DERIVED_FROM",
                granularity="L3",
                confidence=1.0,
                provenance="metric_definition",
                change_reason="metric_definition",
            )
            if created:
                added += 1
        return deleted, added

    async def upsert_metric_column_edge(
        self,
        *,
        metric_code: str,
        column_node: str,
        edge_type: str = "READS_COLUMN",
        confidence: float = 1.0,
        provenance: str = "metric_definition",
        change_reason: str = "metric_definition",
    ) -> LineageEdge:
        """写入「指标↔字段」血缘边（粒度 L3，幂等）。

        ``column:{db}.{tbl}.{col}`` → ``metric:{code}``（READS_COLUMN），表示该指标
        来源于表的某个具体字段（度量列/维度列）。幂等性由 ``_upsert`` 唯一键保证。

        Args:
            metric_code: 指标编码。
            column_node: 字段节点（``column:{tbl}.{col}``，由调用方经 ``node_column`` 构造）。
        """
        return await self._upsert(
            source_node=column_node,
            target_node=f"metric:{metric_code}",
            edge_type=edge_type,
            granularity="L3",
            confidence=confidence,
            provenance=provenance,
            change_reason=change_reason,
        )

    async def register_break(
        self,
        *,
        node: str,
        external_system: str,
        owner: str,
        direction: str = "downstream",
    ) -> LineageEdge:
        """登记断链：写入 EXTERNAL_BREAK 边，另一侧为 ``external:{system}`` 占位节点。

        direction=downstream 时 node 为上游（node -> external），
        direction=upstream 时 node 为下游（external -> node）；幂等。
        """
        external_node = f"external:{external_system}"
        if direction == "upstream":
            source_node, target_node = external_node, node
        else:
            source_node, target_node = node, external_node
        return await self._upsert(
            source_node=source_node,
            target_node=target_node,
            edge_type="EXTERNAL_BREAK",
            granularity="L1",
            confidence=1.0,
            provenance="manual",
            change_reason="manual",
            owner=owner,
        )

    async def _edges_from(self, node: str) -> list[LineageEdge]:
        # P1-2：stale 失效边不再参与影响分析（与模型注释「stale 不再参与影响分析」对齐）
        return list(
            (
                await self._db.execute(
                    select(LineageEdge).where(
                        LineageEdge.source_node == node,
                        LineageEdge.deleted_at.is_(None),
                        LineageEdge.stale.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _edges_from_many(self, nodes: list[str]) -> list[LineageEdge]:
        """批量取多个 source_node 的下游边（P2：分层 BFS 用，一次查询替代 N 次）。"""
        if not nodes:
            return []
        return list(
            (
                await self._db.execute(
                    select(LineageEdge).where(
                        LineageEdge.source_node.in_(nodes),
                        LineageEdge.deleted_at.is_(None),
                        LineageEdge.stale.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _edges_to(self, node: str) -> list[LineageEdge]:
        # P1-2：stale 失效边不再参与影响分析
        return list(
            (
                await self._db.execute(
                    select(LineageEdge).where(
                        LineageEdge.target_node == node,
                        LineageEdge.deleted_at.is_(None),
                        LineageEdge.stale.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def edges_for_node(self, node: str, direction: str = "both") -> list[LineageEdge]:
        """返回与节点直接相连（一跳）的未删除血缘边。

        Args:
            node: 节点 id（如 ``metric:gmv_total`` / ``table:dws_metric_gmv``）。
            direction: ``upstream`` 仅入边（target==node）、``downstream`` 仅出边
                （source==node）、``both`` 双向。
        """
        if direction == "upstream":
            stmt = select(LineageEdge).where(
                LineageEdge.target_node == node, LineageEdge.deleted_at.is_(None)
            )
        elif direction == "downstream":
            stmt = select(LineageEdge).where(
                LineageEdge.source_node == node, LineageEdge.deleted_at.is_(None)
            )
        else:
            stmt = select(LineageEdge).where(
                (LineageEdge.source_node == node) | (LineageEdge.target_node == node),
                LineageEdge.deleted_at.is_(None),
            )
        return list((await self._db.execute(stmt)).scalars().all())

    async def query_impact(
        self, node: str, direction: str = "downstream", max_hops: int = 5, max_edges: int = 5000
    ) -> list[LineageEdge]:
        """基于 BFS 的影响分析（按跳数展开，最多 max_hops 跳，结果上限 max_edges）。"""
        visited: set[str] = {node}
        frontier: list[str] = [node]
        result: list[LineageEdge] = []
        seen_edges: set[int] = set()
        hops = 0
        while frontier and hops < max_hops:
            next_frontier: list[str] = []
            for n in frontier:
                if direction in ("downstream", "both"):
                    for e in await self._edges_from(n):
                        if e.id not in seen_edges:
                            seen_edges.add(e.id)
                            result.append(e)
                        if e.target_node not in visited:
                            visited.add(e.target_node)
                            next_frontier.append(e.target_node)
                if direction in ("upstream", "both"):
                    for e in await self._edges_to(n):
                        if e.id not in seen_edges:
                            seen_edges.add(e.id)
                            result.append(e)
                        if e.source_node not in visited:
                            visited.add(e.source_node)
                            next_frontier.append(e.source_node)
            frontier = next_frontier
            hops += 1
            if len(result) >= max_edges:
                break
        return result

    async def soft_delete_by_node(self, node: str) -> int:
        """级联软删某节点相关的全部血缘边（影响分析失效时维护一致性）。

        置 ``deleted_at`` 而非物理删除：血缘边是审计/溯源对象，物理删除会连带
        丢失 ``lineage_edge_history`` 的关联上下文，且与全仓软删约定（所有查询
        以 ``deleted_at IS NULL`` 过滤）不一致。
        """
        stmt = (
            update(LineageEdge)
            .where(
                (LineageEdge.source_node == node) | (LineageEdge.target_node == node),
                LineageEdge.deleted_at.is_(None),
            )
            .values(deleted_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        result = await self._db.execute(stmt)
        await self._db.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    async def restore_by_node(self, node: str) -> int:
        """级联恢复某节点相关的全部软删血缘边（指标回收站恢复时对称重建）。

        与 ``soft_delete_by_node`` 对称：清除 ``deleted_at`` 使已失效边重新参与
        影响分析，避免恢复的指标血缘为空直到下次编辑/发布（TD §12 血缘一致性）。
        """
        stmt = (
            update(LineageEdge)
            .where(
                (LineageEdge.source_node == node) | (LineageEdge.target_node == node),
                LineageEdge.deleted_at.is_not(None),
            )
            .values(deleted_at=None)
            .execution_options(synchronize_session=False)
        )
        result = await self._db.execute(stmt)
        await self._db.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    async def soft_deleted_edges_for_node(self, node: str) -> list[LineageEdge]:
        """取某节点相关的全部软删血缘边（恢复节点时重建图存储用）。

        与 ``edges_for_node`` 对称，但过滤 ``deleted_at IS NOT NULL``——恢复路径
        需要知道哪些边将重新激活，才能把图存储（Neo4j）中的对应边重建回来。
        """
        stmt = select(LineageEdge).where(
            (LineageEdge.source_node == node) | (LineageEdge.target_node == node),
            LineageEdge.deleted_at.is_not(None),
        )
        return list((await self._db.execute(stmt)).scalars().all())

    # ---- 增量采集与失效管理（TD §12.2 血缘采集通道）----

    async def _source_l1_edges(self, source: str) -> list[LineageEdge]:
        """取某来源通道全部未删除的表级（L1/DERIVED_FROM）血缘边。

        provenance 现可含多来源（``hive+dp_sql``，P2-7 合并语义）——匹配「该
        source 是否在来源集合中」（``provenance_contains`` token 匹配）。
        """
        return list(
            (
                await self._db.execute(
                    select(LineageEdge).where(
                        provenance_contains(LineageEdge.provenance, source),
                        LineageEdge.edge_type == "DERIVED_FROM",
                        LineageEdge.granularity == "L1",
                        LineageEdge.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def mark_seen(self, source: str, seen_pairs: set[tuple[str, str]]) -> tuple[int, int]:
        """把本次采集确认存在的边标记为「已见」。

        对 ``(source_node, target_node)`` 命中 ``seen_pairs`` 的边刷新
        ``last_seen_at`` 并清零 ``missing_count``；此前处于失效队列（stale=True）
        的边一并恢复（stale=False、stale_since=None）。

        Args:
            source: 来源通道标识（如 ``dp_csv``）。
            seen_pairs: 本次采集确认存在的 ``(source_node, target_node)`` 集合。

        Returns:
            ``(confirmed_count, restored_count)``：确认边数、恢复的失效边数。
        """
        rows = await self._source_l1_edges(source)
        confirmed = 0
        restored = 0
        now = datetime.now(UTC)
        for edge in rows:
            if (edge.source_node, edge.target_node) not in seen_pairs:
                continue
            edge.last_seen_at = now
            if edge.missing_count != 0:
                edge.missing_count = 0
            if edge.stale:
                edge.stale = False
                edge.stale_since = None
                restored += 1
            confirmed += 1
        if confirmed:
            await self._db.flush()
        return confirmed, restored

    async def touch_edges_seen(self, pairs: set[tuple[str, str]]) -> int:
        """把扫描轮外写库的边直接置为「已见」（last_seen_at=now 并清观察计数）。

        N4：resolve_ticket / reprocess / retry_llm_tickets 等**扫描轮外**的写边
        入口没有扫描轮的 seen_pairs——若不刷新这些边的 last_seen_at，mark_missing
        会因 ``last_seen_at IS NULL`` 跳过它们（不进入失效观察），任务/节点删除后
        这些边永不 stale（删除语义不闭合，与 D6 记忆复用重新确认的设计不一致）。
        调用方（dp_sync_service resolve 区）写边后传入本次实际写入的边对集合，
        此处批量 UPDATE 置 seen——使它们进入后续全量轮失效观察闭环（任务仍存在且
        SQL 未变时由记忆复用/重扫重新确认，不会被误删；任务删除后正常 stale）。

        返回命中行数（仅统计活跃边；分块避免 IN 过长）。
        """
        if not pairs:
            return 0
        now = datetime.now(UTC)
        total = 0
        items = sorted(pairs)
        for i in range(0, len(items), 400):
            chunk = items[i : i + 400]
            cond = or_(
                *[
                    and_(
                        LineageEdge.source_node == s,
                        LineageEdge.target_node == t,
                    )
                    for (s, t) in chunk
                ]
            )
            result = await self._db.execute(
                update(LineageEdge)
                .where(cond, LineageEdge.deleted_at.is_(None))
                .values(
                    last_seen_at=now,
                    missing_count=0,
                    stale=False,
                    stale_since=None,
                )
            )
            total += int(getattr(result, "rowcount", 0) or 0)
        return total

    async def mark_missing(
        self,
        source: str,
        seen_pairs: set[tuple[str, str]],
        threshold: int,
    ) -> tuple[int, int]:
        """增量采集的失效检测：对未再出现的边累加观察期计数。

        仅处理此前至少被确认过一次（``last_seen_at`` 非空）的边；本次仍在
        ``seen_pairs`` 中的边跳过。``missing_count`` 达到 ``threshold`` 时标记
        进入失效队列（stale=True、stale_since=now），避免单次未采到误删真实血缘。

        Args:
            source: 来源通道标识。
            seen_pairs: 本次采集确认存在的 ``(source_node, target_node)`` 集合。
            threshold: 连续未出现轮次阈值（观察期）。

        Returns:
            ``(missing_count, stale_flagged_count)``：未再出现边数、新失效边数。
        """
        rows = await self._source_l1_edges(source)
        missing = 0
        stale_flagged = 0
        now = datetime.now(UTC)
        for edge in rows:
            if edge.last_seen_at is None:
                continue
            if (edge.source_node, edge.target_node) in seen_pairs:
                continue
            edge.missing_count += 1
            missing += 1
            if edge.missing_count >= threshold and not edge.stale:
                edge.stale = True
                edge.stale_since = now
                stale_flagged += 1
        if missing:
            await self._db.flush()
        return missing, stale_flagged

    async def begin_ingest_run(self, source: str) -> LineageIngestRun:
        """开始一次增量采集运行（写入 running 状态记录）。"""
        run = LineageIngestRun(source=source, status="running")
        self._db.add(run)
        await self._db.flush()
        return run

    async def finish_ingest_run(
        self,
        run: LineageIngestRun,
        *,
        status: str,
        total_edges: int = 0,
        added: int = 0,
        updated: int = 0,
        missing: int = 0,
        stale_flagged: int = 0,
        restored: int = 0,
        skipped: int = 0,
        error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """结束增量采集运行，回写变更摘要与状态。

        Args:
            detail: 本次运行详情快照（dict 序列化为 ``detail_json`` 文本列）。
                SQL 解析存 SQL 原文/方言/落点/边明细；批量采集存变更边明细。
            skipped: 因循环依赖被跳过的边数（批次解析等场景），并入详情快照。
        """
        run.status = status
        run.total_edges = total_edges
        run.added_count = added
        run.updated_count = updated
        run.missing_count = missing
        run.stale_flagged_count = stale_flagged
        run.restored_count = restored
        run.error = error
        payload = dict(detail or {})
        if skipped:
            payload["skipped"] = skipped
        run.detail_json = json.dumps(payload, ensure_ascii=False) if payload else None
        await self._db.flush()

    async def get_ingest_run(self, run_id: int) -> LineageIngestRun | None:
        """按主键取采集运行记录（含 ``detail_json``，由上层解析展示）。"""
        return (
            await self._db.execute(select(LineageIngestRun).where(LineageIngestRun.id == run_id))
        ).scalar_one_or_none()

    async def latest_ingest_run(self, source: str) -> LineageIngestRun | None:
        """取某来源通道最近一次运行记录（无记录返回 None）。"""
        return (
            await self._db.execute(
                select(LineageIngestRun)
                .where(LineageIngestRun.source == source)
                .order_by(LineageIngestRun.run_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def list_ingest_runs(self, source: str, limit: int = 20) -> list[LineageIngestRun]:
        """取某来源通道最近的运行历史（按时间倒序）。"""
        return list(
            (
                await self._db.execute(
                    select(LineageIngestRun)
                    .where(LineageIngestRun.source == source)
                    .order_by(LineageIngestRun.run_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def list_channels(self) -> list[dict[str, Any]]:
        """血缘采集通道总览：按来源聚合边数/节点数/失效边数/最近运行。

        Returns:
            ``[{source, edge_count, node_count, stale_count, last_run}]``。
        """
        # 边数 + 失效边数按来源聚合
        edge_rows = (
            await self._db.execute(
                select(
                    LineageEdge.provenance,
                    func.count(LineageEdge.id),
                    func.sum(case((LineageEdge.stale.is_(True), 1), else_=0)),
                )
                .where(LineageEdge.deleted_at.is_(None))
                .group_by(LineageEdge.provenance)
            )
        ).all()
        # 节点数（源节点 ∪ 目标节点 去重）按来源聚合
        src_q = select(LineageEdge.provenance.label("p"), LineageEdge.source_node.label("n")).where(
            LineageEdge.deleted_at.is_(None)
        )
        tgt_q = select(LineageEdge.provenance.label("p"), LineageEdge.target_node.label("n")).where(
            LineageEdge.deleted_at.is_(None)
        )
        union = src_q.union(tgt_q).subquery()
        node_rows = (
            await self._db.execute(
                select(union.c.p, func.count(func.distinct(union.c.n))).group_by(union.c.p)
            )
        ).all()
        node_counts = {str(p): int(c or 0) for p, c in node_rows}

        channels: list[dict[str, Any]] = []
        for provenance, edge_count, stale_count in edge_rows:
            source = str(provenance)
            channels.append(
                {
                    "source": source,
                    "edge_count": int(edge_count or 0),
                    "node_count": int(node_counts.get(source, 0)),
                    "stale_count": int(stale_count or 0),
                    "last_run": await self.latest_ingest_run(source),
                }
            )
        # 补全内置已知通道（当前 0 边也展示，如 SQL 解析尚未产生血缘时通道仍可见）
        existing = {c["source"] for c in channels}
        for source in _KNOWN_CHANNELS:
            if source not in existing:
                channels.append(
                    {
                        "source": source,
                        "edge_count": 0,
                        "node_count": 0,
                        "stale_count": 0,
                        "last_run": await self.latest_ingest_run(source),
                    }
                )
        channels.sort(key=lambda c: c["edge_count"], reverse=True)
        return channels

    async def list_stale_edges(
        self, source: str | None = None, limit: int = 200
    ) -> list[LineageEdge]:
        """失效队列：stale=True 且未删除的边（按进入失效时间倒序）。"""
        stmt = (
            select(LineageEdge)
            .where(LineageEdge.stale.is_(True), LineageEdge.deleted_at.is_(None))
            .order_by(LineageEdge.stale_since.desc())
            .limit(limit)
        )
        if source:
            stmt = stmt.where(provenance_contains(LineageEdge.provenance, source))
        return list((await self._db.execute(stmt)).scalars().all())

    async def get_edge(self, edge_id: int) -> LineageEdge | None:
        """按主键取未删除的血缘边。"""
        return (
            await self._db.execute(
                select(LineageEdge).where(
                    LineageEdge.id == edge_id, LineageEdge.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()

    async def soft_delete_edge(self, edge_id: int) -> LineageEdge | None:
        """按主键软删单条血缘边（置 deleted_at），返回被删边；不存在返回 None。

        区别于 ``soft_delete_by_node``（级联删整节点），用于人工治理的单边删除
        （误登记/断链修复），保留 ``lineage_edge_history`` 审计上下文。
        """
        edge = await self.get_edge(edge_id)
        if edge is None:
            return None
        edge.deleted_at = datetime.now(UTC)
        await self._db.flush()
        return edge

    async def soft_delete_edge_by_key(
        self, source_node: str, target_node: str, edge_type: str
    ) -> LineageEdge | None:
        """按 (source, target, edge_type) 软删单条血缘边（跨服务关系撤销联动用）。

        解绑指标-维度等"明确撤销关系"动作时调用，即时移除陈旧血缘边，
        避免 register 的追加语义在关系解除后残留 USES_DIMENSION 等边。
        """
        stmt = select(LineageEdge).where(
            LineageEdge.source_node == source_node,
            LineageEdge.target_node == target_node,
            LineageEdge.edge_type == edge_type,
            LineageEdge.deleted_at.is_(None),
        )
        edge = (await self._db.execute(stmt)).scalar_one_or_none()
        if edge is None:
            return None
        edge.deleted_at = datetime.now(UTC)
        await self._db.flush()
        return edge

    async def confirm_stale(self, edge: LineageEdge) -> None:
        """确认失效边：软删（置 deleted_at），不再参与血缘查询。"""
        edge.deleted_at = datetime.now(UTC)
        await self._db.flush()

    async def restore_stale(self, edge: LineageEdge) -> None:
        """恢复失效边：清除失效标记与观察期计数，重新参与血缘查询。"""
        edge.stale = False
        edge.stale_since = None
        edge.missing_count = 0
        await self._db.flush()

    async def invalidate_dropped_table(self, table_node: str) -> int:
        """表删除（``DROP TABLE``）依赖失效：软删除以该表为源或目标的血缘边。

        表实体已不存在，其上下游血缘边均失去意义（依赖失效）；软删除保留审计
        痕迹与历史快照，可追踪「该表被 DROP 后哪些边随之失效」。

        Args:
            table_node: 表节点标识（``table:db.name``）。

        Returns:
            失效（软删除）的边数。
        """
        result = await self._db.execute(
            update(LineageEdge)
            .where(
                or_(
                    LineageEdge.source_node == table_node,
                    LineageEdge.target_node == table_node,
                ),
                LineageEdge.deleted_at.is_(None),
            )
            .values(deleted_at=datetime.now(UTC))
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def list_nodes(self, kw: str | None = None, limit: int = 50) -> list[tuple[str, int]]:
        """血缘候选节点：聚合血缘边（lineage_edge）两端节点。

        无 ``kw`` 时只返回表/指标级节点并按参与边数倒序返回 top-N（预加载常用节点，
        不掺入字段映射——避免 3.6 万行列映射把表级节点挤出选项框）；带 ``kw`` 时
        额外聚合字段映射（lineage_field_mapping）为 ``field:`` 节点并按节点 id 模糊
        过滤——用户输入裸列名/表.列 可搜出字段节点作为字段级血缘起点。

        Returns:
            ``[(node, count)]``，count 为该节点参与的血缘边数。
        """
        src_q = select(LineageEdge.source_node.label("node")).where(
            LineageEdge.deleted_at.is_(None)
        )
        tgt_q = select(LineageEdge.target_node.label("node")).where(
            LineageEdge.deleted_at.is_(None)
        )
        # 无关键词预加载 = 只返回表/指标级节点（选项框保持常用表/指标）。
        # 字段级候选（方案 B）仅在有搜索词时聚合 lineage_field_mapping 的列映射为 field: 节点——
        # 用户输入列名（如 real_amount）或 表.列 即可搜出字段节点作为字段级血缘起点；
        # 不参与无关键词 top-N 预加载，避免 3.6 万行字段映射把表级节点挤出选项框。
        # 仅聚合有效列映射（source_column 非空，排除表级降级/表达式占位）。
        if kw:
            fld_src_q = select(
                func.concat(
                    "field:",
                    LineageFieldMapping.source_table,
                    ".",
                    LineageFieldMapping.source_column,
                ).label("node")
            ).where(
                LineageFieldMapping.deleted_at.is_(None),
                LineageFieldMapping.source_column.is_not(None),
            )
            fld_tgt_q = select(
                func.concat(
                    "field:",
                    LineageFieldMapping.target_table,
                    ".",
                    LineageFieldMapping.target_column,
                ).label("node")
            ).where(LineageFieldMapping.deleted_at.is_(None))
            union = src_q.union(tgt_q, fld_src_q, fld_tgt_q).subquery()
        else:
            union = src_q.union(tgt_q).subquery()
        stmt = (
            select(union.c.node, func.count().label("cnt"))
            .group_by(union.c.node)
            .order_by(func.count().desc())
        )
        if kw:
            # 通配符转义（对齐 FR-035）：kw 含 %/_ 时须转义防模糊放大
            escaped = kw.replace("/", "//").replace("%", "/%").replace("_", "/_")
            stmt = stmt.where(union.c.node.like(f"%{escaped}%", escape="/"))
        rows = (await self._db.execute(stmt.limit(limit))).all()
        return [(str(r[0]), int(r[1] or 0)) for r in rows]

    async def field_mappings_hop(
        self,
        *,
        direction: str,
        tables: set[str] | None = None,
        fields: set[tuple[str, str]] | None = None,
        limit: int = 500,
    ) -> list[LineageFieldMapping]:
        """字段级血缘单跳展开（方案 B）：沿 ``lineage_field_mapping`` 查与起点相邻的列映射行。

        Args:
            direction: ``upstream`` = 起点作目标，返回这些映射行（其 source 是上游来源列）；
                ``downstream`` = 起点作源，返回这些映射行（其 target 是下游去向列）。
            tables: 表级起点集合（``db.tbl``，匹配该表作为 目标/源 的全部有效列映射）。
            fields: 字段级起点集合（``(table, column)`` 精确对，匹配该字段作为 目标/源 的映射）。
            limit: 单跳返回上限（防大扇出爆炸；BFS 由 service 层按跳数收敛）。

        Returns:
            按 id 升序的映射行列表（软删过滤、有效列映射 source_column 非空）。
        """
        conds: list[Any] = [
            LineageFieldMapping.deleted_at.is_(None),
            LineageFieldMapping.source_column.is_not(None),
        ]
        if direction == "upstream":
            # 起点作为「目标」→ 这些行的 source 列是它的上游来源
            if tables:
                conds.append(LineageFieldMapping.target_table.in_(tables))
            if fields:
                conds.append(
                    or_(
                        *[
                            and_(
                                LineageFieldMapping.target_table == t,
                                LineageFieldMapping.target_column == c,
                            )
                            for t, c in fields
                        ]
                    )
                )
        else:
            # 起点作为「源」→ 这些行的 target 列是它的下游去向
            if tables:
                conds.append(LineageFieldMapping.source_table.in_(tables))
            if fields:
                conds.append(
                    or_(
                        *[
                            and_(
                                LineageFieldMapping.source_table == t,
                                LineageFieldMapping.source_column == c,
                            )
                            for t, c in fields
                        ]
                    )
                )
        stmt = (
            select(LineageFieldMapping)
            .where(*conds)
            .order_by(LineageFieldMapping.id)
            .limit(limit)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def reverse_metric_edges(
        self,
        *,
        table_names: set[str] | None = None,
        column_ids: set[str] | None = None,
        dimension_ids: set[str] | None = None,
    ) -> list[LineageEdge]:
        """反查「直接依赖给定节点」的指标血缘边（what-if 预览的受影响指标反查）。

        覆盖三种"改动 → 指标受伤"的直连形态（软删过滤，去重后按 id 升序）：
        - ``DERIVED_FROM`` ``table:{T} → metric:{M}``：指标以 T 为源表（T 可带库名）；
        - ``READS_COLUMN`` ``column:{T}.{c} → metric:{M}``：指标读 T 的列——
          ``table_names`` 命中列前缀（``column:{T}.%``，LIKE 通配符转义防误配）；
          ``column_ids`` 命中完整列节点（字段级起点精确到列）；
        - ``USES_DIMENSION`` ``metric:{M} → dimension:{D}``：指标使用维度 D
          （``dimension_ids`` 命中 target）。

        Args:
            table_names: 表名集合（可含 ``db.`` 前缀；服务层负责补裸表名候选）。
            column_ids: 完整列节点集合（``column:{db}.{tbl}.{col}``，精确匹配 source）。
            dimension_ids: 维度节点集合（``dimension:{code}``，匹配 USES_DIMENSION 的 target）。

        Returns:
            命中的 ``LineageEdge`` 列表（含目标指标在 target_node）。
        """
        conds: list[Any] = [LineageEdge.deleted_at.is_(None)]
        or_parts: list[Any] = []
        if table_names:
            names = {t for t in table_names if t}
            if names:
                or_parts.append(
                    and_(
                        LineageEdge.edge_type == "DERIVED_FROM",
                        LineageEdge.source_node.in_({f"table:{t}" for t in names}),
                        LineageEdge.target_node.like("metric:%"),
                    )
                )
                or_parts.append(
                    and_(
                        LineageEdge.edge_type == "READS_COLUMN",
                        or_(
                            *[
                                LineageEdge.source_node.like(
                                    f"column:{like_literal(t)}.%", escape="\\"
                                )
                                for t in names
                            ]
                        ),
                        LineageEdge.target_node.like("metric:%"),
                    )
                )
        if column_ids:
            cols = {c for c in column_ids if c}
            if cols:
                or_parts.append(
                    and_(
                        LineageEdge.edge_type == "READS_COLUMN",
                        LineageEdge.source_node.in_(cols),
                        LineageEdge.target_node.like("metric:%"),
                    )
                )
        if dimension_ids:
            dims = {d for d in dimension_ids if d}
            if dims:
                or_parts.append(
                    and_(
                        LineageEdge.edge_type == "USES_DIMENSION",
                        LineageEdge.target_node.in_(dims),
                    )
                )
        if not or_parts:
            return []
        stmt = select(LineageEdge).where(*conds, or_(*or_parts)).order_by(LineageEdge.id)
        return list((await self._db.execute(stmt)).scalars().all())

    async def field_mapping_target_tables(self, *, source_tables: set[str]) -> set[str]:
        """字段映射下游目标表集合：``source_table`` 命中给定表名的有效列映射去重。

        供 what-if 表起点预览补充"字段加工去向"——表级血缘边（``lineage_edge``
        table→table）未覆盖而字段映射（``lineage_field_mapping``）已建立的场景，
        仍能算出受影响物理表。

        Args:
            source_tables: 源表名集合（服务层负责补裸表名候选）。

        Returns:
            目标表名集合（``db.tbl`` 形式，未加 ``table:`` 前缀）。
        """
        names = {t for t in (source_tables or set()) if t}
        if not names:
            return set()
        rows = (
            await self._db.execute(
                select(LineageFieldMapping.target_table)
                .where(
                    LineageFieldMapping.deleted_at.is_(None),
                    LineageFieldMapping.source_column.is_not(None),
                    LineageFieldMapping.source_table.in_(names),
                )
                .distinct()
            )
        ).all()
        return {r[0] for r in rows}

    async def resolve_node_meta(self, node_ids: set[str]) -> dict[str, dict[str, Any]]:
        """批量解析血缘节点的基础元数据（影响分析/边列表响应的 ``nodes`` 字段）。

        与资产地图 ``graph_from_mysql`` 的节点映射对齐：
        - ``metric:`` 查 metric 表（domain/pii_flag/owner_id）；
        - ``table:`` 查 db_catalog join data_source（entity_id/domain/
          sensitivity→pii/owner_id，软删过滤）；
        - ``field:`` 无独立元数据表，展示 ``表.列`` 并由所属表继承业务域；
        - external/未知类型仅回填类型与 label（无目录实体）。

        Args:
            node_ids: 血缘节点 id 集合（如 ``table:db.orders`` / ``metric:gmv``）。

        Returns:
            ``{node_id: {id, type, label, entity_id, pii, domain, owner}}``。
        """
        result: dict[str, dict[str, Any]] = {}
        if not node_ids:
            return result
        metric_codes: set[str] = set()
        table_names: set[str] = set()
        # 字段所属表（继承业务域用）：即使表节点未显式出现在集合中也能解析域
        field_parents: set[str] = set()
        for nid in node_ids:
            if nid.startswith("metric:"):
                code = nid[len("metric:") :]
                metric_codes.add(code)
                result[nid] = self._fallback_node_meta(nid, "metric", code)
            elif nid.startswith("table:"):
                name = nid[len("table:") :]
                table_names.add(name)
                result[nid] = self._fallback_node_meta(nid, "table", name)
            elif nid.startswith("field:"):
                result[nid] = self._fallback_node_meta(nid, "field", nid[len("field:") :])
                field_parents.add(nid[len("field:") :].rsplit(".", 1)[0])
            else:
                prefix = nid.split(":", 1)[0] if ":" in nid else ""
                ntype = "external" if prefix == "external" else "other"
                label = nid.split(":", 1)[1] if ":" in nid else nid
                result[nid] = self._fallback_node_meta(nid, ntype, label)
        if metric_codes:
            rows = (
                await self._db.execute(
                    select(
                        Metric.metric_code,
                        Metric.domain,
                        Metric.pii_flag,
                        Metric.owner_id,
                        Metric.dw_layer,
                    ).where(
                        Metric.metric_code.in_(metric_codes),
                        Metric.deleted_at.is_(None),
                    )
                )
            ).all()
            for r in rows:
                nid = f"metric:{r.metric_code}"
                result[nid] = {
                    "id": nid,
                    "type": "metric",
                    "label": r.metric_code,
                    "entity_id": None,
                    "pii": bool(r.pii_flag),
                    "domain": r.domain,
                    "owner": str(r.owner_id) if r.owner_id else None,
                    # 数仓分层（前端血缘泳道/层色描边消费）：小写归一化（ODS→ods），
                    # 空值返回 None 保持"未分层"语义（前端 layerOf 据此回退）
                    "dw_layer": (r.dw_layer or "").lower() or None,
                }
        # 目录元数据：显式 table 节点 + 字段所属表（后者仅用于字段继承域，不产生条目）
        catalog_names = table_names | field_parents
        field_domain: dict[str, str | None] = {}
        if catalog_names:
            catalog_rows = (
                await self._db.execute(
                    select(
                        DBCatalog.id,
                        DBCatalog.entity_name,
                        DBCatalog.sensitivity_level,
                        DBCatalog.owner_id,
                        DataSource.domain,
                    )
                    .join(DataSource, DataSource.source_id == DBCatalog.source_id)
                    .where(
                        DBCatalog.entity_name.in_(catalog_names),
                        DBCatalog.deleted_at.is_(None),
                        DBCatalog.entity_type.in_(["TABLE", "VIEW"]),
                        DataSource.deleted_at.is_(None),
                    )
                )
            ).all()
            for cr in catalog_rows:
                field_domain[cr.entity_name] = cr.domain
                if cr.entity_name in table_names:
                    nid = f"table:{cr.entity_name}"
                    result[nid] = {
                        "id": nid,
                        "type": "table",
                        "label": cr.entity_name,
                        "entity_id": cr.id,
                        "pii": "PII" in (cr.sensitivity_level or ""),
                        "domain": cr.domain,
                        "owner": str(cr.owner_id) if cr.owner_id else None,
                    }
        # 字段节点继承所属表业务域（field 无独立元数据，域取自身份上层的表）
        for nid, meta in result.items():
            if nid.startswith("field:"):
                table_part = nid[len("field:") :].rsplit(".", 1)[0]
                dom = field_domain.get(table_part)
                if dom:
                    meta["domain"] = dom
        # 表节点数仓分层：以 dw_layer 字典为唯一事实源，按 ``库.表`` 名派生
        # （库名后缀命中字典 active 码即归层；字典未收录的分层在管理员补录后
        # 自动归层，无需重新采集）。指标侧已随 Metric.dw_layer 下发，表侧在此统一派生。
        layer_codes = await load_active_dw_layer_codes(self._db)
        for nid, meta in result.items():
            if nid.startswith("table:"):
                meta["dw_layer"] = derive_dw_layer_from_catalog_name(
                    nid[len("table:") :], layer_codes
                )
        return result

    @staticmethod
    def _fallback_node_meta(nid: str, ntype: str, label: str) -> dict[str, Any]:
        """节点兜底元数据：未在库中命中目录实体时的类型/label（entity_id 为空）。"""
        return {
            "id": nid,
            "type": ntype,
            "label": label,
            "entity_id": None,
            "pii": False,
            "domain": None,
            "owner": None,
        }

    async def graph_from_edges(
        self,
        *,
        provenance: str | None = None,
        limit: int = 2000,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """从血缘边直接构建表级血缘图谱（不依赖 db_catalog 交集）。

        与 ``AssetMapRepository.graph_from_mysql`` 不同：该方法是资产视角——
        节点只取 db_catalog（采集目录）里的表 + 指标，DP 元数据导入的表（如
        ``wedw_dwd.tjhis_*``）不在采集目录就进不了图。本方法从 ``lineage_edge``
        权威存储出发：节点 = 血缘边两端的所有 ``table:``/``metric:``/``field:``
        节点（去重），再经 ``resolve_node_meta`` 富集目录元数据（命中 db_catalog
        则带 entity_id/domain/pii/owner；未命中保留基础 label，供搜索定位）。

        Args:
            provenance: 按来源通道过滤（dp_csv / sqlglot / metric_definition）；
                为 ``None`` 时返回全部通道血缘。
            limit: 边数上限（默认 2000，前端力导向图另行限流节点数）。

        Returns:
            ``(nodes, edges)``——边为**自包含子图**（节点集来自边两端，天然自包含）。
        """
        stmt = select(
            LineageEdge.source_node,
            LineageEdge.target_node,
            LineageEdge.edge_type,
        ).where(LineageEdge.deleted_at.is_(None))
        if provenance:
            stmt = stmt.where(provenance_contains(LineageEdge.provenance, provenance))
        rows = (await self._db.execute(stmt.limit(limit))).all()
        if not rows:
            return [], []

        node_ids: set[str] = set()
        edges: list[dict[str, Any]] = []
        for r in rows:
            node_ids.add(str(r.source_node))
            node_ids.add(str(r.target_node))
            edges.append(
                {
                    "source": str(r.source_node),
                    "target": str(r.target_node),
                    "type": str(r.edge_type),
                }
            )

        meta = await self.resolve_node_meta(node_ids)
        nodes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for nid in sorted(node_ids):
            if nid in seen:
                continue
            seen.add(nid)
            m = meta.get(nid) or self._fallback_node_meta(
                nid,
                "metric" if nid.startswith("metric:") else "table",
                nid.split(":", 1)[1] if ":" in nid else nid,
            )
            nodes.append(
                {
                    "id": nid,
                    "type": m["type"],
                    "label": m["label"],
                    "entity_id": m.get("entity_id"),
                    "pii": bool(m.get("pii")),
                    "domain": m.get("domain"),
                    "owner": m.get("owner"),
                    # 数仓分层：resolve_node_meta 已按 dw_layer 字典为 table: 节点派生
                    # （整库名/分段码/表前缀三形态归层），此处必须透传，否则血缘图谱
                    # provenance=all 主视图的表节点全部落回「未分层」泳道
                    "dw_layer": m.get("dw_layer"),
                }
            )
        return nodes, edges

    async def graph_from_field_mappings(
        self,
        *,
        limit: int = 1200,
        pair_cap: int = 80,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """从字段映射构建字段级血缘图谱（「显示字段」图层懒加载用）。

        ``lineage_field_mapping`` 全量去重后约 3.6 万条字段边，直接全量返回
        JSON 体积（MB 级）与渲染量都不可接受。两步聚合控制规模：

        1. 表对热度：按 ``(source_table, target_table)`` 聚合计数取 top
           ``pair_cap`` 个表对——字段边集中在最有信息量的加工链路上（与表级
           图谱「边数上限截断」的取前语义对齐，而非字母序偏斜）；
        2. 字段边：仅取命中表对的去重字段映射（四元组 GROUP BY + MAX
           expression），按 ``limit`` 截断。

        节点 = 边两端 ``field:{表}.{列}`` 去重，经 ``resolve_node_meta`` 富集
        （字段继承所属表业务域/PII）；域收敛由 service 层完成（与
        ``graph_from_edges`` 的 provenance 分支同模式）。

        Returns:
            ``(nodes, edges)``——边为自包含子图（节点集来自边两端）。
        """
        # 步骤 1：表对热度 top N（决定字段边落在哪些加工链路上）
        pair_rows = (
            await self._db.execute(
                select(
                    LineageFieldMapping.source_table,
                    LineageFieldMapping.target_table,
                    func.count().label("cnt"),
                )
                .where(LineageFieldMapping.deleted_at.is_(None))
                .group_by(
                    LineageFieldMapping.source_table,
                    LineageFieldMapping.target_table,
                )
                .order_by(func.count().desc())
                .limit(pair_cap)
            )
        ).all()
        if not pair_rows:
            return [], []
        # concat 键做 IN 过滤（MySQL 元组 IN 方言不稳，concat 键全表扫描 3.6 万行可忽略）
        pair_keys = {f"{r.source_table}\x1f{r.target_table}" for r in pair_rows}
        pair_key = func.concat(
            LineageFieldMapping.source_table,
            "\x1f",
            LineageFieldMapping.target_table,
        )
        # 步骤 2：命中表对的去重字段边（四元组聚合；聚合/计算列 source_column 为
        # 空时该映射由表级血缘承载，不伪造字段边——与 field-drill 口径一致）
        rows = (
            await self._db.execute(
                select(
                    LineageFieldMapping.source_table,
                    LineageFieldMapping.source_column,
                    LineageFieldMapping.target_table,
                    LineageFieldMapping.target_column,
                    func.max(LineageFieldMapping.expression).label("expr"),
                )
                .where(
                    LineageFieldMapping.deleted_at.is_(None),
                    LineageFieldMapping.source_column.is_not(None),
                    pair_key.in_(pair_keys),
                )
                .group_by(
                    LineageFieldMapping.source_table,
                    LineageFieldMapping.source_column,
                    LineageFieldMapping.target_table,
                    LineageFieldMapping.target_column,
                )
                .order_by(
                    LineageFieldMapping.source_table,
                    LineageFieldMapping.source_column,
                    LineageFieldMapping.target_table,
                    LineageFieldMapping.target_column,
                )
                .limit(limit)
            )
        ).all()

        node_ids: set[str] = set()
        edges: list[dict[str, Any]] = []
        for r in rows:
            src_col = (r.source_column or "").strip()
            if not src_col or not r.source_table:
                continue
            src_id = f"field:{r.source_table}.{src_col}"
            dst_id = f"field:{r.target_table}.{r.target_column}"
            node_ids.add(src_id)
            node_ids.add(dst_id)
            edges.append(
                {
                    "source": src_id,
                    "target": dst_id,
                    "type": "DERIVED_FROM",
                    "expression": r.expr,
                }
            )
        if not edges:
            return [], []

        meta = await self.resolve_node_meta(node_ids)
        nodes: list[dict[str, Any]] = []
        for nid in sorted(node_ids):
            m = meta.get(nid) or self._fallback_node_meta(nid, "field", nid[len("field:") :])
            nodes.append(
                {
                    "id": nid,
                    "type": "field",
                    "label": nid[len("field:") :].rsplit(".", 1)[-1],
                    "table": nid[len("field:") :].rsplit(".", 1)[0],
                    "pii": bool(m.get("pii")),
                    "domain": m.get("domain"),
                }
            )
        return nodes, edges

    # ---- 消费方节点注册（Task A）----

    async def list_active_consumers_for_metric(self, metric_code: str) -> list[str]:
        """返回消费指定指标的活动接入方 ``client_id``（消费方节点注册用）。

        命中规则：状态为 ACTIVE、未软删，且 ``metric_whitelist`` 为空（=域内全量）
        或白名单含该指标。接入方数量级小，拉取活跃接入方后在 Python 侧按白名单过滤，
        避免 JSON 包含查询的方言耦合。

        Args:
            metric_code: 指标编码。

        Returns:
            应建立 ``metric:{code} → consumer:{client_id}`` 边的 client_id 列表。
        """
        rows = (
            await self._db.execute(
                select(ApiClient.client_id, ApiClient.metric_whitelist).where(
                    ApiClient.status == ApiClientStatus.ACTIVE,
                    ApiClient.deleted_at.is_(None),
                )
            )
        ).all()
        return [
            r.client_id
            for r in rows
            if r.metric_whitelist is None or metric_code in (r.metric_whitelist or [])
        ]

    # ---- 血缘覆盖率治理（Task B）----

    async def metric_total(self, domain: str | None = None) -> int:
        """指标总数（soft 删除过滤）。

        ``domain`` 非空时仅统计该业务域指标（治理统计的读路径域收敛）。
        """
        stmt = select(func.count(Metric.id)).where(Metric.deleted_at.is_(None))
        if domain:
            stmt = stmt.where(Metric.domain == domain)
        n = await self._db.execute(stmt)
        return int(n.scalar_one_or_none() or 0)

    async def metric_codes_with_lineage(self) -> set[str]:
        """有血缘边的指标编码集合（血缘边任意一端的 ``metric:`` 节点去重）。"""
        src_q = select(LineageEdge.source_node.label("n")).where(LineageEdge.deleted_at.is_(None))
        tgt_q = select(LineageEdge.target_node.label("n")).where(LineageEdge.deleted_at.is_(None))
        union = src_q.union(tgt_q).subquery()
        rows = (await self._db.execute(select(union.c.n))).all()
        return {str(r[0])[len("metric:") :] for r in rows if str(r[0]).startswith("metric:")}

    async def all_metric_rows(self) -> list[tuple[str, str | None]]:
        """全量指标 ``(metric_code, domain)``（孤儿指标清单用）。"""
        rows = (
            await self._db.execute(
                select(Metric.metric_code, Metric.domain).where(Metric.deleted_at.is_(None))
            )
        ).all()
        return [(r.metric_code, r.domain) for r in rows]

    async def table_total(self) -> int:
        """采集目录表总数（TABLE/VIEW，soft 删除过滤）。"""
        n = await self._db.execute(
            select(func.count(DBCatalog.id)).where(
                DBCatalog.deleted_at.is_(None),
                DBCatalog.entity_type.in_(["TABLE", "VIEW"]),
            )
        )
        return int(n.scalar_one_or_none() or 0)

    async def table_no_downstream_count(self) -> int:
        """无下游血缘的表数：作为血缘边目标、但从未作为边源的 ``table:`` 节点数。"""
        src_rows = (
            await self._db.execute(
                select(func.distinct(LineageEdge.source_node)).where(
                    LineageEdge.deleted_at.is_(None),
                    LineageEdge.source_node.like("table:%"),
                )
            )
        ).all()
        tgt_rows = (
            await self._db.execute(
                select(func.distinct(LineageEdge.target_node)).where(
                    LineageEdge.deleted_at.is_(None),
                    LineageEdge.target_node.like("table:%"),
                )
            )
        ).all()
        sources = {str(r[0]) for r in src_rows}
        targets = {str(r[0]) for r in tgt_rows}
        return len(targets - sources)

    async def table_nodes_in_edges(self) -> int:
        """血缘边中出现的去重 ``table:`` 节点数（source ∪ target，软删过滤）。

        用于健康度「表端到端完整度」分母：与 ``table_no_downstream_count`` 同口径
        （均来自血缘边），避免与采集目录 ``table_total``（口径不同）混算。
        """
        src_q = select(func.distinct(LineageEdge.source_node)).where(
            LineageEdge.deleted_at.is_(None), LineageEdge.source_node.like("table:%")
        )
        tgt_q = select(func.distinct(LineageEdge.target_node)).where(
            LineageEdge.deleted_at.is_(None), LineageEdge.target_node.like("table:%")
        )
        src_rows = (await self._db.execute(src_q)).all()
        tgt_rows = (await self._db.execute(tgt_q)).all()
        return len({str(r[0]) for r in src_rows} | {str(r[0]) for r in tgt_rows})

    async def edge_total(self) -> int:
        """血缘边总数（soft 删除过滤）。"""
        n = await self._db.execute(
            select(func.count(LineageEdge.id)).where(LineageEdge.deleted_at.is_(None))
        )
        return int(n.scalar_one_or_none() or 0)

    async def metric_reuse_counts(self) -> dict[str, dict[str, int]]:
        """按指标聚合被引用统计（复用度分析数据源，P0）。

        统计以 ``metric:{code}`` 为 source_node 的**存活**血缘边（source 为被依赖方）：
        - ``DERIVED_FROM`` → target 为派生该指标的派生指标（计派生引用数）
        - ``CONSUMED_BY`` → target 为消费方节点（计报表/接入方引用数）

        同一边类型下按 target 去重（同一派生指标/消费方可能有多条同类型边），
        失效队列边（``stale=True``）不计入——「被引用」只统计当前生效的引用。

        Returns:
            ``{metric_code: {"derived_by": int, "consumed_by": int}}``；无引用边
            的指标不出现在结果中（由上层以 0 兜底）。
        """
        rows = (
            await self._db.execute(
                select(
                    LineageEdge.source_node,
                    LineageEdge.edge_type,
                    func.count(func.distinct(LineageEdge.target_node)),
                )
                .where(
                    LineageEdge.deleted_at.is_(None),
                    LineageEdge.stale.is_(False),
                    LineageEdge.source_node.like("metric:%"),
                    LineageEdge.edge_type.in_(["DERIVED_FROM", "CONSUMED_BY"]),
                )
                .group_by(LineageEdge.source_node, LineageEdge.edge_type)
            )
        ).all()
        out: dict[str, dict[str, int]] = {}
        for node, edge_type, cnt in rows:
            code = str(node)[len("metric:") :]
            bucket = out.setdefault(code, {"derived_by": 0, "consumed_by": 0})
            bucket["derived_by" if edge_type == "DERIVED_FROM" else "consumed_by"] = int(cnt)
        return out

    async def metric_referrers(self, metric_code: str) -> list[dict[str, str]]:
        """返回引用指定指标的活跃血缘引用者（deprecate 被引用拦截用）。

        以 ``metric:{code}`` 为 source_node 的存活边：``DERIVED_FROM`` → target 为
        派生该指标的派生指标；``BASED_ON`` → target 为以该指标为基础原子的派生指标
        （OneData 派生 = 基础原子 + 业务限定 + 时间周期）；``CONSUMED_BY`` → target 为
        消费方/报表。stale 与
        软删边过滤——「被引用」只统计当前生效的引用，废弃被引用指标会让下游
        引用悬空，调用方据此在未指定替代指标时拦截废弃。

        Args:
            metric_code: 指标编码。

        Returns:
            ``[{"node": "metric:xxx"|"consumer:xxx"|"table:xxx", "edge_type": ...}]``
            按 node 去重；无引用返回空列表。
        """
        rows = (
            await self._db.execute(
                select(LineageEdge.target_node, LineageEdge.edge_type)
                .where(
                    LineageEdge.deleted_at.is_(None),
                    LineageEdge.stale.is_(False),
                    LineageEdge.source_node == f"metric:{metric_code}",
                    LineageEdge.edge_type.in_(
                        ["DERIVED_FROM", "BASED_ON", "CONSUMED_BY"]
                    ),
                )
                .distinct()
            )
        ).all()
        return [{"node": str(r[0]), "edge_type": str(r[1])} for r in rows]

    async def metric_referrers_batch(
        self, metric_codes: list[str]
    ) -> dict[str, list[dict[str, str]]]:
        """批量查询多指标的被引用情况（批量下线下游审查用）。

        一次 ``source_node IN (...)`` 查询返回所有指标的存活引用边，避免
        逐个调用 :meth:`metric_referrers` 的 N 次查询。语义与单查完全一致：
        ``DERIVED_FROM`` → 派生该指标的派生指标；``CONSUMED_BY`` → 消费方/报表；
        stale 与软删边过滤。

        Args:
            metric_codes: 指标编码列表。

        Returns:
            ``{metric_code: [{"node": "metric:xxx"|"consumer:xxx"|"table:xxx", "edge_type": ...}]}``
            每个入参编码都有键（无引用为空列表）。
        """
        if not metric_codes:
            return {}
        rows = (
            await self._db.execute(
                select(LineageEdge.source_node, LineageEdge.target_node, LineageEdge.edge_type)
                .where(
                    LineageEdge.deleted_at.is_(None),
                    LineageEdge.stale.is_(False),
                    LineageEdge.source_node.in_([f"metric:{c}" for c in metric_codes]),
                    LineageEdge.edge_type.in_(["DERIVED_FROM", "CONSUMED_BY"]),
                )
                .distinct()
            )
        ).all()
        out: dict[str, list[dict[str, str]]] = {c: [] for c in metric_codes}
        for source, target, edge_type in rows:
            code = str(source)[len("metric:") :]
            if code in out:
                out[code].append({"node": str(target), "edge_type": str(edge_type)})
        return out

    async def list_all_edges(self, limit: int | None = None) -> list[LineageEdge]:
        """取出全部未删除血缘边（断链校验用，按 id 升序）。"""
        stmt = select(LineageEdge).where(LineageEdge.deleted_at.is_(None)).order_by(LineageEdge.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self._db.execute(stmt)).scalars().all())

    async def list_export_edges(
        self,
        *,
        node: str | None = None,
        direction: str = "both",
        granularity: str | None = None,
        provenance: str | None = None,
        limit: int = 10_000,
    ) -> list[LineageEdge]:
        """血缘导出查询：按节点/方向/粒度/来源过滤全部未删除边（P4 标准导出）。

        与影响分析（图优先/分页）不同，导出是全量开放 API 的数据源：无节点过滤时
        返回全部边（按 id 升序、limit 截断），带节点时仅返回该节点直接相关边
        （``direction`` 控制上游/下游/双向，与 ``edges_for_node`` 语义一致）。

        Args:
            node: 可选节点 id（带前缀，如 ``table:db.tbl``）。
            direction: 节点过滤方向（upstream=目标为该节点 / downstream=源为该节点 /
                both=任一方向）。
            granularity: 可选粒度过滤（L1/L2/L3）。
            provenance: 可选来源通道过滤。
            limit: 返回边数上限。

        Returns:
            过滤后的未删除血缘边列表（按 id 升序）。
        """
        stmt = select(LineageEdge).where(LineageEdge.deleted_at.is_(None))
        if granularity:
            stmt = stmt.where(LineageEdge.granularity == granularity)
        if provenance:
            stmt = stmt.where(provenance_contains(LineageEdge.provenance, provenance))
        if node:
            if direction == "upstream":
                stmt = stmt.where(LineageEdge.target_node == node)
            elif direction == "downstream":
                stmt = stmt.where(LineageEdge.source_node == node)
            else:
                stmt = stmt.where(
                    or_(LineageEdge.source_node == node, LineageEdge.target_node == node)
                )
        stmt = stmt.order_by(LineageEdge.id).limit(limit)
        return list((await self._db.execute(stmt)).scalars().all())

    async def coverage_broken_edges(self, limit: int = 200) -> list[dict[str, Any]]:
        """断链边明细：source 节点对应的目录/指标实体已不存在。

        仅校验 ``metric:``（metrics 表）与 ``table:``（db_catalog 表）；``field:``/
        ``external:``/``consumer:`` 节点为派生或约定占位，不参与断链判定。
        返回按 ``limit`` 截断的边明细 dict 列表（自上而下校验）。

        Args:
            limit: 返回断链边条数上限。

        Returns:
            断链边 ``[{id, source_node, target_node, edge_type, granularity,
            confidence, provenance}]``。
        """
        edges = await self.list_all_edges()
        metric_keys = sorted(
            {s[len("metric:") :] for s in self._edge_sources(edges) if s.startswith("metric:")}
        )
        table_keys = sorted(
            {s[len("table:") :] for s in self._edge_sources(edges) if s.startswith("table:")}
        )
        existing_metrics: set[str] = set()
        if metric_keys:
            rows = await self._db.execute(
                select(Metric.metric_code).where(
                    Metric.metric_code.in_(metric_keys), Metric.deleted_at.is_(None)
                )
            )
            existing_metrics = {r.metric_code for r in rows.all()}
        existing_tables: set[str] = set()
        if table_keys:
            rows = await self._db.execute(
                select(DBCatalog.entity_name).where(
                    DBCatalog.entity_name.in_(table_keys), DBCatalog.deleted_at.is_(None)
                )
            )
            existing_tables = {r.entity_name for r in rows.all()}
        broken: list[dict[str, Any]] = []
        for e in edges:
            src = e.source_node
            metric_break = src.startswith("metric:") and (
                src[len("metric:") :] not in existing_metrics
            )
            table_break = src.startswith("table:") and src[len("table:") :] not in existing_tables
            if metric_break or table_break:
                broken.append(self._edge_dict(e))
            if len(broken) >= limit:
                break
        return broken

    @staticmethod
    def _edge_sources(edges: list[LineageEdge]) -> set[str]:
        """取边集合的 source 节点集合（断链归集用）。"""
        return {e.source_node for e in edges}

    @staticmethod
    def _edge_dict(e: LineageEdge) -> dict[str, Any]:
        """血缘边 → 响应 dict（断链明细序列化）。"""
        return {
            "id": e.id,
            "source_node": e.source_node,
            "target_node": e.target_node,
            "edge_type": e.edge_type,
            "granularity": e.granularity,
            "confidence": e.confidence,
            "provenance": e.provenance,
        }

    # ---- 血缘边变更历史（Task D）----

    async def edge_history_by_key(
        self,
        source_node: str,
        target_node: str,
        edge_type: str,
        granularity: str,
    ) -> list[LineageEdgeHistory]:
        """按边唯一键取该边的变更历史快照（按时间倒序）。"""
        return list(
            (
                await self._db.execute(
                    select(LineageEdgeHistory)
                    .where(
                        LineageEdgeHistory.source_node == source_node,
                        LineageEdgeHistory.target_node == target_node,
                        LineageEdgeHistory.edge_type == edge_type,
                        LineageEdgeHistory.granularity == granularity,
                    )
                    .order_by(LineageEdgeHistory.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

    # ---- 健康度（P2）与路径查询（P3）----

    async def stale_edge_count(self) -> int:
        """当前失效（stale=True 且未删除）边总数（健康度失效率维度原料）。"""
        n = await self._db.execute(
            select(func.count(LineageEdge.id)).where(
                LineageEdge.stale.is_(True), LineageEdge.deleted_at.is_(None)
            )
        )
        return int(n.scalar_one_or_none() or 0)

    async def latest_ingest_run_time(self) -> datetime | None:
        """全部通道最近一次采集运行时间（健康度新鲜度维度原料）。

        跨 ``lineage_ingest_run`` 取 ``run_at`` 最大值；无任何运行记录返回 None。
        """
        row = (
            await self._db.execute(select(func.max(LineageIngestRun.run_at)))
        ).scalar_one_or_none()
        return row if isinstance(row, datetime) else None

    async def find_paths(
        self, source: str, target: str, max_hops: int = 5, limit: int = 50
    ) -> list[list[LineageEdge]]:
        """DFS 找 ``source`` → ``target`` 的全部有向路径（MySQL 兜底，图不可达时用）。

        边方向即血缘方向（source 上游 → target 下游）；沿 ``_edges_from`` 展开，
        不回溯（跳过已访问节点防环），跳数上限 ``max_hops``、路径条数上限 ``limit``。

        Args:
            source: 起点节点 id。
            target: 终点节点 id。
            max_hops: 最大跳数（边数），小于 1 时返回空。
            limit: 返回路径条数上限。

        Returns:
            每条路径为 ``[LineageEdge, ...]``（source 起 → target 止）。
        """
        if max_hops < 1:
            return []
        adj: dict[str, list[LineageEdge]] = {}

        async def _downstream(n: str) -> list[LineageEdge]:
            if n not in adj:
                adj[n] = await self._edges_from(n)
            return adj[n]

        paths: list[list[LineageEdge]] = []

        async def _dfs(node: str, current: list[LineageEdge], visited: set[str]) -> None:
            if len(paths) >= limit:
                return
            for e in await _downstream(node):
                if e.target_node in visited:
                    continue
                nxt = current + [e]
                if e.target_node == target:
                    paths.append(nxt)
                    if len(paths) >= limit:
                        return
                elif len(nxt) < max_hops:
                    visited.add(e.target_node)
                    await _dfs(e.target_node, nxt, visited)
                    visited.remove(e.target_node)

        await _dfs(source, [], {source})
        return paths

    async def find_terminals(
        self, node: str, max_hops: int = 5, limit: int = 100
    ) -> list[tuple[str, list[str]]]:
        """从 ``node`` 沿下游 DFS 找终止节点（无下游边的死端，断链定位用）。

        语义：从起点沿血缘方向展开，深度 ≤ ``max_hops``，收集所有「无下游边」
        的节点——它们是合理边界（如 ADS 结果表）或断链嫌疑点（对应实体已不存在
        但仍被边引用，实体存在性由上层结合权威库判定）。

        Args:
            node: 起点节点 id。
            max_hops: 最大搜索深度（跳数），小于 1 时返回空。
            limit: 返回终止节点数上限。

        Returns:
            ``[(terminal_node, path_nodes)]``，``path_nodes`` 为从起点到该节点的
            最短节点序列（含起点）。
        """
        if max_hops < 1:
            return []
        adj: dict[str, list[LineageEdge]] = {}
        best: dict[str, list[str]] = {}
        terminals: list[tuple[str, list[str]]] = []

        async def _downstream(n: str) -> list[LineageEdge]:
            if n not in adj:
                adj[n] = await self._edges_from(n)
            return adj[n]

        async def _dfs(n: str, path: list[str], visited: set[str]) -> None:
            if len(terminals) >= limit:
                return
            edges = await _downstream(n)
            if not edges:
                if n not in best or len(path) < len(best[n]):
                    best[n] = list(path)
                if all(t[0] != n for t in terminals):
                    terminals.append((n, list(path)))
                return
            if len(path) >= max_hops:
                return  # 达到搜索深度上限：非死端，不深入
            for e in edges:
                if e.target_node in visited:
                    continue
                visited.add(e.target_node)
                await _dfs(e.target_node, path + [e.target_node], visited)
                visited.remove(e.target_node)

        await _dfs(node, [node], {node})
        return terminals

    async def entity_exists(self, node: str) -> bool:
        """节点对应实体在权威库中是否存在（断链嫌疑判定）。

        仅判定 ``metric:``（metrics 表）与 ``table:``（db_catalog 表，含 soft 删除
        过滤）；``field:``/``external:``/``consumer:`` 等派生或约定占位节点中性
        返回 True（不构成断链）。
        """
        if node.startswith("metric:"):
            code = node[len("metric:") :]
            row = await self._db.execute(
                select(Metric.metric_code).where(
                    Metric.metric_code == code, Metric.deleted_at.is_(None)
                )
            )
            return row.scalar_one_or_none() is not None
        if node.startswith("table:"):
            name = node[len("table:") :]
            row = await self._db.execute(
                select(DBCatalog.entity_name).where(
                    DBCatalog.entity_name == name, DBCatalog.deleted_at.is_(None)
                )
            )
            return row.scalar_one_or_none() is not None
        return True

    async def affected_asset_owners(
        self, node: str, max_hops: int = 3, limit: int = 50
    ) -> set[str]:
        """收集 ``node`` 及其下游受影响资产（``table:``/``metric:``）的 Owner 集合。

        DDL 变更事件化用：表/列重命名、DROP TABLE 会让下游资产血缘断裂/失效，
        仅靠缓存失效用户感知不到，需定向通知受影响资产 Owner（治理闭环）。

        沿血缘边下游 DFS（复用 ``_edges_from``），收集 ``table:``/``metric:``
        节点（含起点自身）后经权威库解析 owner_id：``table:`` 查 db_catalog
        join data_source、``metric:`` 查 metric，软删过滤；无 Owner 的资产不计入。

        Args:
            node: 起点节点 id。
            max_hops: 下游最大搜索深度（跳数）。
            limit: 收集的受影响资产节点数上限（防超大下游刷屏）。

        Returns:
            Owner user_id 集合（去重）。
        """
        if max_hops < 1:
            return set()
        # P2（审查修复）：DFS 改为分层 BFS——每层对当前全部节点批量取下游边
        # （_edges_from_many 一次 IN 查询），此前每访问一个节点发一次 SELECT，
        # 1000 节点 × 2ms ≈ 2s+ 串行。
        affected: set[str] = set()
        current_layer = [node]
        visited: set[str] = {node}
        depth = 0
        while current_layer and depth < max_hops and len(affected) < limit:
            edges = await self._edges_from_many(current_layer)
            next_layer: list[str] = []
            for e in edges:
                t = e.target_node
                if t in visited:
                    continue
                visited.add(t)
                if t.startswith("table:") or t.startswith("metric:"):
                    affected.add(t)
                    if len(affected) >= limit:
                        break
                next_layer.append(t)
            current_layer = next_layer
            depth += 1

        if node.startswith("table:") or node.startswith("metric:"):
            affected.add(node)
        if not affected:
            return set()
        meta = await self.resolve_node_meta(affected)
        owners: set[str] = set()
        for m in meta.values():
            if m.get("owner") is not None:
                owners.add(str(m["owner"]))
        return owners
