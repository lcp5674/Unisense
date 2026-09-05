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
from dataclasses import dataclass
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
from app.services.lineage.dp_sync_meta import DP_STEP_TYPES, merged_exclude_table_patterns
from app.services.lineage.dp_sync_parser import parse_dp_step
from app.services.lineage.dp_sync_repo import DpLineageRepository
from app.services.lineage.dp_sync_typed import parse_dp_step_typed
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

#: 明细批量预取批大小（阶段 1）：逐任务拉取 task+step 明细 = 每任务 2 次源库往返，
#: 一轮 N 任务 ≈ 2N 次小查询、长占源库连接几十分钟。改为按批预取——每批任务
#: 静态字段 + step 明细各 1~2 次批量 ``IN`` 查询载入内存（SQL 全文本地解析），
#: 源库往返从 2N 降到 ~N/100，占用从分钟级降到秒级。
_PREFETCH_BATCH_SIZE = 200
#: 单条 ``IN`` 子句最大元素数（超过分块并发查询，避免 SQL 过长/参数上限）。
_IN_CHUNK = 400
#: LLM 裁决并发度（方案 A）：需 LLM 的 step 攒批后以该并发度同时调用——
#: 单轮 288 次 LLM × ~1.5s 串行 ≈ 7 分钟，Semaphore(4) 下降到 ~1/4。
#: LLM 客户端每次调用独立建连（无共享可变状态），并发安全；db 写仍在
#: 批后逐任务串行（共享 AsyncSession 不可并发）。
_LLM_CONCURRENCY = 4


@dataclass
class _LlmWork:
    """延迟到批级并发执行的 LLM 裁决工作项（复杂确认 / 失败兜底）。

    scan_once 预取批内，需 LLM 的 step 先打包为工作项攒批（不现场调 LLM——
    现场调用 = 每任务串行 await，单轮数百次 LLM 全串行）。批末统一
    Semaphore 并发裁决后回填 ``result``/``error``，再按任务归集应用写库。
    """

    kind: str  # "confirm"=复杂节点共识确认 / "fallback"=失败节点兜底提炼
    task: dict[str, Any]
    step: dict[str, Any]
    sql: str
    sql_hash: str
    outcome: Any
    result: Any = None  # ConfirmVerdict / FallbackFlow（_resolve_llm_works 后回填）
    error: str | None = None  # LLM 输出异常（phase2 转建单，不静默丢失）


def _chunks(items: list[int], size: int) -> list[list[int]]:
    """把 id 列表切成不超过 ``size`` 的块（批量 IN 查询分块用）。"""
    return [items[i : i + size] for i in range(0, len(items), size)]


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


#: dp ``dispatch_task`` 行 SELECT 基线列映射（源列, 别名）——**基线 = 生产真实 DDL
#: 观测列**（2026-09 dp 元库 dispatch_task，用户提供的表结构）。这些列在目标环境
#: 必然存在，SELECT 永不报 ``Unknown column``（不依赖运行时探测也安全）。
#: ``_TASK_ENHANCED_COLS`` 为部分环境才有的增强列（结算/主任务语义）——仅当
#: information_schema 探测确认存在才追加 SELECT，缺失/探测失败一律不带。
_TASK_SELECT_COLS: tuple[tuple[str, str], ...] = (
    ("id", "task_id"),
    ("task_no", "task_no"),
    ("name", "task_name"),
    ("type", "type"),
    ("out_table", "out_table"),
    ("director", "director"),
    ("created_user_id", "created_user_id"),
    ("modified_user_id", "modified_user_id"),
    ("checker", "checker"),
    ("project_id", "project_id"),
    ("cycle", "cycle"),
    ("cron_express", "cron_express"),
    ("week_day", "week_day"),
    ("month_day", "month_day"),
    ("specific_time", "specific_time"),
    ("frequence", "frequence"),
    ("remark", "remark"),
    ("task_version_desc", "task_version_desc"),
    ("task_version", "task_version"),
    # gmt_modified：供 F3 预取快照新鲜度校验（task 行变更时间 > 本轮变更集水位
    # 时跳过留待下轮，防批量预取放大「查询→处理」竞态窗口导致旧快照覆盖）。
    # 该列真实 DDL 必然存在（_changed_ids 增量查询依赖它）；build_task_ref 的
    # task_fields 不含它 → 不落入 dp_task_refs 快照，零副作用。
    ("gmt_modified", "gmt_modified"),
)

#: 可选增强列（生产精简表无；含结算/主任务语义的环境由探测动态补上）。
_TASK_ENHANCED_COLS: tuple[tuple[str, str], ...] = (
    ("settle_project_director", "settle_project_director"),
    ("settle_project_name", "settle_project_name"),
    ("settle_department_name", "settle_department_name"),
    ("budget_unit_name", "budget_unit_name"),
    ("master_task_id", "master_task_id"),
    ("is_master_task", "is_master_task"),
)

#: dp ``dispatch_task_step`` 行 SELECT 基线列映射——基线 = 生产真实 DDL 观测列
#: （2026-09 dispatch_task_step：id/task_id/task_step/task_step_name/
#: task_step_type/script_info）；``task_node_type`` 等为假设增强列，仅探测确认
#: 存在才带。is_deleted 只在 WHERE 用（真实 DDL 有；缺失时探测后省略）。
_STEP_SELECT_COLS: tuple[tuple[str, str], ...] = (
    ("id", "step_id"),
    ("task_id", "task_id"),
    ("task_step", "task_step"),
    ("task_step_name", "step_name"),
    ("task_step_type", "task_step_type"),
    ("script_info", "script_info"),
    # gmt_modified：供 N2 预取 step 快照新鲜度校验（step 行变更时间 > 本轮变更集
    # 水位时跳过留待下轮，防批量预取放大「查询→处理」竞态窗口致 SQL 脚本变更
    # 静默延迟到 24h 自动全量——F3 只校验了 task 行，漏了最常见的变更源）。
    # 该列真实 DDL 必然存在（_changed_ids 的 step 增量查询依赖它）；build_task_ref
    # 的 step_fields 不含它 → 不落入 dp_task_refs 快照，零副作用。
    ("gmt_modified", "gmt_modified"),
)

_STEP_ENHANCED_COLS: tuple[tuple[str, str], ...] = (
    ("task_node_type", "task_node_type"),
)


def _utc_aware(dt: datetime | None) -> datetime | None:
    """MySQL DATETIME 读出为 naive，统一按 UTC 补时区（与全仓 UTC 落库一致）。

    避免 ``datetime.now(UTC) - naive`` 抛 offset-naive/aware TypeError
    （周期轮询在 last_scan_at/last_full_scan_at 比较处曾每分钟崩溃）。
    """
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=UTC)


