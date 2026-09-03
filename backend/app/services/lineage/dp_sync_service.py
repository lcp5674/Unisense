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

import asyncio
import hashlib
import json
import logging
import re
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
from app.services.lineage.dp_sync_meta import merged_exclude_table_patterns
from app.services.lineage.dp_sync_parser import parse_dp_step
from app.services.lineage.dp_sync_repo import DpLineageRepository
from app.services.lineage.parser import node_table
from app.services.lineage.repository import LineageRepository

logger = logging.getLogger(__name__)


class _ScanCancelledError(Exception):
    """强制终止信号：置位 force_event 后在子步骤检查点抛出，中断本轮扫描。"""


#: 单次 step LLM 生成上限（确认/兜底均够用）。
_LLM_MAX_TOKENS = 2000

#: dp 通道 provenance 标记。
DP_PROVENANCE = "dp_sql"

#: 自动全量观察周期（秒）。稳态增量轮下任务/节点删除后其边永不进失效队列
#: （mark_missing 仅全量轮执行，P1-6 防误伤设计）——每满该周期自动执行一次
#: 全量扫描（忽略水位全量拉取 + mark_missing），闭合「删除语义」（M1）。
_AUTO_FULL_SCAN_SECONDS = 24 * 3600


def _in_clause(
    column: str, values: list[int], prefix: str, params: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """构造 `` AND col IN (:p0,:p1)`` 子句；values 为空（=全部）返回空子句。"""
    if not values:
        return "", params
    ph = ",".join(f":{prefix}{i}" for i in range(len(values)))
    for i, v in enumerate(values):
        params[f"{prefix}{i}"] = v
    return f" AND {column} IN ({ph})", params


def _type_filters(config: Any) -> tuple[list[int], list[int]]:
    """类型过滤取值：显式空列表 = 全部；未设置（None）= 默认 SQL 范围 [1]/[7]。"""
    task_types = (
        list(config.task_type_filter)
        if config.task_type_filter is not None
        else [1]
    )
    step_types = (
        list(config.step_type_filter)
        if config.step_type_filter is not None
        else [7]
    )
    return task_types, step_types



def sql_fingerprint(sql: str) -> str:
    """SQL 内容指纹（裁决记忆 key：同内容不重复进单）。"""
    return hashlib.sha256((sql or "").encode("utf-8")).hexdigest()


def _split_table_column(name: str) -> tuple[str | None, str | None]:
    """拆分 ``库.表.列`` 为 (库.表, 列)；无列时 (整名, None)。"""
    parts = name.rsplit(".", 1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return (name, None) if name else (None, None)


#: 标识符白名单（schema/表名只允许字母数字下划线——SQL 拼接防注入）。
_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _safe_table_name(value: str | None, field: str, default: str) -> str:
    """配置表名/库名标识符白名单校验：非法抛 ValueError（fail fast，运维可见）。

    配置值经 f-string 拼进 SQL，必须校验为合法标识符；非法时抛错由 scan_once
    外层捕获记 failed（原因可见），不做静默回退（避免扫错表）。
    """
    text = (value or "").strip() or default
    if not _IDENT_RE.match(text):
        raise ValueError(f"{field} 不是合法标识符（仅允许字母/数字/下划线）: {text!r}")
    return text


#: 手动血缘表名合法字符：库名.表名（点号分隔），仅字母/数字/下划线/点。
_TABLE_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)?$")


def _validate_manual_table(name: str) -> None:
    """手动裁决/手动边的表名格式校验：非法抛 ValueError（M3）。

    manual 边由用户手填，脏节点（空格/分号/超长/协议前缀）会污染血缘图且
    无回收路径；入库前 fail-fast 拒绝，提示合法形态（``库.表`` 或 ``表``）。
    """
    text = (name or "").strip()
    if not text:
        raise ValueError("手动血缘表名不能为空")
    if len(text) > 255:
        raise ValueError(f"手动血缘表名过长（>255）: {text[:30]!r}...")
    if not _TABLE_RE.match(text):
        raise ValueError(
            f"手动血缘表名不合法（仅允许 库.表 或 表，字符限字母/数字/下划线/点）: {text!r}"
        )


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
        seen_pairs: set[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """处理单个 SQL 节点，返回结果摘要（run_log detail 项）。

        seen_pairs: 可选——实际**入库**的边集合（node_table 化）；各写入路径
            写入后自行 add，供收尾 mark_seen/mark_missing 精确确认（不再靠
            事后重复 sqlglot 解析，P2-9 #10）。
        """
        sql_hash = sql_fingerprint(sql)
        outcome = parse_dp_step(
            sql,
            dialect="hive",
            exclude_patterns=merged_exclude_table_patterns(
                config.exclude_table_patterns
            ),
            rules=config.llm_complexity_rules,
            target_table=task.get("out_table") or None,
        )
        if outcome.status == "no_flow":
            # SQL 从有流转演化为无流转：清该 step 旧映射（保留本次 hash 无映射可写）
            await self._dp_repo.soft_delete_field_mappings(
                step_id=step.get("step_id"), keep_sql_hash=sql_hash
            )
            return {"step_id": step.get("step_id"), "status": "no_flow"}

        # SQL 演进清理（P2-8）：同 step 旧 sql_hash 的字段映射先软删（保留本次
        # hash——新映射随后写入），避免旧列映射永久残留致表膨胀/展示过时。
        await self._dp_repo.soft_delete_field_mappings(
            step_id=step.get("step_id"), keep_sql_hash=sql_hash
        )

        if outcome.status == "ok" and not outcome.is_complex:
            await self._store_sqlglot_edges(
                outcome, task, step, sql_hash, config, seen_pairs
            )
            return {"step_id": step.get("step_id"), "status": "parsed_ok"}

        # 复杂或失败：先查裁决记忆（同 step+hash 已裁决自动沿用）
        if config.resolve_memory_enabled:
            reused = await self._reuse_resolution(
                task, step, sql, sql_hash, outcome, config, seen_pairs
            )
            if reused:
                return reused

        if outcome.status == "ok":
            return await self._handle_complex(
                task, step, sql, sql_hash, outcome, config, seen_pairs
            )
        return await self._handle_failed(
            task, step, sql, sql_hash, outcome, config, seen_pairs
        )

    async def _handle_complex(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        sql: str,
        sql_hash: str,
        outcome: Any,
        config: Any,
        seen_pairs: set[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """复杂节点：LLM 共识确认（一致入库 / 分歧建单）；LLM 关闭建待抉择单。"""
        if not config.llm_enabled or self._llm_chat is None:
            await self._dp_repo.create_ticket(
                task_id=task.get("task_id"),
                step_id=step.get("step_id"),
                task_name=task.get("task_name"),
                out_table=task.get("out_table"),
                task_refs=build_task_ref(task, step),
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
                task_refs=build_task_ref(task, step),
                sql_text=sql,
                sql_hash=sql_hash,
                status="diverged",
                sqlglot_result=edges_to_json(outcome.table_edges, outcome.field_edges),
                divergence_reason=f"LLM 确认输出异常：{exc}",
            )
            return {"step_id": step.get("step_id"), "status": "diverged"}
        if verdict.agree:
            await self._store_sqlglot_edges(
                outcome, task, step, sql_hash, config, seen_pairs
            )
            return {"step_id": step.get("step_id"), "status": "llm_confirmed"}
        # 分歧：建待抉择单（附 sqlglot 结果 + LLM 意见 + 原因）
        await self._dp_repo.create_ticket(
            task_id=task.get("task_id"),
            step_id=step.get("step_id"),
            task_name=task.get("task_name"),
            out_table=task.get("out_table"),
            task_refs=build_task_ref(task, step),
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
        seen_pairs: set[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """失败节点：LLM 兜底提炼（llm_fallback / unparseable）；LLM 关闭建单。"""
        sqlglot_json = edges_to_json(outcome.table_edges, outcome.field_edges)
        if not config.llm_enabled or self._llm_chat is None:
            await self._dp_repo.create_ticket(
                task_id=task.get("task_id"),
                step_id=step.get("step_id"),
                task_name=task.get("task_name"),
                out_table=task.get("out_table"),
                task_refs=build_task_ref(task, step),
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
                task_refs=build_task_ref(task, step),
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
                task_refs=build_task_ref(task, step),
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
            task_refs=build_task_ref(task, step),
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
        seen_pairs: set[tuple[str, str]] | None = None,
    ) -> dict[str, Any] | None:
        ticket = await self._dp_repo.find_ticket_by_step_hash(
            step.get("step_id"), sql_hash
        )
        if ticket is None or ticket.status not in ("resolved", "ignored"):
            return None
        if ticket.resolution == "ignore" or ticket.status == "ignored":
            return {"step_id": step.get("step_id"), "status": "memory_ignored"}
        if ticket.resolution == "accept_sqlglot":
            await self._store_sqlglot_edges(
                outcome, task, step, sql_hash, config, seen_pairs
            )
            return {"step_id": step.get("step_id"), "status": "memory_reused"}
        if ticket.resolution == "accept_llm":
            await self._apply_llm_opinion(
                ticket.llm_opinion, task, step, sql_hash, seen_pairs
            )
            return {"step_id": step.get("step_id"), "status": "memory_reused"}
        if ticket.resolution == "manual":
            await self._apply_manual_edges(
                ticket.manual_edges_json, task, step, sql_hash, seen_pairs
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
        seen_pairs: set[tuple[str, str]] | None = None,
    ) -> None:
        """入库表级边 + dp_task_refs 合并 + 字段映射独立表（幂等聚合）。"""
        ref = build_task_ref(task, step)
        for te in outcome.table_edges:
            edge = await self._upsert_edge(te.source, te.target, task, step, ref)
            if edge is None:
                continue
            if seen_pairs is not None:
                seen_pairs.add((node_table(te.source), node_table(te.target)))
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
        seen_pairs: set[tuple[str, str]] | None = None,
    ) -> None:
        """采纳 LLM（分歧单）：意见补漏边入库（missing_edges 参考语义）。"""
        if not opinion:
            return
        ref = build_task_ref(task, step)
        for edge in opinion.get("missing_edges") or []:
            source = edge.get("source")
            target = edge.get("target")
            if source and target:
                written = await self._upsert_edge(source, target, task, step, ref)
                if written is not None and seen_pairs is not None:
                    seen_pairs.add((node_table(source), node_table(target)))

    async def _apply_manual_edges(
        self,
        manual: dict[str, Any] | None,
        task: dict[str, Any],
        step: dict[str, Any],
        sql_hash: str,
        seen_pairs: set[tuple[str, str]] | None = None,
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
            # M3: manual 表名格式校验（手填脏节点——含空格/分号/超长——会污染
            # 血缘图且无回收路径；对比 lineage.py 手动建边同样做格式/域校验）
            for tbl in (source, target):
                _validate_manual_table(tbl)
            edge = await self._upsert_edge(source, target, task, step, ref)
            if edge is None:
                continue
            if seen_pairs is not None:
                seen_pairs.add((node_table(source), node_table(target)))
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
    async def scan_once(
        self,
        fetch_collector: Callable[[str], Awaitable[Any]],
        *,
        progress: dict[str, Any] | None = None,
        cancel_event: asyncio.Event | None = None,
        force_event: asyncio.Event | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """执行一轮 dp 血缘增量扫描（由 arq 周期任务或手动「立即扫描」触发）。

        Args:
            fetch_collector: ``async (source_id) -> collector``（含 query/dispose）。
                由部署侧注入（tasks 用真实 dp 连接），测试注入假 collector。
            progress: 可选进度字典（手动扫描实时反馈）；就地更新 total/processed/
                current_task_id/stage。
            cancel_event: 可选取消事件；置位后在**当前 step 边界**停止（协作式：
                已处理结果保留、水位不推进，下轮从原水位重扫，幂等安全）。
            force_event: 可选强制终止事件（更强信号）；置位后在子步骤检查点
                立即抛出 ``_ScanCancelledError`` 中断本轮（不等当前任务剩余 steps），
                由调用方保证事务安全回滚。
            force: True 时绕过 enabled/轮询间隔检查（手动「立即扫描」用）；
                周期任务保持 False（按配置节流）。
        """
        config = await self._dp_repo.get_config()
        if config is None or (not config.enabled and not force):
            return {"skipped": "not_configured_or_disabled"}
        wm = await self._dp_repo.get_watermark("task")
        now = datetime.now(UTC)
        # 全量轮判定：无 task 水位 = 首轮/重置后全量；或距上次全量超周期自动全量
        # （M1：稳态增量轮任务删除永不 stale——每 _AUTO_FULL_SCAN_SECONDS 强制一次
        # 全量观察，使 mark_missing 对「不再出现的任务边」执行失效闭环）。仅全量轮
        # 对未再出现的边执行失效观察——增量轮只处理变更任务，未变更任务边不在
        # seen_pairs，若每轮 mark_missing 会误伤大量正常边（P1-6）。
        full_scan = wm is None or wm.last_max_update is None
        auto_full = (
            not full_scan
            and wm is not None
            and (
                wm.last_full_scan_at is None
                or (now - wm.last_full_scan_at).total_seconds()
                >= _AUTO_FULL_SCAN_SECONDS
            )
        )
        if auto_full:
            full_scan = True
        if not force and wm is not None and wm.last_scan_at is not None:
            interval = max(1, int(config.poll_interval_minutes or 5)) * 60
            if (now - wm.last_scan_at).total_seconds() < interval:
                return {"skipped": "interval_not_due"}
        if progress is not None:
            progress["stage"] = "collecting"
        run = await self._dp_repo.create_run_log(status="running", run_at=now)
        collector = None
        ingest_run = None
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
            # 连接获取与采集通道运行记录（begin）一并纳入 try：fetch 失败/中途异常
            # 统一走 except 记录 failed，不再让未绑定 collector 从 finally 二次抛错。
            collector = await fetch_collector(config.source_id)
            ingest_run = await self._lineage_repo.begin_ingest_run(DP_PROVENANCE)
            task_ids, task_max, step_max = await self._changed_ids(
                collector, config, wm, force_full=full_scan
            )
            if progress is not None:
                progress["total"] = len(task_ids)
                progress["processed"] = 0
                progress["current_task_id"] = None
                progress["stage"] = "parsing"
            cancelled = False
            for idx, task_id in enumerate(task_ids, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                if progress is not None:
                    progress["processed"] = idx - 1
                    progress["current_task_id"] = task_id
                try:
                    # per-task 事务隔离（savepoint）：单任务 DB 异常（唯一冲突/
                    # DataError/断连）只回滚自身 savepoint，不再让整轮单事务进入
                    # PendingRollbackError 导致后续任务全部失败、整轮 1000 任务
                    # 白做。任务成功释放 savepoint，收尾统一 commit。
                    async with self._db.begin_nested():
                        await self._process_task(
                            collector,
                            config,
                            task_id,
                            counters,
                            seen_pairs,
                            cancel_event=cancel_event,
                            force_event=force_event,
                        )
                except _ScanCancelledError:
                    # 强制终止：当前任务已随 savepoint 回滚（事务由 scan_once 收尾统一处理）
                    cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001 —— 单任务失败不阻断整轮
                    counters["errors"] += 1
                    logger.warning("dp_sync_task_failed task_id=%s error=%s", task_id, exc)
                if progress is not None:
                    progress["processed"] = idx
            await self._db.commit()
            # 收尾：水位 + 边确认（stale 机制）。取消/单任务失败时**不推进 max 水位**——
            # 未处理任务保留在变更集内，下轮从原水位重扫（幂等安全），避免跳过：
            # 失败任务 gmt_modified ≤ 全表 MAX，若照常推进会被新水位永久跳过（H1）。
            if cancelled or counters["errors"] > 0:
                await self._dp_repo.update_watermark("task", last_scan_at=now)
                await self._dp_repo.update_watermark("step", last_scan_at=now)
            else:
                # full_scan 成功时记录 last_full_scan_at（M1 周期全量判定的基准）
                await self._dp_repo.update_watermark(
                    "task",
                    last_max_update=task_max,
                    last_scan_at=now,
                    full_scan=full_scan,
                )
                await self._dp_repo.update_watermark(
                    "step",
                    last_max_update=step_max,
                    last_scan_at=now,
                    full_scan=full_scan,
                )
            confirmed, restored = await self._lineage_repo.mark_seen(
                DP_PROVENANCE, seen_pairs
            )
            counters["tickets_resolved"] += restored
            # 删除语义闭环（P1-6）：仅**全量轮**对未再出现的边执行失效观察——
            # mark_missing 累加 missing_count（threshold=2 观察期）后标 stale，
            # 任务/节点删除后其边保留历史但进入失效队列。增量轮跳过（防误伤）。
            missing = 0
            stale_flagged = 0
            if full_scan and not cancelled:
                missing, stale_flagged = await self._lineage_repo.mark_missing(
                    DP_PROVENANCE, seen_pairs, threshold=2
                )
            await self._db.commit()
            detail = dict(counters)
            detail["seen_pairs"] = len(seen_pairs)
            detail["missing"] = missing
            detail["stale_flagged"] = stale_flagged
            log_status = "cancelled" if cancelled else "success"
            await self._dp_repo.update_run_log(
                run.id,
                status=log_status,
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
                detail_json=json.dumps(detail, ensure_ascii=False),
            )
            # 双轨：同步写血缘采集通道运行摘要（ingest_run），与 dp_sync_run_log 并存
            # ——采集通道视图（lineage_ingest_run）获得 dp_sql 运行历史，失效治理同机制。
            if ingest_run is not None:
                await self._lineage_repo.finish_ingest_run(
                    ingest_run,
                    status=log_status,
                    total_edges=len(seen_pairs),
                    restored=restored,
                    missing=missing,
                    stale_flagged=stale_flagged,
                    detail=detail,
                )
            await self._db.commit()
            if progress is not None:
                progress["stage"] = "cancelled" if cancelled else "done"
            if cancelled:
                counters["skipped"] = "cancelled"
            return counters
        except Exception as exc:  # noqa: BLE001 —— 记录失败，下轮重试
            await self._db.rollback()
            try:
                # 失败必须可见：rollback 已撤销 run_log/ingest_run 的未提交行，
                # 直接 update 会 0 行静默失败 → 重建一条 failed 记录（双轨同写）。
                await self._dp_repo.create_run_log(
                    status="failed", error=str(exc), run_at=now
                )
                failed_run = await self._lineage_repo.begin_ingest_run(DP_PROVENANCE)
                await self._lineage_repo.finish_ingest_run(
                    failed_run, status="failed", error=str(exc)
                )
                await self._db.commit()
            except Exception:  # noqa: BLE001 —— 失败记录兜底，不影响错误上报
                await self._db.rollback()
            logger.exception("dp_sync_scan_failed")
            return {"skipped": "failed", "error": str(exc)}
        finally:
            if collector is not None:
                await collector.dispose()

    async def _changed_ids(
        self,
        collector: Any,
        config: Any,
        wm: Any,
        *,
        force_full: bool = False,
    ) -> tuple[list[int], datetime | None, datetime | None]:
        """按 gmt_modified 水位查变更任务 id 集合（task 变更 ∪ step 变更关联任务）。

        首轮无水位 = 全量（活跃 type 过滤任务）；force_full=True（周期自动全量，
        M1）时忽略既有水位同样全量拉取——使 mark_missing 能观察「不再出现的
        任务边」。返回 (ids, task_max, step_max)。
        表名取自配置（schema/task_table/step_table），标识符白名单校验（P1-5）。
        """
        schema, task_table, step_table = self._table_scope(config)
        task_types, step_types = _type_filters(config)
        params: dict[str, Any] = {}
        task_clause, params = _in_clause("type", task_types, "t", params)
        effective_wm = None if force_full else wm
        task_wm = effective_wm.last_max_update if effective_wm is not None else None
        # 变更集查询直接带 gmt_modified（M2：不再单独查全表 MAX——两查询分离窗口内
        # 新提交行 ts ≤ 全表 MAX 会被水位永久跳过；用「已扫集 max」推进则窗口内
        # 新行 ts > 已扫集 max，下轮必然命中）。
        base = (
            f"SELECT id, gmt_modified FROM {schema}.{task_table} "
            f"WHERE is_deleted=0{task_clause}"
        )
        if task_wm is not None:
            params["twm"] = task_wm
            base += " AND gmt_modified > :twm"
        rows = await collector.query(base, params)
        ids = {int(r["id"]) for r in rows}
        # task 变更水位推进 = 本轮已扫 task 变更集内最大 gmt_modified（无 task 变更
        # 则保持旧水位——不要因 step 变更引入任务而推进 task 水位，防超前漏扫）。
        task_changed_max = max(
            (r["gmt_modified"] for r in rows if r.get("gmt_modified") is not None),
            default=None,
        )
        task_max = (
            task_changed_max if task_changed_max is not None else task_wm
        )
        # step 独立变更：按 step 水位补任务（跨表 join 保证 task type 过滤）。
        # 变更集查询直接带 gmt_modified（M2 同 task：水位 = 已扫 step 集 max，
        # 不单独查全表 MAX——防两查询窗口内新提交 step 被水位跳过）。
        step_wm = None
        swm_row = await self._dp_repo.get_watermark("step")
        if swm_row is not None and not force_full:
            step_wm = swm_row.last_max_update
        sp: dict[str, Any] = {}
        task_join_clause, sp = _in_clause("t.type", task_types, "t", sp)
        step_clause, sp = _in_clause("st.task_step_type", step_types, "s", sp)
        step_sql = (
            f"SELECT st.task_id AS id, st.gmt_modified AS gm "
            f"FROM {schema}.{step_table} st "
            f"JOIN {schema}.{task_table} t ON st.task_id=t.id "
            f"WHERE st.is_deleted=0 AND t.is_deleted=0{task_join_clause}{step_clause}"
        )
        if step_wm is not None:
            sp["swm"] = step_wm
            step_sql += " AND st.gmt_modified > :swm"
        step_rows = await collector.query(step_sql, sp)
        ids.update(int(r["id"]) for r in step_rows)
        # step 变更水位推进 = 本轮已扫 step 变更集内最大 gmt_modified
        step_changed_max = max(
            (r["gm"] for r in step_rows if r.get("gm") is not None),
            default=None,
        )
        step_max = (
            step_changed_max if step_changed_max is not None else step_wm
        )
        return sorted(ids), task_max, step_max

    @staticmethod
    def _table_scope(config: Any) -> tuple[str, str, str]:
        """解析并校验扫描表名（schema/task_table/step_table，标识符白名单）。"""
        schema = _safe_table_name(config.schema_name, "schema_name", "dp_stable")
        task_table = _safe_table_name(
            config.task_table, "task_table", "dispatch_task"
        )
        step_table = _safe_table_name(
            config.step_table, "step_table", "dispatch_task_step"
        )
        return schema, task_table, step_table

    async def _process_task(
        self,
        collector: Any,
        config: Any,
        task_id: int,
        counters: dict[str, int],
        seen_pairs: set[tuple[str, str]],
        *,
        cancel_event: asyncio.Event | None = None,
        force_event: asyncio.Event | None = None,
    ) -> None:
        """处理单个任务：拉 task 静态字段 + SQL 节点 → process_step 逐节点。

        取消检查点下沉（A 方案）：fetch task 后 / 每个 step 前 / 资产回填前检查——
        协作取消（仅 cancel_event）在 step 边界停止，不再等完整任务跑完；
        强制终止（force_event 一并置位）在检查点直接抛 ``_ScanCancelled`` 中断。
        """
        task = await self._fetch_task(collector, task_id, config)
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
        # M8: llm_calls/tickets_created 计数——包装注入的 llm_chat 与 repo 建单
        # （此前恒 0，LLM 成本不可见）。service 实例每轮新建无并发共享，函数内
        # 替换 + finally 恢复保证异常安全。
        llm_orig = self._llm_chat
        create_orig = self._dp_repo.create_ticket
        counters["llm_calls"] = counters.get("llm_calls", 0)
        counters["tickets_created"] = counters.get("tickets_created", 0)

        async def _counted_llm(messages: Any, **kw: Any) -> dict[str, Any]:
            counters["llm_calls"] += 1
            return await llm_orig(messages, **kw)  # type: ignore[misc]

        async def _counted_create_ticket(**kw: Any) -> Any:
            counters["tickets_created"] += 1
            return await create_orig(**kw)

        if llm_orig is not None:
            self._llm_chat = _counted_llm  # type: ignore[method-assign]
        self._dp_repo.create_ticket = _counted_create_ticket  # type: ignore[method-assign]
        try:
            for step in steps:
                if cancel_event is not None and cancel_event.is_set():
                    if force_event is not None and force_event.is_set():
                        raise _ScanCancelledError(f"task {task_id} force-stop")
                    break  # 协作取消：本任务剩余 steps 不再处理（已处理 steps 保留）
                sql = str(step.get("script_info") or "")
                counters["scanned_steps"] += 1
                result = await self.process_step(task, step, sql, config, seen_pairs)
                status = result.get("status", "unknown")
                if status in counters:
                    counters[status] += 1
        finally:
            self._llm_chat = llm_orig
            self._dp_repo.create_ticket = create_orig
        # 资产 Owner 回填（产出表孤儿）
        if cancel_event is not None and cancel_event.is_set():
            if force_event is not None and force_event.is_set():
                raise _ScanCancelledError(f"task {task_id} force-stop at backfill")
            return  # 协作取消：跳过回填
        await self.backfill_owner(task, config)

    async def _fetch_task(
        self, collector: Any, task_id: int, config: Any
    ) -> dict[str, Any] | None:
        schema, task_table, _ = self._table_scope(config)
        rows = await collector.query(
            "SELECT id AS task_id, task_no, name AS task_name, type, out_table, "
            "director, created_user_id, modified_user_id, checker, settle_project_director, "
            "project_id, settle_project_name, settle_department_name, budget_unit_name, "
            "cycle, cron_express, week_day, month_day, specific_time, frequence, remark, "
            "task_version_desc, task_version, master_task_id, is_master_task "
            f"FROM {schema}.{task_table} WHERE id=:tid",
            {"tid": task_id},
        )
        return rows[0] if rows else None

    async def _fetch_sql_steps(
        self, collector: Any, task_id: int, config: Any
    ) -> list[dict[str, Any]]:
        schema, _, step_table = self._table_scope(config)
        _, step_types = _type_filters(config)
        params: dict[str, Any] = {"tid": task_id}
        step_clause, params = _in_clause(
            "task_step_type", step_types, "s", params
        )
        return await collector.query(
            "SELECT id AS step_id, task_id, task_step, "
            "task_step_name AS step_name, task_step_type, task_node_type, script_info "
            f"FROM {schema}.{step_table} "
            f"WHERE task_id=:tid AND is_deleted=0{step_clause} "
            "ORDER BY task_step",
            params,
        )

    # ---- 待抉择裁决（D9 人工抉择工作台） ----
    @staticmethod
    def _restore_task_step(ticket: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """从 ticket.task_refs_json 快照还原完整 task/step（无快照回退 id 三字段）。

        快照为 build_task_ref 产物：task 字段（task_id/task_no/task_name/out_table/
        director/cycle/…）+ step 字段（step_id/step_name/task_node_type/task_step）。
        """
        step_keys = {"step_id", "step_name", "task_node_type", "task_step"}
        ref = getattr(ticket, "task_refs_json", None)
        if isinstance(ref, dict) and ref:
            step = {k: v for k, v in ref.items() if k in step_keys}
            task = {k: v for k, v in ref.items() if k not in step_keys}
            step.setdefault("step_id", ticket.step_id)
            task.setdefault("task_id", ticket.task_id)
            task.setdefault("task_name", ticket.task_name)
            task.setdefault("out_table", ticket.out_table)
            return task, step
        return (
            {
                "task_id": ticket.task_id,
                "task_name": ticket.task_name,
                "out_table": ticket.out_table,
            },
            {"step_id": ticket.step_id},
        )

    async def resolve_ticket(
        self,
        ticket_id: int,
        *,
        resolution: str,
        resolved_by: int,
        manual_edges: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """裁决一张待抉择单并按所选结果入库（accept_sqlglot/accept_llm/manual/ignore）。

        已裁决单幂等（M3）：同 resolution 重复提交放行（repository resolve 覆盖
        留痕不重复写边）；**不同 resolution** 重复裁决拒绝——先 accept 后 ignore
        会造成「边已落库但裁决改 ignore」的永久背离且无撤销路径。
        """
        ticket = await self._dp_repo.get_ticket(ticket_id)
        if ticket is None:
            raise LookupError(f"待抉择单不存在: {ticket_id}")
        if ticket.resolution is not None:
            if ticket.resolution == resolution:
                # 同 resolution 重复提交幂等：边已按上次裁决写入，直接返回现状
                return {"ticket_id": ticket_id, "resolution": resolution}
            raise ValueError(
                f"该单已裁决为 {ticket.resolution}，如需改判请先在运维侧处理"
                f"（防 accept 落边后改 ignore 造成血缘事实背离）"
            )
        # 用建单时快照还原完整 task/step（含 director/cycle 等准静态元数据），
        # 使 build_task_ref 产出完整 dp_task_refs——此前只用 id/name/out_table，
        # 责任人快照在裁决入库时丢失（P2-9 #12）。
        task, step = self._restore_task_step(ticket)
        sql_hash = ticket.sql_hash
        if resolution == "accept_sqlglot":
            await self._apply_json_edges(
                ticket.sqlglot_result, task, step, sql_hash,
                provenance="sqlglot", confidence=1.0,
            )
        elif resolution == "accept_llm":
            await self._apply_llm_resolution(ticket, task, step, sql_hash)
        elif resolution == "manual":
            payload = manual_edges if manual_edges is not None else ticket.manual_edges_json
            await self._apply_manual_edges(payload, task, step, sql_hash)
        elif resolution != "ignore":
            raise ValueError(f"未知裁决方式: {resolution}")
        await self._dp_repo.resolve_ticket(
            ticket_id,
            resolution=resolution,
            resolved_by=resolved_by,
            manual_edges=manual_edges,
        )
        return {"ticket_id": ticket_id, "resolution": resolution}

    async def _apply_llm_resolution(
        self, ticket: Any, task: dict[str, Any], step: dict[str, Any], sql_hash: str
    ) -> None:
        """采纳 LLM：按单类型应用意见（diverged=sqlglot 边+补漏；llm_fallback=兜底流转）。"""
        if ticket.status == "llm_fallback":
            await self._apply_fallback_flow(ticket.llm_opinion, task, step, sql_hash)
            return
        # diverged：LLM 判定 sqlglot 部分边为错误（wrong_edges）——先剔除再入库，
        # 再补 LLM 认为漏掉的边（missing_edges）。此前 wrong_edges 是死字段，
        # 「采纳 LLM」与「采纳 sqlglot」实际等价，错误边从未被剔除（P1-4）。
        sqlglot_json = self._without_wrong_edges(
            ticket.sqlglot_result, ticket.llm_opinion
        )
        await self._apply_json_edges(
            sqlglot_json, task, step, sql_hash,
            provenance="sqlglot", confidence=1.0,
        )
        await self._apply_llm_opinion(ticket.llm_opinion, task, step, sql_hash)

    @staticmethod
    def _without_wrong_edges(
        edges_json: dict[str, Any] | None, opinion: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """从 sqlglot 结果中剔除 LLM 判定错误的表边及其字段映射（深拷贝，不污染原单）。

        wrong_edges 语义：LLM 认为 sqlglot 声称的 ``source->target`` 流转不成立
        （来源误判/目标表错/实为过滤非流转）。采纳 LLM 即不采纳这些边。
        """
        if not edges_json:
            return edges_json
        wrong = {
            (str(w.get("source")), str(w.get("target")))
            for w in (opinion or {}).get("wrong_edges") or []
            if w.get("source") and w.get("target")
        }
        if not wrong:
            return edges_json
        cleaned = json.loads(json.dumps(edges_json, ensure_ascii=False))
        cleaned["table_edges"] = [
            te
            for te in cleaned.get("table_edges") or []
            if (te.get("source"), te.get("target")) not in wrong
        ]
        cleaned["field_edges"] = [
            fe
            for fe in cleaned.get("field_edges") or []
            if (fe.get("source_table"), fe.get("target_table")) not in wrong
        ]
        return cleaned

    async def _apply_json_edges(
        self,
        edges_json: dict[str, Any] | None,
        task: dict[str, Any],
        step: dict[str, Any],
        sql_hash: str,
        *,
        provenance: str,
        confidence: float,
        seen_pairs: set[tuple[str, str]] | None = None,
    ) -> None:
        """从 ticket.sqlglot_result JSON 写边（table_edges + field_edges）。"""
        if not edges_json:
            return
        ref = build_task_ref(task, step)
        for te in edges_json.get("table_edges") or []:
            source = te.get("source")
            target = te.get("target")
            if not (source and target):
                continue
            edge = await self._upsert_edge(source, target, task, step, ref)
            if edge is None:
                continue
            if seen_pairs is not None:
                seen_pairs.add((node_table(source), node_table(target)))
            for fe in edges_json.get("field_edges") or []:
                if fe.get("target_table") == target and fe.get("source_table") == source:
                    await self._dp_repo.upsert_field_mapping(
                        edge_id=edge.id,
                        source_table=source,
                        source_column=fe.get("source_column"),
                        target_table=target,
                        target_column=fe.get("target_column"),
                        expression=fe.get("expression"),
                        degraded=bool(fe.get("degraded")),
                        confidence=confidence,
                        provenance=provenance,
                        sql_hash=sql_hash,
                        task_id=task.get("task_id"),
                        step_id=step.get("step_id"),
                    )

    async def _apply_fallback_flow(
        self,
        opinion: dict[str, Any] | None,
        task: dict[str, Any],
        step: dict[str, Any],
        sql_hash: str,
        seen_pairs: set[tuple[str, str]] | None = None,
    ) -> None:
        """采纳 LLM 兜底流转：field_mappings 明确的写字段边；无映射时源集→目标表边。"""
        if not opinion:
            return
        ref = build_task_ref(task, step)
        written: set[tuple[str, str]] = set()
        for pair in opinion.get("field_mappings") or []:
            if len(pair) < 2:
                continue
            source_t, source_c = _split_table_column(str(pair[0]))
            target_t, target_c = _split_table_column(str(pair[1]))
            if not (source_t and target_t):
                continue
            key = (source_t, target_t)
            if key not in written:
                edge = await self._upsert_edge(source_t, target_t, task, step, ref)
                written.add(key)
                if edge is None:
                    continue
                if seen_pairs is not None:
                    seen_pairs.add((node_table(source_t), node_table(target_t)))
            else:
                edge = None
            if edge is not None and source_c and target_c:
                await self._dp_repo.upsert_field_mapping(
                    edge_id=edge.id,
                    source_table=source_t,
                    source_column=source_c,
                    target_table=target_t,
                    target_column=target_c,
                    expression=None,
                    degraded=False,
                    confidence=0.5,
                    provenance="llm",
                    sql_hash=sql_hash,
                    task_id=task.get("task_id"),
                    step_id=step.get("step_id"),
                )
        # 无字段映射时：源表集合 → 每个目标表建表级边
        sources = [s for s in opinion.get("source_tables") or [] if s]
        targets = [t for t in opinion.get("target_tables") or [] if t]
        for target in targets:
            for source in sources:
                if (source, target) not in written:
                    edge = await self._upsert_edge(source, target, task, step, ref)
                    written.add((source, target))
                    if edge is not None and seen_pairs is not None:
                        seen_pairs.add((node_table(source), node_table(target)))
