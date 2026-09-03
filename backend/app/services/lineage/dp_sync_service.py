"""dp 调度血缘同步编排服务（单 step 三态处理 + 写入 + 资产回填）。

对齐 `spec/dp-lineage-ingest/plan.md` §4/§5/§6 与决策 D4/D5/D6/D10：
- 单节点（step）经 ``parse_dp_step`` 三态判定后：
    * ok + 简单 → 直接入库（表级 L1 边 + 字段映射独立表 + dp_task_refs 静态快照）
    * ok + 复杂 → LLM 共识确认（一致入库 / 分歧建待抉择单）
    * failed → LLM 兜底提炼（建 llm_fallback 参考单 / unparseable 原文单）
    * no_flow → 跳过（无数据流转）
- 裁决记忆复用：同 step + sql_hash 已裁决 → 自动沿用（accept_sqlglot 入库 /
  accept_llm 应用意见 / manual 应用手填 / ignore 跳过），不重复进待抉择
- 资产 Owner 回填（D10）：产出表资产 owner 为空时按 director 回填（自动创建影子用户）
- 写入语义（D4）：表级边 upsert 天然聚合（同 source/target 复用），dp_task_refs
  按 step_id 去重合并；字段映射按 uq 幂等

LLM 调用由调用方注入 ``llm_chat``（async (messages, **kw) -> {content}），
便于 mock/成本/熔断控制；未注入或 llm_enabled=False 时走「纯 sqlglot + 待抉择」。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.db.mysql import AsyncSession
from app.models.lineage import LineageEdge
from app.services.lineage.dp_sync_llm import (
    ConfirmVerdict,
    DpSyncLlmError,
    build_confirm_messages,
    build_fallback_messages,
    edges_to_json,
    parse_confirm_response,
    parse_fallback_response,
)
from app.services.lineage.dp_sync_parser import parse_dp_step
from app.services.lineage.dp_sync_repo import DpLineageRepository
from app.services.lineage.parser import node_table
from app.services.lineage.repository import LineageRepository

logger = logging.getLogger(__name__)

#: 单次 step LLM 生成上限（确认/兜底均够用）。
_LLM_MAX_TOKENS = 2000

#: dp 通道 provenance 标记。
DP_PROVENANCE = "dp_sql"


def sql_fingerprint(sql: str) -> str:
    """SQL 内容指纹（裁决记忆 key：同内容不重复进单）。"""
    return hashlib.sha256((sql or "").encode("utf-8")).hexdigest()


def build_task_ref(task: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    """从 dp task/step 行构建 dp_task_refs 数组元素（静态身份 + 准静态元数据）。

    只取 D10 约定的静态/准静态字段；动态运行态（task_state/run_status/run_time）
    不落边（展示层实时旁路）。
    """
    task_fields = (
        "task_id",
        "task_no",
        "task_name",
        "out_table",
        "director",
        "created_user_id",
        "modified_user_id",
        "checker",
        "settle_project_director",
        "project_id",
        "settle_project_name",
        "settle_department_name",
        "budget_unit_name",
        "cycle",
        "cron_express",
        "week_day",
        "month_day",
        "specific_time",
        "frequence",
        "remark",
        "task_version_desc",
        "task_version",
        "master_task_id",
        "is_master_task",
    )
    ref: dict[str, Any] = {}
    for key in task_fields:
        if key in task and task[key] is not None:
            ref[key] = task[key]
    step_fields = ("step_id", "step_name", "task_node_type", "task_step")
    for key in step_fields:
        if key in step and step[key] is not None:
            ref[key] = step[key]
    return ref


class DpSyncService:
    """dp 血缘同步编排服务。"""

    def __init__(
        self,
        db: AsyncSession,
        llm_chat: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._db = db
        self._llm_chat = llm_chat
        self._lineage_repo = LineageRepository(db)
        self._dp_repo = DpLineageRepository(db)

    # ---- 单 step 处理 ----
    async def process_step(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        sql: str,
        config: Any,
    ) -> dict[str, Any]:
        """处理单个 SQL 节点，返回结果摘要（run_log detail 项）。"""
        sql_hash = sql_fingerprint(sql)
        outcome = parse_dp_step(
            sql,
            dialect="hive",
            exclude_patterns=config.exclude_table_patterns,
            rules=config.llm_complexity_rules,
            target_table=task.get("out_table") or None,
        )
        if outcome.status == "no_flow":
            return {"step_id": step.get("step_id"), "status": "no_flow"}

        if outcome.status == "ok" and not outcome.is_complex:
            await self._store_sqlglot_edges(outcome, task, step, sql_hash, config)
            return {"step_id": step.get("step_id"), "status": "parsed_ok"}

        # 复杂或失败：先查裁决记忆（同 step+hash 已裁决自动沿用）
        if config.resolve_memory_enabled:
            reused = await self._reuse_resolution(
                task, step, sql, sql_hash, outcome, config
            )
            if reused:
                return reused

        if outcome.status == "ok":
            return await self._handle_complex(task, step, sql, sql_hash, outcome, config)
        return await self._handle_failed(task, step, sql, sql_hash, outcome, config)

    async def _handle_complex(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        sql: str,
        sql_hash: str,
        outcome: Any,
        config: Any,
    ) -> dict[str, Any]:
        """复杂节点：LLM 共识确认（一致入库 / 分歧建单）；LLM 关闭建待抉择单。"""
        if not config.llm_enabled or self._llm_chat is None:
            await self._dp_repo.create_ticket(
                task_id=task.get("task_id"),
                step_id=step.get("step_id"),
                task_name=task.get("task_name"),
                out_table=task.get("out_table"),
                sql_text=sql,
                sql_hash=sql_hash,
                status="diverged",
                sqlglot_result=edges_to_json(outcome.table_edges, outcome.field_edges),
                divergence_reason="LLM 已关闭（配置），复杂节点未确认，请人工抉择",
            )
            return {"step_id": step.get("step_id"), "status": "diverged"}

        try:
            verdict = await self._llm_confirm(sql, outcome)
        except DpSyncLlmError as exc:
            # LLM 输出异常/无法解析：不能静默入库，建待抉择单交人工
            await self._dp_repo.create_ticket(
                task_id=task.get("task_id"),
                step_id=step.get("step_id"),
                task_name=task.get("task_name"),
                out_table=task.get("out_table"),
                sql_text=sql,
                sql_hash=sql_hash,
                status="diverged",
                sqlglot_result=edges_to_json(outcome.table_edges, outcome.field_edges),
                divergence_reason=f"LLM 确认输出异常：{exc}",
            )
            return {"step_id": step.get("step_id"), "status": "diverged"}
        if verdict.agree:
            await self._store_sqlglot_edges(outcome, task, step, sql_hash, config)
            return {"step_id": step.get("step_id"), "status": "llm_confirmed"}
        # 分歧：建待抉择单（附 sqlglot 结果 + LLM 意见 + 原因）
        await self._dp_repo.create_ticket(
            task_id=task.get("task_id"),
            step_id=step.get("step_id"),
            task_name=task.get("task_name"),
            out_table=task.get("out_table"),
            sql_text=sql,
            sql_hash=sql_hash,
            status="diverged",
            sqlglot_result=edges_to_json(outcome.table_edges, outcome.field_edges),
            llm_opinion={
                "agree": False,
                "missing_edges": verdict.missing_edges,
                "wrong_edges": verdict.wrong_edges,
            },
            divergence_reason=verdict.reason or "sqlglot 与 LLM 意见不一致",
        )
        return {"step_id": step.get("step_id"), "status": "diverged"}

    async def _handle_failed(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        sql: str,
        sql_hash: str,
        outcome: Any,
        config: Any,
    ) -> dict[str, Any]:
        """失败节点：LLM 兜底提炼（llm_fallback / unparseable）；LLM 关闭建单。"""
        sqlglot_json = edges_to_json(outcome.table_edges, outcome.field_edges)
        if not config.llm_enabled or self._llm_chat is None:
            await self._dp_repo.create_ticket(
                task_id=task.get("task_id"),
                step_id=step.get("step_id"),
                task_name=task.get("task_name"),
                out_table=task.get("out_table"),
                sql_text=sql,
                sql_hash=sql_hash,
                status="unparseable",
                sqlglot_result=sqlglot_json,
                divergence_reason="LLM 已关闭（配置），无法兜底解析，请手动配置",
            )
            return {"step_id": step.get("step_id"), "status": "unparseable"}

        try:
            flow = await self._llm_fallback(sql)
        except DpSyncLlmError as exc:
            await self._dp_repo.create_ticket(
                task_id=task.get("task_id"),
                step_id=step.get("step_id"),
                task_name=task.get("task_name"),
                out_table=task.get("out_table"),
                sql_text=sql,
                sql_hash=sql_hash,
                status="unparseable",
                sqlglot_result=sqlglot_json,
                divergence_reason=f"LLM 兜底输出异常：{exc}",
            )
            return {"step_id": step.get("step_id"), "status": "unparseable"}
        if flow.ok:
            await self._dp_repo.create_ticket(
                task_id=task.get("task_id"),
                step_id=step.get("step_id"),
                task_name=task.get("task_name"),
                out_table=task.get("out_table"),
                sql_text=sql,
                sql_hash=sql_hash,
                status="llm_fallback",
                sqlglot_result=sqlglot_json,
                llm_opinion={
                    "target_tables": flow.target_tables,
                    "source_tables": flow.source_tables,
                    "field_mappings": flow.field_mappings,
                    "note": flow.note,
                },
                divergence_reason=flow.note or "sqlglot 解析失败，LLM 兜底提炼（低置信参考）",
            )
            return {"step_id": step.get("step_id"), "status": "llm_fallback"}
        await self._dp_repo.create_ticket(
            task_id=task.get("task_id"),
            step_id=step.get("step_id"),
            task_name=task.get("task_name"),
            out_table=task.get("out_table"),
            sql_text=sql,
            sql_hash=sql_hash,
            status="unparseable",
            sqlglot_result=sqlglot_json,
            llm_opinion={"note": flow.note},
            divergence_reason=flow.note or "sqlglot 与 LLM 均无法解析，请手动配置",
        )
        return {"step_id": step.get("step_id"), "status": "unparseable"}

    # ---- 裁决记忆复用 ----
    async def _reuse_resolution(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        sql: str,
        sql_hash: str,
        outcome: Any,
        config: Any,
    ) -> dict[str, Any] | None:
        ticket = await self._dp_repo.find_ticket_by_step_hash(
            step.get("step_id"), sql_hash
        )
        if ticket is None or ticket.status not in ("resolved", "ignored"):
            return None
        if ticket.resolution == "ignore" or ticket.status == "ignored":
            return {"step_id": step.get("step_id"), "status": "memory_ignored"}
        if ticket.resolution == "accept_sqlglot":
            await self._store_sqlglot_edges(outcome, task, step, sql_hash, config)
            return {"step_id": step.get("step_id"), "status": "memory_reused"}
        if ticket.resolution == "accept_llm":
            await self._apply_llm_opinion(
                ticket.llm_opinion, task, step, sql_hash, config
            )
            return {"step_id": step.get("step_id"), "status": "memory_reused"}
        if ticket.resolution == "manual":
            await self._apply_manual_edges(
                ticket.manual_edges_json, task, step, sql_hash, config
            )
            return {"step_id": step.get("step_id"), "status": "memory_reused"}
        return None

    # ---- LLM 调用 ----
    async def _llm_confirm(self, sql: str, outcome: Any) -> ConfirmVerdict:
        messages = build_confirm_messages(
            sql, edges_to_json(outcome.table_edges, outcome.field_edges)
        )
        result = await self._llm_chat(messages, max_tokens=_LLM_MAX_TOKENS)
        return parse_confirm_response(str(result.get("content") or ""))

    async def _llm_fallback(self, sql: str):
        messages = build_fallback_messages(sql)
        result = await self._llm_chat(messages, max_tokens=_LLM_MAX_TOKENS)
        return parse_fallback_response(str(result.get("content") or ""))

    # ---- 写入 ----
    async def _store_sqlglot_edges(
        self,
        outcome: Any,
        task: dict[str, Any],
        step: dict[str, Any],
        sql_hash: str,
        config: Any,
    ) -> None:
        """入库表级边 + dp_task_refs 合并 + 字段映射独立表（幂等聚合）。"""
        ref = build_task_ref(task, step)
        for te in outcome.table_edges:
            edge = await self._upsert_edge(te.source, te.target, task, step, ref)
            if edge is None:
                continue
            # 字段映射：匹配该表边的字段级边（source_table/target_table 对齐）
            for fe in outcome.field_edges:
                if fe.target_table == te.target and fe.source_table == te.source:
                    await self._dp_repo.upsert_field_mapping(
                        edge_id=edge.id,
                        source_table=fe.source_table,
                        source_column=fe.source_column,
                        target_table=fe.target_table,
                        target_column=fe.target_column,
                        expression=fe.expression,
                        degraded=fe.degraded,
                        confidence=1.0,
                        provenance="sqlglot",
                        sql_hash=sql_hash,
                        task_id=task.get("task_id"),
                        step_id=step.get("step_id"),
                    )

    async def _upsert_edge(
        self,
        source_table: str,
        target_table: str,
        task: dict[str, Any],
        step: dict[str, Any],
        ref: dict[str, Any],
    ) -> LineageEdge | None:
        sn = node_table(source_table)
        tn = node_table(target_table)
        probe = LineageEdge(
            source_node=sn, target_node=tn, edge_type="DERIVED_FROM", granularity="L1"
        )
        if await self._lineage_repo.would_create_cycle(probe):
            logger.warning("dp_sync_edge_cycle_skipped: %s -> %s", sn, tn)
            return None
        edge, _ = await self._lineage_repo.upsert_edge_with_status(
            source_node=sn,
            target_node=tn,
            edge_type="DERIVED_FROM",
            granularity="L1",
            provenance=DP_PROVENANCE,
            change_reason="dp_sync",
        )
        merged = DpLineageRepository.merge_task_refs(edge.dp_task_refs, ref)
        if merged != edge.dp_task_refs:
            edge.dp_task_refs = merged
        return edge

    async def _apply_llm_opinion(
        self,
        opinion: dict[str, Any] | None,
        task: dict[str, Any],
        step: dict[str, Any],
        sql_hash: str,
        config: Any,
    ) -> None:
        """采纳 LLM：把意见中补漏的边（missing_edges）入库（无字段映射，参考语义）。"""
        if not opinion:
            return
        ref = build_task_ref(task, step)
        for edge in opinion.get("missing_edges") or []:
            source = edge.get("source")
            target = edge.get("target")
            if source and target:
                await self._upsert_edge(source, target, task, step, ref)

    async def _apply_manual_edges(
        self,
        manual: dict[str, Any] | None,
        task: dict[str, Any],
        step: dict[str, Any],
        sql_hash: str,
        config: Any,
    ) -> None:
        """采纳手动配置：table_edges/field_mappings 手填入库。"""
        if not manual:
            return
        ref = build_task_ref(task, step)
        for te in manual.get("table_edges") or []:
            source = te.get("source")
            target = te.get("target")
            if not (source and target):
                continue
            edge = await self._upsert_edge(source, target, task, step, ref)
            if edge is None:
                continue
            for fm in manual.get("field_mappings") or []:
                if fm.get("source_table") == source and fm.get("target_table") == target:
                    await self._dp_repo.upsert_field_mapping(
                        edge_id=edge.id,
                        source_table=source,
                        source_column=fm.get("source_column"),
                        target_table=target,
                        target_column=fm.get("target_column"),
                        expression=fm.get("expression"),
                        degraded=bool(fm.get("degraded")),
                        confidence=0.5,
                        provenance="manual",
                        sql_hash=sql_hash,
                        task_id=task.get("task_id"),
                        step_id=step.get("step_id"),
                    )

    # ---- 资产 Owner 回填（D10） ----
    async def backfill_owner(self, task: dict[str, Any], config: Any) -> dict[str, Any]:
        """对任务产出表资产执行 owner 回填（仅孤儿 + director 匹配/影子用户）。

        Returns:
            {"backfilled": int, "shadow_created": bool}
        """
        if config.owner_backfill == "never":
            return {"backfilled": 0, "shadow_created": False}
        out_table = task.get("out_table")
        director = task.get("director")
        if not out_table or not director:
            return {"backfilled": 0, "shadow_created": False}
        catalogs = await self._dp_repo.find_orphan_catalogs(out_table)
        if not catalogs:
            return {"backfilled": 0, "shadow_created": False}
        user = await self._dp_repo.find_user_by_username(director)
        shadow_created = False
        if user is None:
            user = await self._dp_repo.create_shadow_user(director)
            shadow_created = True
        for catalog in catalogs:
            await self._dp_repo.update_catalog_owner(catalog.id, user.id)
        return {"backfilled": len(catalogs), "shadow_created": shadow_created}

    # ---- 扫描（D7 定时增量轮询） ----
    async def scan_once(self, fetch_collector: Callable[[str], Awaitable[Any]]) -> dict[str, Any]:
        """执行一轮 dp 血缘增量扫描（由 arq 1 分钟 ticker 按间隔触发）。

        Args:
            fetch_collector: ``async (source_id) -> collector``（含 query/dispose）。
                由部署侧注入（tasks 用真实 dp 连接），测试注入假 collector。
        """
        config = await self._dp_repo.get_config()
        if config is None or not config.enabled:
            return {"skipped": "not_configured_or_disabled"}
        wm = await self._dp_repo.get_watermark("task")
        now = datetime.now(UTC)
        if wm is not None and wm.last_scan_at is not None:
            interval = max(1, int(config.poll_interval_minutes or 5)) * 60
            if (now - wm.last_scan_at).total_seconds() < interval:
                return {"skipped": "interval_not_due"}
        run = await self._dp_repo.create_run_log(status="running", run_at=now)
        collector = await fetch_collector(config.source_id)
        counters: dict[str, int] = {
            "scanned_tasks": 0,
            "scanned_steps": 0,
            "parsed_ok": 0,
            "llm_confirmed": 0,
            "diverged": 0,
            "llm_fallback": 0,
            "unparseable": 0,
            "tickets_created": 0,
            "tickets_resolved": 0,
            "errors": 0,
            "llm_calls": 0,
        }
        seen_pairs: set[tuple[str, str]] = set()
        try:
            task_ids, task_max, step_max = await self._changed_ids(
                collector, config, wm
            )
            for task_id in task_ids:
                try:
                    await self._process_task(
                        collector, config, task_id, counters, seen_pairs
                    )
                except Exception as exc:  # noqa: BLE001 —— 单任务失败不阻断整轮
                    counters["errors"] += 1
                    logger.warning("dp_sync_task_failed task_id=%s error=%s", task_id, exc)
            await self._db.commit()
            # 收尾：水位推进 + 边确认（stale 机制）
            await self._dp_repo.update_watermark(
                "task", last_max_update=task_max, last_scan_at=now
            )
            await self._dp_repo.update_watermark(
                "step", last_max_update=step_max, last_scan_at=now
            )
            confirmed, restored = await self._lineage_repo.mark_seen(
                DP_PROVENANCE, seen_pairs
            )
            counters["tickets_resolved"] += restored
            await self._db.commit()
            detail = dict(counters)
            detail["seen_pairs"] = len(seen_pairs)
            await self._dp_repo.update_run_log(
                run.id,
                status="success",
                scanned_tasks=counters["scanned_tasks"],
                scanned_steps=counters["scanned_steps"],
                parsed_ok=counters["parsed_ok"],
                llm_confirmed=counters["llm_confirmed"],
                diverged=counters["diverged"],
                llm_fallback=counters["llm_fallback"],
                unparseable=counters["unparseable"],
                tickets_created=counters["tickets_created"],
                tickets_resolved=counters["tickets_resolved"],
                errors=counters["errors"],
                llm_calls=counters["llm_calls"],
                detail_json=__import__("json").dumps(detail, ensure_ascii=False),
            )
            await self._db.commit()
            return counters
        except Exception as exc:  # noqa: BLE001 —— 记录失败，下轮重试
            await self._db.rollback()
            await self._dp_repo.update_run_log(run.id, status="failed", error=str(exc))
            await self._db.commit()
            logger.exception("dp_sync_scan_failed")
            return {"skipped": "failed", "error": str(exc)}
        finally:
            await collector.dispose()

    async def _changed_ids(
        self,
        collector: Any,
        config: Any,
        wm: Any,
    ) -> tuple[list[int], datetime | None, datetime | None]:
        """按 gmt_modified 水位查变更任务 id 集合（task 变更 ∪ step 变更关联任务）。

        首轮无水位 = 全量（活跃 type 过滤任务）。返回 (ids, task_max, step_max)。
        """
        task_types = list(config.task_type_filter or [1])
        step_types = list(config.step_type_filter or [7])
        placeholders = ",".join(f":t{i}" for i in range(len(task_types)))
        params: dict[str, Any] = {f"t{i}": v for i, v in enumerate(task_types)}
        task_wm = wm.last_max_update if wm is not None else None
        base = (
            "SELECT id FROM dp_stable.dispatch_task "
            f"WHERE is_deleted=0 AND type IN ({placeholders})"
        )
        if task_wm is not None:
            params["twm"] = task_wm
            base += " AND gmt_modified > :twm"
        rows = await collector.query(base, params)
        ids = {int(r["id"]) for r in rows}
        # task 变更水位推进（增量模式下含本批最大；全量模式取全部最大）
        task_rows = await collector.query(
            "SELECT MAX(gmt_modified) AS m FROM dp_stable.dispatch_task "
            f"WHERE is_deleted=0 AND type IN ({placeholders})",
            params,
        )
        task_max = task_rows[0]["m"] if task_rows and task_rows[0]["m"] else task_wm
        # step 独立变更：按 step 水位补任务（跨表 join 保证 task type 过滤）
        step_wm = None
        swm_row = await self._dp_repo.get_watermark("step")
        if swm_row is not None:
            step_wm = swm_row.last_max_update
        sph = ",".join(f":s{i}" for i in range(len(step_types)))
        sp = {f"s{i}": v for i, v in enumerate(step_types)}
        step_sql = (
            "SELECT DISTINCT st.task_id AS id FROM dp_stable.dispatch_task_step st "
            "JOIN dp_stable.dispatch_task t ON st.task_id=t.id "
            f"WHERE st.is_deleted=0 AND t.is_deleted=0 AND t.type IN ({placeholders}) "
            f"AND st.task_step_type IN ({sph})"
        )
        sp.update({f"t{i}": v for i, v in enumerate(task_types)})
        if step_wm is not None:
            sp["swm"] = step_wm
            step_sql += " AND st.gmt_modified > :swm"
        step_rows = await collector.query(step_sql, sp)
        ids.update(int(r["id"]) for r in step_rows)
        # step 变更水位推进
        step_max_rows = await collector.query(
            "SELECT MAX(gmt_modified) AS m FROM dp_stable.dispatch_task_step "
            f"WHERE is_deleted=0 AND task_step_type IN ({sph})",
            {f"s{i}": v for i, v in enumerate(step_types)},
        )
        step_max = (
            step_max_rows[0]["m"] if step_max_rows and step_max_rows[0]["m"] else step_wm
        )
        return sorted(ids), task_max, step_max

    async def _process_task(
        self,
        collector: Any,
        config: Any,
        task_id: int,
        counters: dict[str, int],
        seen_pairs: set[tuple[str, str]],
    ) -> None:
        """处理单个任务：拉 task 静态字段 + SQL 节点 → process_step 逐节点。"""
        task = await self._fetch_task(collector, task_id)
        if task is None:
            return
        # 排除规则：任务名命中排除
        patterns = list(config.exclude_task_patterns or [])
        if patterns:
            import re as _re

            name = str(task.get("task_name") or "")
            if any(_re.search(p, name) for p in patterns):
                return
        steps = await self._fetch_sql_steps(collector, task_id, config)
        if not steps:
            return
        counters["scanned_tasks"] += 1
        for step in steps:
            sql = str(step.get("script_info") or "")
            counters["scanned_steps"] += 1
            result = await self.process_step(task, step, sql, config)
            status = result.get("status", "unknown")
            if status in counters:
                counters[status] += 1
            if status in ("parsed_ok", "llm_confirmed", "memory_reused"):
                # 记录确认边（供收尾 mark_seen）
                outcome_rows = await self._collect_seen_pairs(task, step, sql, config)
                seen_pairs.update(outcome_rows)
        # 资产 Owner 回填（产出表孤儿）
        await self.backfill_owner(task, config)

    async def _collect_seen_pairs(
        self, task: dict[str, Any], step: dict[str, Any], sql: str, config: Any
    ) -> set[tuple[str, str]]:
        """对本轮成功入库的节点重新解析出边集（供 mark_seen 确认）。"""
        outcome = parse_dp_step(
            sql,
            dialect="hive",
            exclude_patterns=config.exclude_table_patterns,
            rules=config.llm_complexity_rules,
            target_table=task.get("out_table") or None,
        )
        return {
            (node_table(e.source), node_table(e.target)) for e in outcome.table_edges
        }

    async def _fetch_task(self, collector: Any, task_id: int) -> dict[str, Any] | None:
        rows = await collector.query(
            "SELECT id AS task_id, task_no, name AS task_name, type, out_table, "
            "director, created_user_id, modified_user_id, checker, settle_project_director, "
            "project_id, settle_project_name, settle_department_name, budget_unit_name, "
            "cycle, cron_express, week_day, month_day, specific_time, frequence, remark, "
            "task_version_desc, task_version, master_task_id, is_master_task "
            "FROM dp_stable.dispatch_task WHERE id=:tid",
            {"tid": task_id},
        )
        return rows[0] if rows else None

    async def _fetch_sql_steps(
        self, collector: Any, task_id: int, config: Any
    ) -> list[dict[str, Any]]:
        step_types = list(config.step_type_filter or [7])
        placeholders = ",".join(f":s{i}" for i in range(len(step_types)))
        params: dict[str, Any] = {f"s{i}": v for i, v in enumerate(step_types)}
        params["tid"] = task_id
        return await collector.query(
            "SELECT id AS step_id, task_id, task_step, "
            "task_step_name AS step_name, task_step_type, task_node_type, script_info "
            "FROM dp_stable.dispatch_task_step "
            f"WHERE task_id=:tid AND is_deleted=0 AND task_step_type IN ({placeholders}) "
            "ORDER BY task_step",
            params,
        )