def _is_stale_snapshot(gmt: Any, max_dt: datetime | None) -> bool:
    """预取快照新鲜度判定：源行变更时间 ``gmt`` > 本轮变更集水位 ``max_dt``。

    F3/N2：批量预取把「变更集查询 → 处理」窗口从逐任务的秒级拉长到整批分钟级——
    任务/step 在窗口内被更新时，预取拿到的行可能不属于本轮已确认的变更集（其
    gmt > 本轮水位）。本轮跳过留待下轮增量命中（水位推进后 ``gmt > 水位`` 的新
    变更由下轮专门处理），防「本轮处理越界行 + 下轮重扫」的重复与快照撕裂。
    """
    if max_dt is None or not isinstance(gmt, datetime):
        return False
    gmt_aware = _utc_aware(gmt)
    max_aware = _utc_aware(max_dt)
    return gmt_aware is not None and max_aware is not None and gmt_aware > max_aware


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
        # 方案 3：schema 感知 star 展开的提供者（scan_once 内创建并绑定 dp
        # collector；实例每轮新建，故实例级缓存安全）。
        self._schema_provider: Any | None = None
        # dp 元表（dispatch_task/dispatch_task_step）真实列探测缓存——不同环境
        # 表结构可能不同，fetch 前按 (schema, table) 探测一次、轮内复用。
        self._dp_table_columns: dict[tuple[str, str], set[str] | None] = {}

    # ---- 单 step 处理 ----
    @staticmethod
    async def _parse_typed(
        sql: str,
        *,
        step_type: int,
        config: Any,
        target_table: str | None,
        schema_columns: dict[str, list[str]] | None = None,
    ) -> Any:
        """同步 sqlglot 解析 offload 线程池（阶段 1：解除事件循环阻塞）。

        ``parse_dp_step_typed`` 是纯 CPU 同步函数；手动「立即扫描」跑在 backend
        uvicorn worker 的事件循环上，直接调用会阻塞同 worker 其它 API 请求（arq
        worker 亦然）。``asyncio.to_thread`` 丢默认线程池执行——解析期间事件循环
        保持响应（长 SQL/复杂 CTE 解析毫秒~百毫秒级，不再饿死心跳/请求）。
        """
        return await asyncio.to_thread(
            parse_dp_step_typed,
            sql,
            step_type=step_type,
            dialect="hive",
            exclude_patterns=merged_exclude_table_patterns(
                config.exclude_table_patterns
            ),
            rules=config.llm_complexity_rules,
            target_table=target_table,
            schema_columns=schema_columns,
        )

    async def _purge_step_old_mappings(
        self, step: dict[str, Any], sql_hash: str
    ) -> None:
        """清该 step 旧 sql_hash 的字段映射（保留本次 hash），供各写入/建单点调用。

        F4：SQL 演进清理（P2-8）下沉到「先清后写/先清后建单」的最终动作——保证
        「清旧」与「写新/留痕」落在同一事务。调用方各写入路径在真正落库/建单前
        调用一次（幂等：同 step+hash 重复清理无副作用），defer 的复杂/失败 step
        不再在 phase1 提前删（见 process_step 注释）。
        """
        await self._dp_repo.soft_delete_field_mappings(
            step_id=step.get("step_id"), keep_sql_hash=sql_hash
        )

    async def _touch_seen(self, seen_pairs: set[tuple[str, str]] | None) -> None:
        """把扫描轮外本次写入的边对置为「已见」（last_seen_at=now）。

        N4：resolve_ticket / reprocess / retry 等入口没有扫描轮 seen_pairs——
        写库的边若不刷新 last_seen_at，mark_missing 会跳过（永 NULL 不进入失效
        观察），任务删除后永不 stale（删除语义不闭合）。本 helper 供这些入口在
        写边后调用（同事务，随调用方 commit 落库）。
        """
        if seen_pairs:
            await self._lineage_repo.touch_edges_seen(seen_pairs)

    async def process_step(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        sql: str,
        config: Any,
        seen_pairs: set[tuple[str, str]] | None = None,
        *,
        defer_llm: bool = False,
    ) -> dict[str, Any] | _LlmWork:
        """处理单个 SQL 节点，返回结果摘要（run_log detail 项）。

        seen_pairs: 可选——实际**入库**的边集合（node_table 化）；各写入路径
            写入后自行 add，供收尾 mark_seen/mark_missing 精确确认（不再靠
            事后重复 sqlglot 解析，P2-9 #10）。
        defer_llm: True 时复杂/失败节点不现场调 LLM——LLM 开启则返回
            ``_LlmWork`` 工作项（由 scan_once 批级并发裁决后 phase2 应用），
            关闭则即时建待抉择单（与不 defer 的关闭行为一致）。
        """
        sql_hash = sql_fingerprint(sql)
        outcome = await self._parse_typed(
            sql,
            step_type=int(step.get("task_step_type") or 7),
            config=config,
            target_table=task.get("out_table") or None,
        )
        if outcome.status == "no_flow":
            # SQL 从有流转演化为无流转：清该 step 旧映射（保留本次 hash 无映射可写）
            await self._dp_repo.soft_delete_field_mappings(
                step_id=step.get("step_id"), keep_sql_hash=sql_hash
            )
            return {"step_id": step.get("step_id"), "status": "no_flow"}

        # 方案 3（schema 感知 star 展开）：首轮解析已给出表级边——拉取各源表列
        # 清单（两级通道尽力获取，per-run 缓存）后二次解析，把 ``SELECT *`` 展开为
        # 逐列真实字段边（穿透 CTE/子查询/JOIN/UNION）；无 schema 的表保持降级
        # 标记（语义与方案 3 之前一致）。
        if outcome.table_edges and self._schema_provider is not None:
            try:
                schema_map = await self._schema_provider.as_map(
                    [te.source for te in outcome.table_edges]
                )
            except Exception as exc:  # noqa: BLE001 —— schema 获取失败即按无 schema 解析
                logger.warning("dp_schema_map_failed step=%s error=%s", step.get("step_id"), exc)
                schema_map = {}
            if schema_map:
                # D7：二轮（schema 显式展开）解析套 try——star 展开路径对异常 SQL
                # 无最外层兜底时若裸调抛错，会上抛至任务级 except 回滚首轮可用
                # outcome 且记 failed（稳定触发则每轮重扫每轮失败）。失败沿用首轮。
                try:
                    second = await self._parse_typed(
                        sql,
                        step_type=int(step.get("task_step_type") or 7),
                        config=config,
                        target_table=task.get("out_table") or None,
                        schema_columns=schema_map,
                    )
                except Exception as exc:  # noqa: BLE001 —— 二轮失败沿用首轮 outcome
                    logger.warning(
                        "dp_schema_second_parse_failed step=%s error=%s",
                        step.get("step_id"),
                        exc,
                    )
                else:
                    # N1：二轮返回 failed **状态**（非抛异常）同样沿用首轮。parse_dp_step
                    # 对语句失败是整体返回 status="failed" 而非上抛——D7 只兜了抛异常
                    # 分支，failed 态曾被无条件采纳（outcome=second），把首轮已产出的
                    # 表级边整体丢弃并降级走 llm_fallback 低置信参考单。仅采纳二轮
                    # ok / no_flow 结果。
                    if second.status == "failed":
                        logger.warning(
                            "dp_schema_second_failed_keep_first step=%s",
                            step.get("step_id"),
                        )
                    else:
                        outcome = second
                        if outcome.status == "no_flow":
                            await self._dp_repo.soft_delete_field_mappings(
                                step_id=step.get("step_id"), keep_sql_hash=sql_hash
                            )
                            return {"step_id": step.get("step_id"), "status": "no_flow"}

        # F4（旧映射清理下沉）：本 step 的旧 sql_hash 字段映射清理不再在此无条件
        # 提前执行——若本 step 将 defer（复杂/失败 + LLM 开启，打包 _LlmWork 到
        # phase2 裁决），phase1 先删旧映射而 phase2 因取消/异常未执行，会造成
        # 「旧映射已删、新映射未写」的断链且无留痕。改为在**每个最终写入/建单点**
        # 先清后写（同事务原子）：_store_sqlglot_edges 开头 / _reuse_resolution
        # 命中后 / _handle_complex / _handle_failed / _apply_confirm_verdict /
        # _apply_fallback_flow_verdict / _finish_deferred_task（见各自实现）。
        # no_flow 分支（无 defer）保持原位清理，见上两处。

        if outcome.status == "ok" and not outcome.is_complex:
            written, degraded_cnt = await self._store_sqlglot_edges(
                outcome, task, step, sql_hash, config, seen_pairs
            )
            return {
                "step_id": step.get("step_id"),
                "status": "parsed_ok",
                "fields_written": written,
                "fields_degraded": degraded_cnt,
            }

        # 复杂或失败：先查裁决记忆 / 未裁决票（G3：合并为一次票查询三态判定——
        # 此前 _reuse_resolution 与 _skip_pending_llm 各查一次 find_ticket_by_step_hash，
        # 每复杂/失败 step 每次重扫 2 次票查询；查一次按态分流：
        #   已裁决（status ∈ resolved/ignored）且记忆开关开 → 复用；
        #   未裁决（resolution IS NULL，diverged/unparseable/llm_fallback 待人工）
        #     → 跳过不重复调 LLM（独立于记忆开关）；
        #   无票 → 走 LLM confirm/fallback）。
        ticket = await self._dp_repo.find_ticket_by_step_hash(
            step.get("step_id"), sql_hash  # type: ignore[arg-type]
        )
        if (
            config.resolve_memory_enabled
            and ticket is not None
            and ticket.status in ("resolved", "ignored")
        ):
            reused = await self._reuse_resolution(
                task, step, sql, sql_hash, outcome, config, seen_pairs,
                ticket=ticket,
            )
            if reused:
                return reused

        # 未裁决票跳过：同 step+sql_hash 已存在未裁决待抉择单（resolution IS NULL）
        # 时不再重复调 LLM——每轮重扫的 LLM 意见被 create_ticket 幂等丢弃（已存在
        # 单不更新），纯烧成本；人工裁决（resolve/ignore）前重复确认无意义。已裁决
        # 票（resolution 非空）由上方记忆复用处理（记忆开关关时此处放行走 LLM，与
        # 原行为一致）；无票则正常走 LLM。
        skip = await self._skip_pending_llm(step, sql_hash, ticket=ticket)
        if skip is not None:
            return skip

        if outcome.status == "ok":
            if defer_llm:
                return await self._defer_complex(
                    task, step, sql, sql_hash, outcome, config, seen_pairs
                )
            return await self._handle_complex(
                task, step, sql, sql_hash, outcome, config, seen_pairs
            )
        if defer_llm:
            return await self._defer_failed(
                task, step, sql, sql_hash, outcome, config, seen_pairs
            )
        return await self._handle_failed(
            task, step, sql, sql_hash, outcome, config, seen_pairs
        )

    async def _defer_complex(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        sql: str,
        sql_hash: str,
        outcome: Any,
        config: Any,
        seen_pairs: set[tuple[str, str]] | None = None,
    ) -> dict[str, Any] | _LlmWork:
        """复杂节点延迟分支：LLM 开启 → 打包 ``_LlmWork``（批级并发裁决）；
        LLM 关闭 → 即时建待抉择单（与 ``_handle_complex`` 关闭分支一致）。"""
        if not config.llm_enabled or self._llm_chat is None:
            return await self._handle_complex(
                task, step, sql, sql_hash, outcome, config, seen_pairs
            )
        return _LlmWork(
            kind="confirm",
            task=task,
            step=step,
            sql=sql,
            sql_hash=sql_hash,
            outcome=outcome,
        )

    async def _defer_failed(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        sql: str,
        sql_hash: str,
        outcome: Any,
        config: Any,
        seen_pairs: set[tuple[str, str]] | None = None,
    ) -> dict[str, Any] | _LlmWork:
        """失败节点延迟分支：LLM 开启 → 打包 ``_LlmWork``；关闭 → 即时建单。"""
        if not config.llm_enabled or self._llm_chat is None:
            return await self._handle_failed(
                task, step, sql, sql_hash, outcome, config, seen_pairs
            )
        return _LlmWork(
            kind="fallback",
            task=task,
            step=step,
            sql=sql,
            sql_hash=sql_hash,
            outcome=outcome,
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
            # F4：建单前清旧 hash 映射（先清后留痕，与建单同事务——SQL 已演进的
            # step 不残留旧列映射；defer 场景由 phase2 在此统一清，不再提前删）
            await self._purge_step_old_mappings(step, sql_hash)
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
            await self._purge_step_old_mappings(step, sql_hash)
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
        return await self._apply_confirm_verdict(
            task, step, sql, sql_hash, outcome, config, seen_pairs, verdict
        )

    async def _apply_confirm_verdict(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        sql: str,
        sql_hash: str,
        outcome: Any,
        config: Any,
        seen_pairs: set[tuple[str, str]] | None,
        verdict: Any,
    ) -> dict[str, Any]:
        """LLM 确认意见应用段（agree→sqlglot 结果入库 / 分歧→建待抉择单）。

        从 ``_handle_complex`` 抽出供批级并发路径复用：并发裁决拿到 verdict 后
        按任务归集，由本函数落库（不入库静默失败，分歧必建单留痕）。
        """
        if verdict.agree:
            # F4：旧映射清理在 _store_sqlglot_edges 开头执行（先清后写同事务）。
            # F6：接住 _store 返回的字段映射写入/降级统计——此前丢弃致 LLM 确认
            # 路径的 field_mappings_written 被系统性计 0（可观测性缺陷）。
            written, degraded = await self._store_sqlglot_edges(
                outcome, task, step, sql_hash, config, seen_pairs
            )
            # G1：agree 落自动消解记忆（status=resolved + accept_sqlglot）——
            # 当场入库的复杂 step 若无记忆，下轮重扫/24h 全量轮会再次调 LLM
            # confirm（幂等入库丢弃意见，纯烧成本）；落票后 _reuse_resolution
            # 命中，行为与人工 accept_sqlglot / retry_llm_tickets 自动消解对齐。
            # repo 幂等：同 step+hash 已有票（待裁决/已裁决）不重复建不覆盖。
            # （task_id/step_id 的 arg-type ignore：task dict 为 Any，实际恒 int，
            #   与文件内 create_ticket 调用点同类。）
            await self._dp_repo.record_auto_accept_memory(
                task_id=task.get("task_id"),  # type: ignore[arg-type]
                step_id=step.get("step_id"),  # type: ignore[arg-type]
                task_name=task.get("task_name"),
                out_table=task.get("out_table"),
                task_refs=build_task_ref(task, step),
                sql_text=sql,
                sql_hash=sql_hash,
                sqlglot_result=edges_to_json(
                    outcome.table_edges, outcome.field_edges
                ),
            )
            return {
                "step_id": step.get("step_id"),
                "status": "llm_confirmed",
                "fields_written": written,
                "fields_degraded": degraded,
            }
        # 分歧：建待抉择单（附 sqlglot 结果 + LLM 意见 + 原因）
        await self._purge_step_old_mappings(step, sql_hash)
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
            # F4：建单前清旧 hash 映射（先清后留痕，同事务）
            await self._purge_step_old_mappings(step, sql_hash)
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
            await self._purge_step_old_mappings(step, sql_hash)
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
        return await self._apply_fallback_flow_verdict(
            task, step, sql, sql_hash, sqlglot_json, flow
        )

    async def _apply_fallback_flow_verdict(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        sql: str,
        sql_hash: str,
        sqlglot_json: Any,
        flow: Any,
    ) -> dict[str, Any]:
        """LLM 兜底流应用段（提炼成功→建低置信参考单 / 失败→unparseable）。

        从 ``_handle_failed`` 抽出供批级并发路径复用（flow 由并发裁决回填）。
        命名带 ``_verdict`` 后缀与 resolve 区既有的 ``_apply_fallback_flow``
        （重试单刷新低置信参考）区分，避免类内覆盖。
        """
        # F4：先清旧 hash 映射再建单（覆盖 flow.ok 参考单与 flow.fail unparseable
        # 两分支；defer 场景由 phase2 经本函数统一清，不再 phase1 提前删）
        await self._purge_step_old_mappings(step, sql_hash)
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
        *,
        ticket: Any | None = None,
    ) -> dict[str, Any] | None:
        if ticket is None:
            ticket = await self._dp_repo.find_ticket_by_step_hash(
                step.get("step_id"), sql_hash  # type: ignore[arg-type]
            )
        if ticket is None or ticket.status not in ("resolved", "ignored"):
            return None
        # F4：裁决记忆命中即本 step 将以记忆写入/忽略收尾（不再 defer）——统一先清
        # 旧 hash 映射再写（accept_sqlglot 经 _store 开头清、accept_llm/manual/
        # ignored 走此处）；phase1 无条件提前删已移除，故必须在此补齐。
        await self._purge_step_old_mappings(step, sql_hash)
        if ticket.resolution == "ignore" or ticket.status == "ignored":
            return {"step_id": step.get("step_id"), "status": "memory_ignored"}
        if ticket.resolution == "accept_sqlglot":
            written, degraded = await self._store_sqlglot_edges(
                outcome, task, step, sql_hash, config, seen_pairs
            )
            return {
                "step_id": step.get("step_id"),
                "status": "memory_reused",
                "fields_written": written,
                "fields_degraded": degraded,
            }
        if ticket.resolution == "accept_llm":
            # D6：基础 sqlglot 边（剔除 LLM 判错的 wrong_edges）重新确认并 add
            # seen_pairs——resolve 时写的基础边从未进扫描轮 seen_pairs，last_seen_at
            # 永 NULL → mark_missing 跳过，任务删除后永不 stale（删除语义不闭合，
            # 与 accept_sqlglot/manual 全量重见不对称）。剔 wrong 后确认与当初
            # resolve 的写库一致（幂等 upsert 不重复）。
            sqlglot_json = self._without_wrong_edges(
                edges_to_json(outcome.table_edges, outcome.field_edges),
                ticket.llm_opinion,
            )
            await self._apply_json_edges(
                sqlglot_json,
                task,
                step,
                sql_hash,
                provenance="sqlglot",
                confidence=1.0,
                seen_pairs=seen_pairs,
            )
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

    # ---- 未裁决票跳过 ----
    async def _skip_pending_llm(
        self,
        step: dict[str, Any],
        sql_hash: str,
        *,
        ticket: Any | None = None,
    ) -> dict[str, Any] | None:
        """同 step+sql_hash 已存在未裁决待抉择单 → 返回跳过摘要（不重复调 LLM）。

        复杂/失败节点若已有未裁决票（``resolution IS NULL``，如 diverged /
        unparseable / llm_fallback 待人工抉择），每轮重扫重复调 LLM 确认/兜底的
        意见会被 ``create_ticket`` 幂等丢弃（已存在单不更新），纯烧成本——人工
        裁决（resolve/ignore）前不重复确认。无未裁决票（无票，或已裁决——已裁决
        由 ``_reuse_resolution`` 记忆复用处理）返回 ``None``，调用方继续走 LLM。

        ticket: 可选——调用方（process_step，G3）已查过票时传入复用，避免同轮
            第二次 find_ticket_by_step_hash；None 时自查（独立调用形态）。
        """
        if ticket is None:
            ticket = await self._dp_repo.find_ticket_by_step_hash(
                step.get("step_id"), sql_hash  # type: ignore[arg-type]
            )
        if ticket is None or ticket.resolution is not None:
            return None
        return {"step_id": step.get("step_id"), "status": "ticket_pending"}

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
    ) -> tuple[int, int]:
        """入库表级边 + dp_task_refs 合并 + 字段映射独立表（幂等聚合）。

        P2 阶段 2 批量写：本 step 的表级边候选先**批量环检测**（would_create_cycle_many
        按 target 分组一次子图加载，成环边过滤），再**批量 upsert**（一次查询现存
        活跃/墓碑 + 一次 flush），字段映射**批量幂等写**（一次查询 + 一次 flush）——
        替代此前逐条「环检测 BFS + 活跃/墓碑双查 + 各自 flush」的 N+1 访问模式
        （dp 全量轮逐条写是自身元数据库每轮数万次小查询的来源）。单条路径
        （``_upsert_edge`` / ``upsert_field_mapping``）保留给补边等小量入口。

        Returns:
            ``(字段映射写入数, 其中降级标记数)``——供 run_log 字段级统计（方案 3
            可观测性：此前成功路径的字段边产出/丢弃完全无记录）。
        """
        # F4：本 step 将写入新映射——先清旧 sql_hash 映射（保留本次 hash）再写，
        # 与批量写同事务（先清后写原子）。覆盖 parsed_ok / accept_sqlglot /
        # confirm-agree / reprocess 等所有经本函数落库的路径。
        await self._purge_step_old_mappings(step, sql_hash)
        ref = build_task_ref(task, step)
        # 1) 表级边候选去重 + 字段请求按 (node 化 source,target) 分组
        table_keys: list[tuple[str, str]] = []
        seen_table: set[tuple[str, str]] = set()
        field_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for te in outcome.table_edges:
            key = (node_table(te.source), node_table(te.target))
            if key not in seen_table:
                seen_table.add(key)
                table_keys.append(key)
            for fe in outcome.field_edges:
                if fe.target_table == te.target and fe.source_table == te.source:
                    field_by_key.setdefault(key, []).append(
                        {
                            "source_table": fe.source_table,
                            "source_column": fe.source_column,
                            "target_table": fe.target_table,
                            "target_column": fe.target_column,
                            "expression": fe.expression,
                            "degraded": bool(fe.degraded),
                        }
                    )
        if not table_keys:
            return 0, 0
        # 2) 批量环检测：成环边跳过（含其字段映射，与单条 _upsert_edge 返回 None 语义一致）
        probes = [
            LineageEdge(
                source_node=s,
                target_node=t,
                edge_type="DERIVED_FROM",
                granularity="L1",
            )
            for (s, t) in table_keys
        ]
        cyclic = await self._lineage_repo.would_create_cycle_many(probes)
        if cyclic:
            for s, t in cyclic:
                logger.warning("dp_sync_edge_cycle_skipped: %s -> %s", s, t)
        valid_keys = [(s, t) for (s, t) in table_keys if (s, t) not in cyclic]
        if not valid_keys:
            return 0, 0
        # 3) 批量 upsert 表级边（一次查询 + 一次 flush），返回边实例供 edge_id/refs
        edges = await self._lineage_repo.upsert_edges_with_status_batch(
            [
                {
                    "source_node": s,
                    "target_node": t,
                    "edge_type": "DERIVED_FROM",
                    "granularity": "L1",
                    "provenance": DP_PROVENANCE,
                    "change_reason": "dp_sync",
                }
                for (s, t) in valid_keys
            ]
        )
        field_items: list[dict[str, Any]] = []
        degraded_cnt = 0
        for key in valid_keys:
            edge, _ = edges[(key[0], key[1], "DERIVED_FROM", "L1")]
            merged = DpLineageRepository.merge_task_refs(edge.dp_task_refs, ref)
            if merged != edge.dp_task_refs:
                edge.dp_task_refs = merged
            if seen_pairs is not None:
                seen_pairs.add(key)
            for fm in field_by_key.get(key, []):
                field_items.append(
                    {
                        **fm,
                        "edge_id": edge.id,
                        "confidence": 1.0,
                        "provenance": "sqlglot",
                        "sql_hash": sql_hash,
                        "task_id": task.get("task_id"),
                        "step_id": step.get("step_id"),
                    }
                )
                if fm["degraded"]:
                    degraded_cnt += 1
        # 4) 字段映射批量幂等写——written 取真实写入数（新建+复活；活跃已存在项
        # 被忽略不计，D5 统计口径），而非「尝试条数」——SQL 未变的重扫不再每轮
        # 把全量映射虚报为 field_mappings_written。
        written = 0
        if field_items:
            written = await self._dp_repo.upsert_field_mappings_batch(field_items)
        return written, degraded_cnt

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
        force_full: bool = False,
        heartbeat: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """执行一轮 dp 血缘扫描（由 arq 周期任务或手动「立即扫描」触发）。

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
            force_full: True 时**强制全量重扫**（忽略水位，完整扫 dp 全部活跃
                任务）——手动「立即扫描一轮」的用户心智是「完整跑一遍看真实解析」，
                而非增量空扫 0 任务；周期任务保持 False（增量 + 周期自动全量）。
            heartbeat: 可选心跳回调（持锁调用方注入——主循环每批任务前续期
                分布式锁，防长扫描 > 锁 TTL 被其它 cron/manual 抢占双跑，D3）。
        """
        config = await self._dp_repo.get_config()
        if config is None or (not config.enabled and not force):
            return {"skipped": "not_configured_or_disabled"}
        wm = await self._dp_repo.get_watermark("task")
        now = datetime.now(UTC)
        # 失败退避（B）：整轮异常（源库不可达等）后累积计数并写 next_scan_at，
        # 此时间前周期任务跳过自动扫描——防 poll_interval 可配到 1 分钟时源库持续
        # 不可达仍每分钟重试一次、持续空转刷 run_log（实测 280+ 轮 failed）。
        # 手动「立即扫描」（force=True）不受退避限制——用户主动触发即放行。
        if (
            not force
            and config.next_scan_at is not None
            and now < _utc_aware(config.next_scan_at)
        ):
            return {
                "skipped": "backoff",
                "next_scan_at": config.next_scan_at.isoformat(),
                "consecutive_failures": config.consecutive_failures,
            }
        # 全量轮判定：手动强制全量（force_full）或首轮/重置后全量；或距上次全量
        # 超周期自动全量（M1：稳态增量轮任务删除永不 stale——每
        # _AUTO_FULL_SCAN_SECONDS 强制一次全量观察，使 mark_missing 对「不再出现
        # 的任务边」执行失效闭环）。仅全量轮对未再出现的边执行失效观察——增量轮
        # 只处理变更任务，未变更任务边不在 seen_pairs，若每轮 mark_missing 会误伤
        # 大量正常边（P1-6）。
        full_scan = force_full or wm is None or wm.last_max_update is None
        # MySQL DATETIME 读出 naive → 归一化为 aware UTC 再比较（H10：周期轮询
        # 曾在此对 naive 水位做减法抛 TypeError，每分钟崩溃）。
        last_full_scan_at = _utc_aware(wm.last_full_scan_at) if wm else None
        last_scan_at = _utc_aware(wm.last_scan_at) if wm else None
        auto_full = (
            not full_scan
            and wm is not None
            and (
                last_full_scan_at is None
                or (now - last_full_scan_at).total_seconds()
                >= _AUTO_FULL_SCAN_SECONDS
            )
        )
        if auto_full:
            full_scan = True
        if not force and wm is not None and last_scan_at is not None:
            interval = max(1, int(config.poll_interval_minutes or 5)) * 60
            if (now - last_scan_at).total_seconds() < interval:
                return {"skipped": "interval_not_due"}
        if progress is not None:
            progress["stage"] = "collecting"
        # run_log 前置提交（B：整轮不再单一大事务——run 行若随首个任务事务
        # 回滚会丢，先独立提交持久化 running 状态，供中途/收尾 update 终态）。
        run = await self._dp_repo.create_run_log(status="running", run_at=now)
        await self._db.commit()
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
            # 未裁决票跳过（重复 LLM 拦截）：同 step+hash 已有待抉择单未裁决，
            # 本轮不重复确认——run_log detail 可见（不入 update_run_log 固定列）
            "ticket_pending": 0,
            "tickets_created": 0,
            "tickets_resolved": 0,
            "errors": 0,
            "llm_calls": 0,
            # 方案 3 可观测性：字段级映射产出统计（此前成功路径完全无记录，
            # 用户无法回答「这张表字段级解析了多少/为什么没有」）
            "field_mappings_written": 0,
            "field_edges_degraded": 0,
        }
        seen_pairs: set[tuple[str, str]] = set()
        # B：本轮失败任务 id（随 run_log detail 记录，下轮显式并入重扫）
        failed_task_ids: list[int] = []
        # D1：前轮遗留待重扫任务 id——**进入 try 前**读取（本轮 run 已前置提交为
        # running 会被 repo 排除，读到最近一条非 running 的 detail）；fetch/整轮
        # 异常发生在 try 内时 prev_retry_ids 仍已取到，收尾 failed detail 并入防
        # 丢失（失败轮被异常轮覆盖不再静默丢任务，只等 24h 全量兜底）。
        prev_retry_ids = await self._dp_repo.pending_retry_task_ids()
        try:
            # 连接获取与采集通道运行记录（begin）一并纳入 try：fetch 失败/中途异常
            # 统一走 except 记录 failed，不再让未绑定 collector 从 finally 二次抛错。
            collector = await fetch_collector(config.source_id)
            # 方案 3：schema 提供者绑定 dp collector + 数据源构建通道（per-run 缓存）
            from app.db.mysql import async_session_factory as _mysql_session_factory
            from app.services.lineage.dp_sync_schema import DpSchemaProvider

            # F1：注入独立 session 工厂——as_map 有界并发（Semaphore 6）下多路
            # 通道 B（Hive 系 DataSource DESCRIBE）用各自独立只读 session 查询，
            # 不再并发 execute 本扫描主链路共享的 self._db（SQLAlchemy AsyncSession
            # 非并发安全对象）。G2：通道 B 在 provider 内按 DataSource 行自建
            # collector，不再接收注入的 fetch_collector（死参数，已删除）。
            self._schema_provider = DpSchemaProvider(
                self._db,
                collector,
                session_factory=_mysql_session_factory,
            )
            ingest_run = await self._lineage_repo.begin_ingest_run(DP_PROVENANCE)
            # ingest_run 前置提交：同 run_log 理由——否则随首个任务事务回滚会丢。
            await self._db.commit()
            task_ids, task_max, step_max = await self._changed_ids(
                collector, config, wm, force_full=full_scan
            )
            # 失败任务重扫（B + D1）：前轮失败任务 id（try 前已读入 prev_retry_ids）
            # 显式并入变更集重扫——水位推进后增量查询不再命中它们；成功即从下轮
            # 消失（detail 只含本轮新失败，幂等安全）。
            if prev_retry_ids:
                merged = set(task_ids) | set(prev_retry_ids)
                task_ids = sorted(merged)
            if progress is not None:
                progress["total"] = len(task_ids)
                progress["processed"] = 0
                progress["current_task_id"] = None
                # 当前正在解析的节点类型（label 权威映射见 DP_STEP_TYPES）——
                # 由 _process_task 的 step 循环实时更新，供前端按类型动态展示进度。
                progress["current_step_type"] = None
                progress["current_step_label"] = None
                progress["stage"] = "parsing"
            cancelled = False
            # 明细批量预取（阶段 1）：逐任务拉取 = 每任务 2 次源库往返（task+step），
            # 一轮 N 任务 ≈ 2N 次小查询、源库连接被占几十分钟。改为按批预取——
            # 每批任务静态字段 + step 明细各 1~2 次批量 IN 查询载入内存（task/steps
            # SQL 全文本地解析），源库往返从 2N → ~N/100。预取失败时 _prefetch_batch
            # 返回 {} → _process_task 回退逐任务拉取（保底不阻断）。
            # 事务与并发（B + A）：每任务 phase1 独立小事务即时提交（无需 LLM 的写
            # 立即可见）；需 LLM 的 step 攒批后 Semaphore 并发裁决，批末逐任务
            # phase2 独立提交——任务 A 的裁决写不随任务 B 的 LLM/异常而阻塞或回滚。
            for batch_start in range(0, len(task_ids), _PREFETCH_BATCH_SIZE):
                # D3：长扫描心跳——每批续期分布式锁（TTL 重置），防扫描超锁 TTL
                # 被其它 cron/manual 抢占双跑（mark_missing 双倍累加/建单撞唯一键）。
                if heartbeat is not None:
                    await heartbeat()
                batch_ids = task_ids[batch_start : batch_start + _PREFETCH_BATCH_SIZE]
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                prefetched = await self._prefetch_batch(collector, config, batch_ids)
                # 批内待 LLM 裁决任务（方案 A phase2 队列）
                deferred: list[tuple[int, list[_LlmWork]]] = []
                for idx, task_id in enumerate(batch_ids, start=batch_start + 1):
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        break
                    if progress is not None:
                        progress["processed"] = idx - 1
                        progress["current_task_id"] = task_id
                    try:
                        # 每任务 phase1 独立小事务：任务内**无需 LLM 的写**（简单/复用
                        # step 的边、LLM 关闭分支建单、owner 回填）在此提交，处理完
                        # 即对其它会话可见（B 实时可见增强）；需 LLM 的 step 打包返回
                        # 工作项（不现场调——现场=逐任务串行 await，单轮数百次 LLM
                        # 全串行数分钟），其裁决写由批末 phase2 独立提交。
                        works = await self._process_task(
                            collector,
                            config,
                            task_id,
                            counters,
                            seen_pairs,
                            prefetched=prefetched,
                            cancel_event=cancel_event,
                            force_event=force_event,
                            progress=progress,
                            task_max=task_max,
                            step_max=step_max,
                        )
                        await self._db.commit()
                        if works:
                            deferred.append((task_id, works))
                    except _ScanCancelledError:
                        # 强制终止：当前任务随 rollback 回滚（已提交的前序任务保留）
                        await self._db.rollback()
                        cancelled = True
                        break
                    except Exception as exc:  # noqa: BLE001 —— 单任务失败不阻断整轮
                        await self._db.rollback()
                        counters["errors"] += 1
                        failed_task_ids.append(task_id)
                        logger.warning(
                            "dp_sync_task_failed task_id=%s error=%s", task_id, exc
                        )
                    if progress is not None:
                        progress["processed"] = idx
                # 批末：攒批 LLM 并发裁决（Semaphore 限流），再逐任务 phase2 独立
                # 提交裁决写（分歧建单/一致入库/LLM 异常建单）——单轮 288 次 LLM
                # 串行 ≈ 7 分钟，并发后压到 ~1/_LLM_CONCURRENCY。phase2 是任务级
                # 独立小事务：A 的裁决写不随 B 的 phase2 异常回滚。
                # F5（取消一致性）：取消信号落在不同批位置不再导致行为分叉——统一为
                # 「resolve 前检查取消：已置位则整批丢弃（不 resolve 不应用，白攒的
                # work 不烧 LLM；水位不推进 → 下轮整批重扫，幂等）；未置位则 resolve
                # 后 phase2 逐任务应用，循环内取消 → 已应用保留、剩余丢弃」。F4 已把
                # 复杂 step 的旧映射清理延后到 phase2，取消丢弃不再产生断链无留痕。
                if deferred:
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        logger.info(
                            "dp_sync_phase2_dropped_on_cancel n=%d", len(deferred)
                        )
                        break
                    batch_works = [w for _, ws in deferred for w in ws]
                    await self._resolve_llm_works(batch_works, counters)
                    for task_id, works in deferred:
                        if cancel_event is not None and cancel_event.is_set():
                            cancelled = True
                            break
                        try:
                            await self._finish_deferred_task(
                                works, config, counters, seen_pairs
                            )
                            await self._db.commit()
                        except _ScanCancelledError:
                            await self._db.rollback()
                            cancelled = True
                            break
                        except Exception as exc:  # noqa: BLE001 —— phase2 单任务失败不阻断
                            await self._db.rollback()
                            counters["errors"] += 1
                            failed_task_ids.append(task_id)
                            logger.warning(
                                "dp_sync_phase2_failed task_id=%s error=%s", task_id, exc
                            )
                if cancelled:
                    break
            # 收尾：水位 + 边确认（stale 机制）。
            # - cancelled：**不推进 max 水位**——未处理任务保留在变更集内，下轮
            #   从原水位重扫（幂等安全）；也不记录 last_full_scan_at（本轮未完整）。
            # - errors>0（部分任务硬失败，如 DB 异常/断连）：**仍推进 max 水位**并记录
            #   last_full_scan_at——失败任务（gmt_modified ≤ max）虽不进下轮增量，
            #   但由周期自动全量观察（_AUTO_FULL_SCAN_SECONDS）兜底重扫；否则只要有
            #   1 个顽固失败任务，水位永远停在初始态，每轮周期任务都全量重扫
            #   上千任务（实测每轮 6 分钟空转）。
            if cancelled:
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
            # D4：失效边恢复数（restored）不并入 tickets_resolved（后者只统计抉择单
            # 裁决）——单列 restored_edges 记入 detail，避免 run_log/统计口径被污染。
            # 删除语义闭环（P1-6）：仅**全量轮**对未再出现的边执行失效观察——
            # mark_missing 累加 missing_count（threshold=2 观察期）后标 stale，
            # 任务/节点删除后其边保留历史但进入失效队列。增量轮跳过（防误伤）。
            missing = 0
            stale_flagged = 0
            # 全量轮带任务失败（errors>0）跳过 mark_missing（D3）：失败任务回滚其
            # 边不在 seen_pairs，照常累加会把「连续两轮全量都失败」的任务旧边误标
            # stale 删边——下一轮全量（24h 自动）再补失效观察即可。
            if full_scan and not cancelled and counters["errors"] == 0:
                missing, stale_flagged = await self._lineage_repo.mark_missing(
                    DP_PROVENANCE, seen_pairs, threshold=2
                )
            await self._db.commit()
            detail = dict(counters)
            detail["scan_mode"] = "full" if full_scan else "incremental"
            detail["seen_pairs"] = len(seen_pairs)
            detail["missing"] = missing
            detail["stale_flagged"] = stale_flagged
            detail["restored_edges"] = restored
            # B：本轮失败任务 id 随 detail 记录 → 下轮 scan_once 经
            # pending_retry_task_ids 显式并入重扫（水位推进后它们不被增量命中）
            if failed_task_ids:
                detail["retry_task_ids"] = failed_task_ids
            log_status = "cancelled" if cancelled else "success"
            await self._dp_repo.update_run_log(
                run.id,
                status=log_status,
                scan_mode="full" if full_scan else "incremental",
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
                field_mappings_written=counters["field_mappings_written"],
                field_edges_degraded=counters["field_edges_degraded"],
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
            # 成功（含 cancelled——用户主动停、源库正常）一轮：连续失败归零、清除
            # 退避截止，下次恢复按 poll_interval 正常扫描。
            await self._dp_repo.reset_backoff(config.id)
            await self._db.commit()
            if progress is not None:
                progress["stage"] = "cancelled" if cancelled else "done"
            if cancelled:
                counters["skipped"] = "cancelled"
            return counters
        except asyncio.CancelledError:
            # D3 BaseException：arq job_timeout/进程关闭会以取消中断扫描——普通
            # except Exception 不捕获 CancelledError，run_log 残留 running 成为
            # latest_run_log、屏蔽前轮 retry ids。此处尽力收尾后重新抛出（取消态下
            # DB 操作失败忽略，不吞 CancelledError）。
            await self._db.rollback()
            try:
                await self._dp_repo.update_run_log(
                    run.id,
                    status="failed",
                    error="扫描被取消（job 超时/进程关闭）",
                    detail_json=json.dumps(
                        {
                            "error": "cancelled",
                            "retry_task_ids": (
                                sorted(set(prev_retry_ids) | set(failed_task_ids))
                                or None
                            ),
                        },
                        ensure_ascii=False,
                    ),
                )
                if ingest_run is not None:
                    await self._lineage_repo.finish_ingest_run(
                        ingest_run, status="failed", error="cancelled"
                    )
                await self._db.commit()
            except Exception:  # noqa: BLE001 —— 收尾失败忽略，CancelledError 继续传播
                await self._db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 —— 记录失败，下轮重试
            await self._db.rollback()
            try:
                # B：run_log 已前置提交 → 直接 update 为 failed（不留 running 残留、
                # 不重复建行）；仅当异常发生在首次前置 commit 前（run 未落库）导致
                # update 0 行时，才走内层 create 兜底。
                await self._dp_repo.update_run_log(
                    run.id,
                    status="failed",
                    scan_mode="full" if full_scan else "incremental",
                    error=str(exc),
                    detail_json=json.dumps(
                        {
                            "error": str(exc),
                            # D1：整轮异常轮（源库不可达/中途崩溃）也要并入前轮遗留
                            # 失败任务——否则本 run 成为 latest_run_log（detail 无
                            # retry ids），pending_retry_task_ids 读到空，前轮失败
                            # 任务静默丢失（只能等 24h 自动全量兜底）。
                            "retry_task_ids": (
                                sorted(set(prev_retry_ids) | set(failed_task_ids))
                                or None
                            ),
                        },
                        ensure_ascii=False,
                    ),
                )
                if ingest_run is not None:
                    await self._lineage_repo.finish_ingest_run(
                        ingest_run, status="failed", error=str(exc)
                    )
                # 整轮异常：连续失败 +1 并按阶梯写退避截止（同事务，随 run_log failed 落库）
                await self._dp_repo.record_backoff_failure(config.id)
                await self._db.commit()
            except Exception:  # noqa: BLE001 —— run 未落库等极端情况：重建 failed（双轨同写）
                await self._db.rollback()
                try:
                    await self._dp_repo.create_run_log(
                        status="failed",
                        scan_mode="full" if full_scan else "incremental",
                        error=str(exc),
                        run_at=now,
                    )
                    failed_run = await self._lineage_repo.begin_ingest_run(DP_PROVENANCE)
                    await self._lineage_repo.finish_ingest_run(
                        failed_run, status="failed", error=str(exc)
                    )
                    await self._dp_repo.record_backoff_failure(config.id)
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
        # D8：is_deleted 条件列自适应——批量变更集查询与单条 fetch 同样走列探测，
        # 极端精简元表无 is_deleted 时省略条件（防整轮 1054）。探测不可知按基线有。
        task_has_del = await self._table_has_is_deleted(
            collector, schema, task_table
        )
        step_has_del = await self._table_has_is_deleted(
            collector, schema, step_table
        )
        task_types, step_types = _type_filters(config)
        params: dict[str, Any] = {}
        task_clause, params = _in_clause("type", task_types, "t", params)
        effective_wm = None if force_full else wm
        task_wm = effective_wm.last_max_update if effective_wm is not None else None
        # 变更集查询直接带 gmt_modified（M2：不再单独查全表 MAX——两查询分离窗口内
        # 新提交行 ts ≤ 全表 MAX 会被水位永久跳过；用「已扫集 max」推进则窗口内
        # 新行 ts > 已扫集 max，下轮必然命中）。
        base = f"SELECT id, gmt_modified FROM {schema}.{task_table} WHERE 1=1"
        if task_has_del:
            base += " AND is_deleted=0"
        base += task_clause
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
            f"JOIN {schema}.{task_table} t ON st.task_id=t.id WHERE 1=1"
        )
        if step_has_del:
            step_sql += " AND st.is_deleted=0"
        if task_has_del:
            step_sql += " AND t.is_deleted=0"
        step_sql += task_join_clause + step_clause
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

    async def _table_has_is_deleted(
        self, collector: Any, schema: str, table: str
    ) -> bool:
        """目标表是否有 ``is_deleted`` 软删列（批量变更集查询 WHERE 条件用）。

        走 ``_available_columns`` 轮内缓存探测；探测不可知（None，如测试 mock/
        无 information_schema 权限）按基线「有」处理（真实 DDL 均含该列）——
        探测失败绝不导致硬编码 ``is_deleted=0`` 在精简表上报 1054（D8）。
        """
        cols = await self._available_columns(collector, schema, table)
        return cols is None or "is_deleted" in cols

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
        progress: dict[str, Any] | None = None,
        prefetched: dict[int, tuple[dict[str, Any] | None, list[dict[str, Any]]]]
        | None = None,
        task_max: datetime | None = None,
        step_max: datetime | None = None,
    ) -> list[_LlmWork]:
        """处理单个任务（方案 A phase1）：取 task 静态字段 + SQL 节点逐 step 处理。

        取消检查点下沉（A 方案）：fetch task 后 / 每个 step 前 / 资产回填前检查——
        协作取消（仅 cancel_event）在 step 边界停止，不再等完整任务跑完；
        强制终止（force_event 一并置位）在检查点直接抛 ``_ScanCancelled`` 中断。
        progress: 非空时在每个 step 处理前写入当前节点类型（current_step_type/
            current_step_label），供前端按类型动态展示扫描文案。
        prefetched: 可选批量预取结果 ``{task_id: (task|None, [steps])}``——scan_once
            按批预取后传入，本方法不再逐任务打源库（2N 次往返 → 每批 2~4 次）。
            为 ``None`` 或键缺失时回退单任务拉取（兼容独立调用/测试与预取失败降级）。
        task_max: 本轮变更集水位（task gmt_modified 最大值）——预取行 gmt_modified
            超出水位说明任务在变更集查询后被更新，跳过留待下轮（F3，见实现处）。
        step_max: 本轮变更集水位（step gmt_modified 最大值）——预取 step 行
            gmt_modified 超出水位说明该 SQL 节点在变更集查询后被更新，跳过该 step
            留待下轮（N2，F3 的 step 侧对偶——SQL 脚本变更不总伴随 task 行变化）。

        Returns:
            需批级并发 LLM 裁决的工作项列表（空=本任务已全部处理完，调用方可在
            phase1 返回后立即 commit——简单/复用 step 的边已写入 session，实时可见；
            工作项由 scan_once 攒批并发裁决后经 ``_finish_deferred_task`` 在 phase2
            独立小事务提交——phase1 与 phase2 是两个独立事务，phase2 失败不回滚
            phase1 已提交的写，二者互不阻塞（A 方案）。
        """
        if prefetched is not None and task_id in prefetched:
            task, steps = prefetched[task_id]
        else:
            task = await self._fetch_task(collector, task_id, config)
            steps = (
                await self._fetch_sql_steps(collector, task_id, config)
                if task is not None
                else []
            )
        if task is None:
            return []
        # F3（预取快照新鲜度）：task 行 gmt_modified 超出本轮变更集水位 task_max，
        # 说明该任务在「变更集查询」之后被更新（预取拿到的是中间快照）。本轮跳过：
        # 下轮增量查询 ``gmt_modified > task_max`` 必然命中最新变更由下轮专门处理。
        # 若本轮用旧快照写库、而处理期间新变更的 gmt_modified 落在 (旧, task_max]
        # 区间，水位推进后下轮增量不再命中 → 该变更静默延迟到 24h 自动全量。
        # 批量预取把「查询→处理」窗口从逐任务的秒级拉长到整批分钟级，故处理前
        # 必须校验（逐任务拉取路径同样适用，窗口小但语义一致）。
        task_gmt = task.get("gmt_modified")
        if _is_stale_snapshot(task_gmt, task_max):
            logger.info(
                "dp_sync_skip_stale_snapshot task_id=%s gmt=%s > task_max=%s",
                task_id,
                task_gmt,
                task_max,
            )
            return []
        # 排除规则：任务名命中排除
        patterns = list(config.exclude_task_patterns or [])
        if patterns:
            import re as _re

            name = str(task.get("task_name") or "")
            if any(_re.search(p, name) for p in patterns):
                return []
        if not steps:
            return []
        counters["scanned_tasks"] += 1
        # M8: llm_calls/tickets_created 计数——包装注入的 llm_chat 与 repo 建单
        # （此前恒 0，LLM 成本不可见）。service 实例每轮新建无并发共享，函数内
        # 替换 + finally 恢复保证异常安全。defer 模式下 llm_calls 由
        # _resolve_llm_works（批级并发裁决）就地累计，此处包装仅覆盖 LLM 关闭
        # 分支即时建单的 tickets_created。
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
        works: list[_LlmWork] = []
        try:
            for step in steps:
                if cancel_event is not None and cancel_event.is_set():
                    if force_event is not None and force_event.is_set():
                        raise _ScanCancelledError(f"task {task_id} force-stop")
                    break  # 协作取消：本任务剩余 steps 不再处理（已处理 steps 保留）
                # N2（step 快照新鲜度）：F3 只校验 task 行——但 SQL 脚本变更是最常见
                # 变更源，且不总伴随 task 行变化（_changed_ids 独立维护 step 水位即
                # 因此）。预取 step 行 gmt_modified 超出本轮 step 水位时，说明该节点
                # 在变更集查询后被更新（预取拿到越界行）；跳过该 step 留待下轮增量
                # 命中（task 级扫描继续处理其余 step，不整任务跳过——与 F3 的
                # task 级语义对偶）。force_full 时 step_max=全表 max，不触发跳过。
                if _is_stale_snapshot(step.get("gmt_modified"), step_max):
                    logger.info(
                        "dp_sync_skip_stale_step_snapshot task_id=%s step_id=%s "
                        "gmt=%s > step_max=%s",
                        task_id,
                        step.get("step_id"),
                        step.get("gmt_modified"),
                        step_max,
                    )
                    continue
                if progress is not None:
                    stype = int(step.get("task_step_type") or 7)
                    progress["current_step_type"] = stype
                    progress["current_step_label"] = DP_STEP_TYPES.get(
                        stype, f"类型 {stype}"
                    )
                sql = str(step.get("script_info") or "")
                counters["scanned_steps"] += 1
                result = await self.process_step(
                    task, step, sql, config, seen_pairs, defer_llm=True
                )
                # 需 LLM 的 step 打包为工作项攒批（现场不调 LLM）；正常 result 立即计数
                if isinstance(result, _LlmWork):
                    works.append(result)
                    continue
                status = result.get("status", "unknown")
                if status in counters:
                    counters[status] += 1
                # 字段级统计聚合（方案 3 可观测性）：parsed_ok/memory_reused
                # 的 detail 携带本次写入的映射数与降级数
                counters["field_mappings_written"] = counters.get(
                    "field_mappings_written", 0
                ) + int(result.get("fields_written") or 0)
                counters["field_edges_degraded"] = counters.get(
                    "field_edges_degraded", 0
                ) + int(result.get("fields_degraded") or 0)
        finally:
            self._llm_chat = llm_orig
            self._dp_repo.create_ticket = create_orig  # type: ignore[method-assign]
        # 资产 Owner 回填（产出表孤儿）——F8：本处执行即随 phase1 提交（scan_once 在
        # _process_task 返回后立即 commit），并非「随 phase2 一起提交」（含工作项的
        # 任务其边/裁决写由 phase2 独立小事务提交，二者互不干扰）。
        if cancel_event is not None and cancel_event.is_set():
            if force_event is not None and force_event.is_set():
                raise _ScanCancelledError(f"task {task_id} force-stop at backfill")
            return works  # 协作取消：跳过回填
        # D9：owner 回填（孤儿资产 + director 匹配/影子用户）包独立 try——影子用户
        # 创建/owner 更新抛错（用户名非法等）若裸调会 rollback 该任务全部边写入并
        # 每轮失败。降级记录，不拖垮任务血缘。
        try:
            await self.backfill_owner(task, config)
        except Exception as exc:  # noqa: BLE001 —— 回填失败仅告警
            logger.warning(
                "dp_sync_owner_backfill_failed task_id=%s error=%s", task_id, exc
            )
        return works

    async def _resolve_llm_works(
        self, works: list[_LlmWork], counters: dict[str, int]
    ) -> None:
        """Semaphore 并发执行批内全部待裁决 LLM 调用（结果回填 work.result/error）。

        纯 LLM 网络 IO（不碰 db session），可安全并发；db 写仍在批后逐任务串行
        （共享 AsyncSession 不可并发）。单轮数百次 LLM 串行 ≈ 数分钟，并发后
        压到 ~1/_LLM_CONCURRENCY。llm_calls 计数就地累计（defer 模式下
        process_step 现场不调 llm_chat，计数不再经 _counted_llm）。
        """
        sem = asyncio.Semaphore(_LLM_CONCURRENCY)

        async def _one(w: _LlmWork) -> None:
            async with sem:
                counters["llm_calls"] = counters.get("llm_calls", 0) + 1
                try:
                    if w.kind == "confirm":
                        w.result = await self._llm_confirm(w.sql, w.outcome)
                    else:
                        w.result = await self._llm_fallback(w.sql)
                except DpSyncLlmError as exc:
                    # LLM 输出异常：phase2 转建单（不静默丢失、不阻断其它并发项）
                    w.error = str(exc)
                except Exception as exc:  # noqa: BLE001 —— O3：注入 llm_chat/协议层
                    # 抛非 DpSyncLlmError 异常（网络中断、协议意外等）同样按 error
                    # 标记——否则 gather 会把异常上抛到 scan_once 批末（无内层 try），
                    # 整轮 failed 且本批全部 work 丢失（幂等靠下轮重扫，但白烧 LLM）。
                    logger.warning(
                        "dp_sync_llm_work_unexpected kind=%s step=%s error=%s",
                        w.kind,
                        w.step.get("step_id"),
                        exc,
                    )
                    w.error = f"LLM 调用异常：{exc}"

        await asyncio.gather(*(_one(w) for w in works))

    async def _finish_deferred_task(
        self,
        works: list[_LlmWork],
        config: Any,
        counters: dict[str, int],
        seen_pairs: set[tuple[str, str]],
    ) -> None:
        """应用一批已完成 LLM 裁决的工作项（phase2）：按任务归集落库。

        对每个工作项把并发裁决结果（result/error）落库——confirm agree 入库 /
        分歧建待抉择单 / LLM 异常建单；fallback 提炼成功建低置信参考 / 失败建
        unparseable。计数（tickets_created/status/字段映射）就地累计，与 phase1
        的语义一致。调用方（scan_once）在本函数返回后统一 commit——本阶段的
        裁决写是**独立小事务**，与 phase1（该任务无需 LLM 的简单边等写）分属两个
        事务：phase2 失败/取消只回滚本阶段写，不影响 phase1 已提交内容（A 方案）。
        F4：error 建单前统一清旧 hash 映射（phase1 对 defer 的 step 不再提前删，
        本处保证「清旧 + 留痕」同事务）；正常路径分别由 _apply_confirm_verdict /
        _apply_fallback_flow_verdict / _store_sqlglot_edges 清理。
        """
        # tickets_created 计数：包装 repo.create_ticket（同 _process_task 语义）
        create_orig = self._dp_repo.create_ticket
        counters["tickets_created"] = counters.get("tickets_created", 0)

        async def _counted_create_ticket(**kw: Any) -> Any:
            counters["tickets_created"] += 1
            return await create_orig(**kw)

        self._dp_repo.create_ticket = _counted_create_ticket  # type: ignore[method-assign]
        try:
            for w in works:
                # result 为各分支统一摘要 dict（agree/dict 摘要/error dict 摘要）
                result: dict[str, Any]
                sqlglot_json = edges_to_json(w.outcome.table_edges, w.outcome.field_edges)
                if w.kind == "confirm":
                    if w.error is not None:
                        await self._purge_step_old_mappings(w.step, w.sql_hash)
                        # O1：create_ticket 返回 DpResolutionTicket ORM（无 .get）——
                        # 不能把返回值当 result dict 用（下方 result.get("fields_*")
                        # 会 AttributeError → 该任务 phase2 整体回滚、刚建的单也丢失、
                        # 记 failed 后下轮重扫重烧 LLM、待抉择单永不落库）。error 建单
                        # 无字段写入，result 用 dict 摘要（fields_* 计 0）。
                        await self._dp_repo.create_ticket(
                            task_id=w.task.get("task_id"),
                            step_id=w.step.get("step_id"),
                            task_name=w.task.get("task_name"),
                            out_table=w.task.get("out_table"),
                            task_refs=build_task_ref(w.task, w.step),
                            sql_text=w.sql,
                            sql_hash=w.sql_hash,
                            status="diverged",
                            sqlglot_result=sqlglot_json,
                            divergence_reason=f"LLM 确认输出异常：{w.error}",
                        )
                        result = {
                            "status": "diverged",
                            "fields_written": 0,
                            "fields_degraded": 0,
                        }
                        status = "diverged"
                    else:
                        result = await self._apply_confirm_verdict(
                            w.task,
                            w.step,
                            w.sql,
                            w.sql_hash,
                            w.outcome,
                            config,
                            seen_pairs,
                            w.result,
                        )
                        status = result.get("status", "diverged")
                else:
                    if w.error is not None:
                        await self._purge_step_old_mappings(w.step, w.sql_hash)
                        # O1：同上——create_ticket 返回 ORM 无 .get，不能作 result dict。
                        await self._dp_repo.create_ticket(
                            task_id=w.task.get("task_id"),
                            step_id=w.step.get("step_id"),
                            task_name=w.task.get("task_name"),
                            out_table=w.task.get("out_table"),
                            task_refs=build_task_ref(w.task, w.step),
                            sql_text=w.sql,
                            sql_hash=w.sql_hash,
                            status="unparseable",
                            sqlglot_result=sqlglot_json,
                            divergence_reason=f"LLM 兜底输出异常：{w.error}",
                        )
                        result = {
                            "status": "unparseable",
                            "fields_written": 0,
                            "fields_degraded": 0,
                        }
                        status = "unparseable"
                    else:
                        result = await self._apply_fallback_flow_verdict(
                            w.task, w.step, w.sql, w.sql_hash, sqlglot_json, w.result
                        )
                        status = result.get("status", "unparseable")
                if status in counters:
                    counters[status] += 1
                counters["field_mappings_written"] = counters.get(
                    "field_mappings_written", 0
                ) + int(result.get("fields_written") or 0)
                counters["field_edges_degraded"] = counters.get(
                    "field_edges_degraded", 0
                ) + int(result.get("fields_degraded") or 0)
        finally:
            self._dp_repo.create_ticket = create_orig  # type: ignore[method-assign]

    async def _available_columns(
        self, collector: Any, schema: str, table: str
    ) -> set[str] | None:
        """探测 dp 元表真实列名集合（小写）；不可知返回 ``None``。

        SELECT 策略（基线 + 增强两级，见 ``_TASK_SELECT_COLS``/``_TASK_ENHANCED_COLS``）：
        基线列 = 生产真实 DDL 观测列（2026-09 dp 元库），默认必然存在，探测不可知
        时直接按基线 SELECT 即安全；探测成功后再裁剪更旧表可能缺失的基线列、
        并补上探测确认存在的增强列（settle_project_*/master_task_* 等）——
        兼容「精简生产表」与「含增强列环境」两种形态，不再依赖探测避免
        ``Unknown column``（生产曾因缺 settle_project_director 报 1054）。

        结果按 (schema, table) 轮内缓存；探测失败/空集（如测试 mock）视为不可知，
        调用方回退基线列（真实列，安全；不含增强列）。
        """
        # __init__ 已初始化缓存 dict；用 setdefault 兜底 __new__ 手工装配的
        # 测试对象（既有 helper 模式），真实对象直接复用既有缓存。
        cache = self.__dict__.setdefault("_dp_table_columns", {})
        key = (schema, table)
        if key in cache:
            return cache[key]
        cols: set[str] | None = None
        try:
            rows = await collector.query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=:d AND table_name=:t",
                {"d": schema, "t": table},
            )
            got = {
                str(r.get("column_name") or r.get("COLUMN_NAME") or "").strip().lower()
                for r in (rows or [])
                if r.get("column_name") or r.get("COLUMN_NAME")
            }
            cols = got or None
        except Exception as exc:  # noqa: BLE001 —— 探测失败即按全列处理，不阻断扫描
            logger.warning(
                "dp_column_probe_failed schema=%s table=%s error=%s", schema, table, exc
            )
            cols = None
        self._dp_table_columns[key] = cols
        return cols

    async def _fetch_task(
        self, collector: Any, task_id: int, config: Any
    ) -> dict[str, Any] | None:
        schema, task_table, _ = self._table_scope(config)
        cols = await self._available_columns(collector, schema, task_table)
        # 基线列 = 生产真实 DDL 列（必然存在）；探测不可知（None）时只用基线
        # 即安全；探测成功时再裁剪更旧表可能缺的基线列 + 补存在的增强列。
        pairs = list(_TASK_SELECT_COLS)
        if cols is not None:
            pairs = [(c, a) for c, a in pairs if c in cols] + [
                (c, a) for c, a in _TASK_ENHANCED_COLS if c in cols
            ]
        select = ", ".join(
            f"{col} AS {alias}" if col != alias else col for col, alias in pairs
        )
        rows = await collector.query(
            f"SELECT {select} FROM {schema}.{task_table} WHERE id=:tid",
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
        cols = await self._available_columns(collector, schema, step_table)
        # 同 task：基线列（真实 DDL）默认全带；探测成功才裁剪 + 补增强列。
        pairs = list(_STEP_SELECT_COLS)
        if cols is not None:
            pairs = [(c, a) for c, a in pairs if c in cols] + [
                (c, a) for c, a in _STEP_ENHANCED_COLS if c in cols
            ]
        select = ", ".join(
            f"{col} AS {alias}" if col != alias else col for col, alias in pairs
        )
        deleted = (
            ""
            if cols is not None and "is_deleted" not in cols
            else " AND is_deleted=0"
        )
        return await collector.query(
            f"SELECT {select} FROM {schema}.{step_table} "
            f"WHERE task_id=:tid{deleted}{step_clause} "
            "ORDER BY task_step",
            params,
        )

    # ---- 明细批量预取（阶段 1：2N 次逐任务往返 → 每批 2~4 次批量往返）----

    async def _fetch_tasks_batch(
        self, collector: Any, config: Any, task_ids: list[int]
    ) -> list[dict[str, Any]]:
        """批量拉取 task 静态字段（``WHERE id IN (...)``，列裁剪与 _fetch_task 同源）。

        任务数超过 ``_IN_CHUNK`` 时按块并发查询后拼接（避免单条 IN 过长）。
        """
        schema, task_table, _ = self._table_scope(config)
        cols = await self._available_columns(collector, schema, task_table)
        pairs = list(_TASK_SELECT_COLS)
        if cols is not None:
            pairs = [(c, a) for c, a in pairs if c in cols] + [
                (c, a) for c, a in _TASK_ENHANCED_COLS if c in cols
            ]
        select = ", ".join(
            f"{col} AS {alias}" if col != alias else col for col, alias in pairs
        )
        out: list[dict[str, Any]] = []
        for chunk in _chunks(task_ids, _IN_CHUNK):
            params = {f"tid{i}": v for i, v in enumerate(chunk)}
            ph = ",".join(f":tid{i}" for i in range(len(chunk)))
            rows = await collector.query(
                f"SELECT {select} FROM {schema}.{task_table} WHERE id IN ({ph})",
                params,
            )
            out.extend(rows)
        return out

    async def _fetch_steps_batch(
        self, collector: Any, config: Any, task_ids: list[int]
    ) -> list[dict[str, Any]]:
        """批量拉取 SQL 节点 step 明细（``task_id IN (...)`` + 类型过滤 + 未删）。

        与 ``_fetch_sql_steps`` 同源列裁剪/is_deleted 探测；按 task_id,task_step 排序
        返回（调用方按 task_id 归组后各任务 steps 保序）。
        """
        schema, _, step_table = self._table_scope(config)
        _, step_types = _type_filters(config)
        cols = await self._available_columns(collector, schema, step_table)
        pairs = list(_STEP_SELECT_COLS)
        if cols is not None:
            pairs = [(c, a) for c, a in pairs if c in cols] + [
                (c, a) for c, a in _STEP_ENHANCED_COLS if c in cols
            ]
        select = ", ".join(
            f"{col} AS {alias}" if col != alias else col for col, alias in pairs
        )
        deleted = (
            ""
            if cols is not None and "is_deleted" not in cols
            else " AND is_deleted=0"
        )
        out: list[dict[str, Any]] = []
        for chunk in _chunks(task_ids, _IN_CHUNK):
            params: dict[str, Any] = {}
            type_clause, params = _in_clause(
                "task_step_type", step_types, "s", params
            )
            id_clause, params = _in_clause("task_id", chunk, "t", params)
            rows = await collector.query(
                f"SELECT {select} FROM {schema}.{step_table} "
                f"WHERE 1=1{deleted}{type_clause}{id_clause} "
                "ORDER BY task_id, task_step",
                params,
            )
            out.extend(rows)
        return out

    async def _prefetch_batch(
        self, collector: Any, config: Any, task_ids: list[int]
    ) -> dict[int, tuple[dict[str, Any] | None, list[dict[str, Any]]]]:
        """按批预取 task 明细 + step 明细到内存（源库往返 2N → 每批 2~4 次）。

        返回 ``{task_id: (task_dict | None, [step_dict, ...])}``——task 已在源库
        消失的键值为 ``(None, [])``（等同 ``_fetch_task`` 返回 None 的语义，处理时
        直接跳过）。批量查询异常时返回 ``{}``（调用方 _process_task 逐任务回退，
        保证预取失败不阻断扫描且单任务错误可定位）。
        """
        if not task_ids:
            return {}
        try:
            tasks = await self._fetch_tasks_batch(collector, config, task_ids)
            steps = await self._fetch_steps_batch(collector, config, task_ids)
        except Exception as exc:  # noqa: BLE001 —— 预取失败降级逐任务拉取
            logger.warning(
                "dp_prefetch_batch_failed n=%d error=%s", len(task_ids), exc
            )
            return {}
        by_id: dict[int, tuple[dict[str, Any] | None, list[dict[str, Any]]]] = {}
        for t in tasks:
            tid = t.get("task_id")
            if tid is not None:
                by_id[int(tid)] = (t, [])
        # 变更集内未命中的任务（并发窗口内被删）补 None——与 _fetch_task None 同语义
        for tid in task_ids:
            by_id.setdefault(int(tid), (None, []))
        for s in steps:
            k = s.get("task_id")
            if k is not None and k in by_id and by_id[k][0] is not None:
                by_id[k][1].append(s)
        return by_id

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
        # N4：人工裁决是扫描轮外写边——构造局部 seen 集传 _apply_*（各路径写入后
        # add），末尾统一 touch_edges_seen 置 last_seen_at=now，使本单写入的边进入
        # 失效观察闭环（任务删除后正常 stale；任务仍在且 SQL 未变时由记忆复用
        # 重新确认，不误删）。
        local_seen: set[tuple[str, str]] = set()
        if resolution == "accept_sqlglot":
            await self._apply_json_edges(
                ticket.sqlglot_result, task, step, sql_hash,
                provenance="sqlglot", confidence=1.0,
                seen_pairs=local_seen,
            )
        elif resolution == "accept_llm":
            await self._apply_llm_resolution(
                ticket, task, step, sql_hash, seen_pairs=local_seen
            )
        elif resolution == "manual":
            payload = manual_edges if manual_edges is not None else ticket.manual_edges_json
            await self._apply_manual_edges(
                payload, task, step, sql_hash, seen_pairs=local_seen
            )
        elif resolution != "ignore":
            raise ValueError(f"未知裁决方式: {resolution}")
        await self._touch_seen(local_seen)
        await self._dp_repo.resolve_ticket(
            ticket_id,
            resolution=resolution,
            resolved_by=resolved_by,
            manual_edges=manual_edges,
        )
        return {"ticket_id": ticket_id, "resolution": resolution}

    async def resolve_llm_disabled_tickets(self, resolved_by: int) -> dict[str, int]:
        """一键处置「LLM 关闭期」待抉择单（diverged 且标记原因含 ``LLM 已关闭``）。

        背景：llm_enabled=false 时复杂节点按 plan §3.1「关 = 纯 sqlglot，复杂/失败
        节点全进待抉择」建 diverged 单（非真实语义分歧——无 LLM 对比，sqlglot 结果
        完整）。LLM 恢复开启后这批历史单仍需人工逐个「采纳 sqlglot」，效率低且淹没
        工作台。本方法按标记筛选并批量 ``accept_sqlglot`` 入库（复用 resolve_ticket
        幂等与防背离），返回 ``{"resolved": n, "failed": n, "skipped": n}``。
        """
        tickets, _ = await self._dp_repo.list_tickets(
            status="diverged", page=1, page_size=500
        )
        targets = [
            t
            for t in tickets
            if (t.divergence_reason or "").startswith("LLM 已关闭")
            and t.resolution is None
        ]
        counters = {
            "resolved": 0,
            "failed": 0,
            "skipped": len(tickets) - len(targets),  # 非标记/已裁决被排除
        }
        for tk in targets:
            try:
                await self.resolve_ticket(
                    ticket_id=tk.id,
                    resolution="accept_sqlglot",
                    resolved_by=resolved_by,
                )
                counters["resolved"] += 1
            except ValueError as exc:  # noqa: BLE001 —— 单张失败不阻断批量
                counters["failed"] += 1
                counters.setdefault("errors", []).append(str(exc))
        return counters

    async def reprocess_unparseable_tickets(self, limit: int = 200) -> dict[str, int]:
        """调度宏展开能力上线后，对存量 ``unparseable`` 单自动重判并尽量消解。

        背景：此前 dp 脚本含 ``${DATA_DATE}`` 等调度宏（平台注入、SQL 内无 set
        定义），sqlglot 无法解析致大量节点落 ``unparseable`` 淹没人工工作台；
        宏展开（``parse_dp_step`` 已内置）使多数可自动解析。本方法供一次性补扫
        与运维复用，按重判结果：
            ok      → 入库边/字段映射 + 单置 ``accept_sqlglot``（系统自动裁决）
            no_flow → 单置 ``ignore``（无数据流，无可采纳对象）
            failed  → 保留待人工（UDF 声明/方言等仍无法解析）
        O2：单内 SQL 已过时（该 step 已被更新版本的 SQL 扫过并写入字段映射，
        ``step_has_other_active_hash`` 命中）→ 作废为 ``ignore``（计 stale）——
        按历史 SQL 重判写库会用旧血缘覆盖/污染当前结果（_store_sqlglot_edges 以
        旧 hash 为 keep 清理会把新映射一并软删）。写边改走 ``_apply_json_edges``
        （不清任何 hash 映射，二道防线）+ 局部 seen 收尾 touch（N4 删除闭环）。
        返回 ``{"parsed": n, "no_flow": n, "kept": n, "stale": n}`` 计数。
        """
        tickets, _ = await self._dp_repo.list_tickets(
            status="unparseable", page=1, page_size=limit
        )
        counters = {"parsed": 0, "no_flow": 0, "kept": 0, "stale": 0}
        for tk in tickets:
            task, step = self._restore_task_step(tk)
            # O2：SQL 演进检测——该 step 已存在其它 hash 的活跃映射 → 单内 SQL
            # 过时，作废不写边（防历史血缘覆盖当前 + purge 误删新映射）。
            if await self._dp_repo.step_has_other_active_hash(
                tk.step_id, tk.sql_hash
            ):
                await self._dp_repo.resolve_ticket(
                    tk.id, resolution="ignore", resolved_by=0
                )
                counters["stale"] += 1
                continue
            outcome = parse_dp_step(
                tk.sql_text,
                dialect="hive",
                target_table=tk.out_table or None,
            )
            if outcome.status == "ok":
                # N4：局部 seen 收尾 touch——本入口写边无扫描轮 seen_pairs，不 touch
                # 则 last_seen_at 永 NULL、任务删除后永不 stale（删除语义不闭合）。
                local_seen: set[tuple[str, str]] = set()
                await self._apply_json_edges(
                    edges_to_json(outcome.table_edges, outcome.field_edges),
                    task,
                    step,
                    tk.sql_hash,
                    provenance="sqlglot",
                    confidence=1.0,
                    seen_pairs=local_seen,
                )
                await self._touch_seen(local_seen)
                await self._dp_repo.resolve_ticket(
                    tk.id, resolution="accept_sqlglot", resolved_by=0
                )
                counters["parsed"] += 1
            elif outcome.status == "no_flow":
                await self._dp_repo.resolve_ticket(
                    tk.id, resolution="ignore", resolved_by=0
                )
                counters["no_flow"] += 1
            else:
                counters["kept"] += 1
        return counters

    async def collect_retry_candidates(
        self, *, ticket_ids: list[int] | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        """收集「LLM 可重试」候选单快照（供异步重试任务建任务行）。

        与同步 ``retry_llm_tickets`` 同一候选范围（repo.list_retryable_llm_
        tickets）；快照落 tickets_json，worker 执行时逐张按 id 重读最新状态
        （已被他人裁决/删除的单跳过），任务中心按快照展示。
        """
        rows = await self._dp_repo.list_retryable_llm_tickets(
            limit=limit, ticket_ids=ticket_ids
        )
        return [
            {
                "ticket_id": t.id,
                "task_name": t.task_name,
                "out_table": t.out_table,
                "status": t.status,
            }
            for t in rows
        ]

    async def retry_llm_tickets(
        self,
        *,
        ticket_ids: list[int] | None = None,
        resolved_by: int = 0,
        limit: int = 500,
    ) -> dict[str, Any]:
        """LLM 恢复/修复后重试「LLM 类型错误」待抉择单（单条或批量）。

        范围（repo.list_retryable_llm_tickets）：未裁决且 LLM 当时失败/未跑/
        兜底低置信的单——diverged（LLM 已关闭/确认输出异常，sqlglot 结果完整）
        + llm_fallback + unparseable（LLM 已关闭/兜底输出异常）。

        处置语义（agree 自动消解，与 reprocess-unparseable 系统自动裁决一致）：
            - diverged → 重跑 LLM confirm：
                agree   → 采纳 sqlglot 入库 + 置 accept_sqlglot（auto_resolved）
                disagree→ 刷新 llm_opinion/原因，保留 diverged 待人工（refreshed）
            - llm_fallback / unparseable → 重跑 LLM 兜底：
                ok      → 刷新为 llm_fallback 低置信参考（refreshed，待人工采纳）
                仍无法提炼 → 保持/转 unparseable（kept）
            - LLM 协议/调用异常 → 保留，计 failed（单张失败不阻断批量）
        返回 ``{"auto_resolved": n, "refreshed": n, "kept": n, "failed": n,
        "details": [...]}``——details 为逐单处置明细（ticket_id/task_name/
        out_table/action/reason），供前端结果面板展示。
        """
        if self._llm_chat is None:
            self._llm_chat = await self._build_llm_chat()
        tickets = await self._dp_repo.list_retryable_llm_tickets(
            limit=limit, ticket_ids=ticket_ids
        )
        counters: dict[str, Any] = {
            "auto_resolved": 0,
            "refreshed": 0,
            "kept": 0,
            "failed": 0,
        }
        details: list[dict[str, Any]] = []
        errors: list[str] = []

        for tk in tickets:
            action, detail, err = await self._retry_one_ticket(
                tk, resolved_by=resolved_by
            )
            if action not in counters:
                counters[action] = 0
            counters[action] += 1
            details.append(detail)
            if err:
                errors.append(err)

        if errors:
            counters["errors"] = errors
        counters["details"] = details
        return counters

    async def _retry_one_ticket(
        self, tk: Any, *, resolved_by: int
    ) -> tuple[str, dict[str, Any], str | None]:
        """处置一张 LLM 可重试单，返回 ``(action, detail, error_text)``。

        供同步 ``retry_llm_tickets`` 与异步任务（``dp_retry_task`` 逐单独立
        session）共用——单张失败不阻断批量，异常转 ``failed`` 不抛出。
        action ∈ auto_resolved / refreshed / kept / failed。
        """
        if self._llm_chat is None:
            self._llm_chat = await self._build_llm_chat()
        try:
            task, step = self._restore_task_step(tk)
            sql_hash = tk.sql_hash
            if tk.status == "diverged":
                verdict = await self._llm_confirm_json(
                    tk.sql_text, tk.sqlglot_result or {}
                )
                if verdict.agree:
                    # N4：重试自动采纳是扫描轮外写边——局部 seen + touch 闭环
                    # （否则 last_seen_at 永 NULL、任务删除后边永不 stale）。
                    local_seen: set[tuple[str, str]] = set()
                    await self._apply_json_edges(
                        tk.sqlglot_result,
                        task,
                        step,
                        sql_hash,
                        provenance="sqlglot",
                        confidence=1.0,
                        seen_pairs=local_seen,
                    )
                    await self._touch_seen(local_seen)
                    await self._dp_repo.resolve_ticket(
                        tk.id,
                        resolution="accept_sqlglot",
                        resolved_by=resolved_by,
                    )
                    return (
                        "auto_resolved",
                        self._retry_detail(
                            tk, "auto_resolved", "LLM 认可 sqlglot，已自动采纳消解"
                        ),
                        None,
                    )
                await self._dp_repo.update_ticket_llm(
                    tk.id,
                    llm_opinion={
                        "agree": False,
                        "missing_edges": verdict.missing_edges,
                        "wrong_edges": verdict.wrong_edges,
                    },
                    divergence_reason=(
                        verdict.reason or "sqlglot 与 LLM 意见不一致"
                    ),
                )
                return (
                    "refreshed",
                    self._retry_detail(
                        tk,
                        "refreshed",
                        verdict.reason or "LLM 不同意 sqlglot，已刷新意见待人工",
                    ),
                    None,
                )
            # llm_fallback / unparseable：失败节点产物 → 重跑兜底
            flow = await self._llm_fallback(tk.sql_text)
            if flow.ok:
                await self._dp_repo.update_ticket_llm(
                    tk.id,
                    status="llm_fallback",
                    llm_opinion={
                        "target_tables": flow.target_tables,
                        "source_tables": flow.source_tables,
                        "field_mappings": flow.field_mappings,
                        "note": flow.note,
                    },
                    divergence_reason=(
                        flow.note or "sqlglot 解析失败，LLM 兜底提炼（低置信参考）"
                    ),
                )
                return (
                    "refreshed",
                    self._retry_detail(
                        tk,
                        "refreshed",
                        flow.note or "LLM 兜底提炼成功，已刷新低置信参考待人工",
                    ),
                    None,
                )
            await self._dp_repo.update_ticket_llm(
                tk.id,
                status="unparseable",
                llm_opinion={"note": flow.note},
                divergence_reason=(
                    flow.note or "sqlglot 与 LLM 均无法解析，请手动配置"
                ),
            )
            return (
                "kept",
                self._retry_detail(
                    tk,
                    "kept",
                    flow.note or "sqlglot 与 LLM 均无法解析，保留待手动配置",
                ),
                None,
            )
        except DpSyncLlmError as exc:  # noqa: BLE001 —— 单张失败不阻断批量
            return (
                "failed",
                self._retry_detail(tk, "failed", f"LLM 异常：{exc}"),
                f"#{tk.id}: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 —— 写库/连接等异常同样容错
            return (
                "failed",
                self._retry_detail(tk, "failed", f"处理异常：{exc}"),
                f"#{tk.id}: {exc}",
            )

    @staticmethod
    def _retry_detail(tk: Any, action: str, reason: str) -> dict[str, Any]:
        """构造一张单的 LLM 重试处置明细（结果面板逐单展示用）。"""
        return {
            "ticket_id": tk.id,
            "task_name": tk.task_name,
            "out_table": tk.out_table,
            "action": action,
            "reason": reason,
        }

    async def _build_llm_chat(self) -> Callable[..., Awaitable[dict[str, Any]]]:
        """按需构造 LLM 闭包（重试端点等未注入 llm_chat 的场景）。

        与 ``dp_sync_tasks._make_llm_chat`` 同源：LlmConfigService.build_client
        （DB 实例优先含 disable_thinking/路由 + env 兜底），异常转空 content
        由协议层建单。延迟 import 避免 service ↔ tasks 循环依赖。
        """
        from app.services.llm.config_service import LlmConfigService

        client = await LlmConfigService(self._db).build_client()

        async def llm_chat(
            messages: list[dict[str, str]], **kwargs: Any
        ) -> dict[str, Any]:
            try:
                return await client.chat(
                    messages,
                    temperature=0.0,
                    max_tokens=int(kwargs.get("max_tokens") or _LLM_MAX_TOKENS),
                )
            except Exception as exc:  # noqa: BLE001 —— LLM 故障转空输出由协议层建单
                logger.warning("dp_sync_llm_call_failed: %s", exc)
                return {"content": ""}

        return llm_chat

    async def _llm_confirm_json(
        self, sql: str, sqlglot_json: dict[str, Any]
    ) -> ConfirmVerdict:
        """对 ticket.sqlglot_result（JSON 形态）重跑 LLM 共识确认（无需重解析）。"""
        messages = build_confirm_messages(sql, sqlglot_json)
        result = await self._llm_chat(messages, max_tokens=_LLM_MAX_TOKENS)
        return parse_confirm_response(str(result.get("content") or ""))

    async def _apply_llm_resolution(
        self,
        ticket: Any,
        task: dict[str, Any],
        step: dict[str, Any],
        sql_hash: str,
        seen_pairs: set[tuple[str, str]] | None = None,
    ) -> None:
        """采纳 LLM：按单类型应用意见（diverged=sqlglot 边+补漏；llm_fallback=兜底流转）。"""
        if ticket.status == "llm_fallback":
            await self._apply_fallback_flow(
                ticket.llm_opinion, task, step, sql_hash, seen_pairs=seen_pairs
            )
            return
        # diverged：LLM 判定 sqlglot 部分边为错误（wrong_edges）——先剔除再入库，
        # 再补 LLM 认为漏掉的边（missing_edges）。此前 wrong_edges 是死字段，
        # 「采纳 LLM」与「采纳 sqlglot」实际等价，错误边从未被剔除（P1-4）。
        sqlglot_json = self._without_wrong_edges(
            ticket.sqlglot_result, ticket.llm_opinion
        )
        await self._apply_json_edges(
            sqlglot_json, task, step, sql_hash,
            provenance="sqlglot", confidence=1.0, seen_pairs=seen_pairs,
        )
        await self._apply_llm_opinion(
            ticket.llm_opinion, task, step, sql_hash, seen_pairs=seen_pairs
        )

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
