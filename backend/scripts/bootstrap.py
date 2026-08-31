"""容器部署自举（bootstrap）：把「需要手动初始化」的动作全部收敛为启动期自动执行。

目标：``docker compose up -d`` 完成后所有服务立即可用，无需运维手工执行任何脚本或调用接口。

编排策略（对齐 app/main.py lifespan 的 best-effort 播种先例）：
    - **阻塞步骤**（admin、主题域+字典）：无账号/无域则平台完全不可用，失败即
      ``exit 1``，由 compose ``restart: unless-stopped`` 自动重试。
    - **尽力步骤**（ES 索引、ES 同步、Neo4j 同步）：均为可选依赖，失败只记
      warning 不阻断启动，后续由定时任务/手工补偿。

用法::

    python -m scripts.bootstrap              # 执行全部启用步骤
    UNISENSE_BOOTSTRAP_STEPS=admin,domains python -m scripts.bootstrap
    python -m scripts.bootstrap --dry-run    # 只打印计划，不执行

幂等性：所有步骤均可重复执行；二次启动全部命中已存在数据 → ``skipped``。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 将 backend/ 加入 sys.path，确保 CLI 直接执行时也能 import app / scripts
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import structlog  # noqa: E402

from app.core.logging import configure_logging  # noqa: E402
from app.db.mysql import async_session_factory  # noqa: E402

logger = structlog.get_logger("unisense.bootstrap")

#: 步骤名 → 是否阻塞可用（False = best-effort，失败不阻断启动）
BLOCKING_STEPS: dict[str, bool] = {
    "admin": True,
    "domains": True,
    "es": False,
    "neo4j": False,
}

#: 默认执行顺序（阻塞步骤在前，最慢的在后）
DEFAULT_STEPS = ("admin", "domains", "es", "neo4j")


# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #
def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("bootstrap_env_int_invalid", name=name, value=raw, fallback=default)
        return default


def _enabled_steps() -> tuple[str, ...]:
    """解析 UNISENSE_BOOTSTRAP_STEPS（逗号分隔，未知名忽略并保持默认顺序）。"""
    if not _env_flag("UNISENSE_BOOTSTRAP_ENABLED", True):
        return ()
    raw = os.getenv("UNISENSE_BOOTSTRAP_STEPS")
    if raw is None or raw.strip() == "":
        return DEFAULT_STEPS
    requested = [s.strip() for s in raw.split(",") if s.strip()]
    unknown = [s for s in requested if s not in BLOCKING_STEPS]
    if unknown:
        logger.warning("bootstrap_unknown_steps_ignored", unknown=unknown)
    return tuple(s for s in requested if s in BLOCKING_STEPS)


# --------------------------------------------------------------------------- #
# 结果模型
# --------------------------------------------------------------------------- #
@dataclass
class StepResult:
    """单步执行结果（汇总日志与退出码判定依据）。"""

    name: str
    status: str  # ok | skipped | failed
    detail: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.name,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            **self.detail,
        }


async def _execute_step(
    name: str,
    fn: Callable[[], Awaitable[StepResult]],
    timeout: int,
) -> StepResult:
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(fn(), timeout=timeout)
    except TimeoutError:
        logger.error("bootstrap_step_timeout", step=name, timeout_seconds=timeout)
        result = StepResult(name, "failed", {"error": f"timeout after {timeout}s"})
    except Exception as exc:  # noqa: BLE001 - 汇总到退出码，不冒泡中断后续步骤
        logger.exception("bootstrap_step_exception", step=name, error=str(exc))
        result = StepResult(name, "failed", {"error": str(exc)})
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info("bootstrap_step", **result.as_dict())
    return result


# --------------------------------------------------------------------------- #
# 分布式锁（多副本安全）
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def _bootstrap_lock() -> AsyncIterator[Any]:
    """整体自举锁。

    与 ``TaskLock`` 的 cron 语义相反：Redis 不可用或抢不到锁时**照常执行**
    （播种全部幂等，重复执行无副作用），仅在抢不到锁时短暂等待其它副本完成。

    Yields:
        Redis 客户端；Redis 不可用时为 ``None``。
    """
    from app.db.redis import close_redis_pool, init_redis_pool

    try:
        pool = await init_redis_pool()
    except Exception as exc:  # noqa: BLE001 - Redis 不可用时降级为无锁执行
        logger.warning("bootstrap_lock_redis_unavailable", error=str(exc))
        yield None
        return

    status, owner = await _try_acquire(
        pool, "main", ttl=_env_int("UNISENSE_BOOTSTRAP_LOCK_TTL", 300)
    )
    if status == "held":
        # 其它副本正在自举：短暂等待其完成（阻塞步骤必须做，故不跳过只等待）
        wait_seconds = _env_int("UNISENSE_BOOTSTRAP_LOCK_WAIT", 20)
        logger.info("bootstrap_lock_held_by_other_replica", wait_seconds=wait_seconds)
        await asyncio.sleep(wait_seconds)
    try:
        yield pool
    finally:
        if status == "acquired":
            await _release(pool, "main", owner)
        await close_redis_pool()


async def _try_acquire(pool: Any, name: str, ttl: int) -> tuple[str, str]:
    """尝试获取锁，返回 ``(状态, owner)``。

    Returns:
        状态为 ``acquired``（本进程持有）/ ``held``（他人持有）/ ``unavailable``
        （Redis 不可用或异常）。``TaskLock`` 把后两种情况都归为「未获锁」，
        无法区分；自举必须区分，否则 Redis 抖动会导致必做步骤被跳过。
    """
    key = f"task_lock:bootstrap:{name}"
    owner = f"{os.getpid()}-{time.monotonic_ns()}"
    try:
        acquired = await pool.set(key, owner, nx=True, ex=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.warning("bootstrap_lock_redis_unavailable", step=name, error=str(exc))
        return "unavailable", owner
    return ("acquired" if acquired else "held"), owner


async def _release(pool: Any, name: str, owner: str) -> None:
    """仅释放自己持有的锁（校验 owner 防误删他人锁）。"""
    key = f"task_lock:bootstrap:{name}"
    try:
        current = await pool.get(key)
        if current and current.decode() == owner:
            await pool.delete(key)
    except Exception as exc:  # noqa: BLE001 - 释放失败靠 TTL 兜底
        logger.warning("bootstrap_lock_release_failed", step=name, error=str(exc))


@asynccontextmanager
async def _guard_once(pool: Any, name: str, ttl: int) -> AsyncIterator[bool]:
    """多副本只跑一次的守卫，且能区分「锁被他人持有」与「Redis 不可用」。

    为什么不复用 ``TaskLock``：它把 Redis 异常按 fail-safe 吞成「未获锁」（对 cron
    「跳过本周期」是对的），但自举里会被误读成「别的副本在灌」→ 跳过必做步骤，
    导致索引空着。此处显式区分：

        - 锁被他人持有 → ``False``（跳过，对方正在灌，写入幂等）；
        - Redis 不可用/异常 → ``True``（照常执行，宁可重复不可漏灌）。

    Yields:
        ``True`` 表示本副本应执行；``False`` 表示应由其它副本完成。
    """
    if pool is None:
        yield True
        return

    status, owner = await _try_acquire(pool, name, ttl)
    if status == "unavailable":
        yield True  # Redis 不可用 → 照常执行（写入幂等，宁可重复不可漏灌）
        return
    if status == "held":
        logger.info("bootstrap_guard_held_by_other_replica", step=name)
        yield False
        return

    try:
        yield True
    finally:
        await _release(pool, name, owner)


# --------------------------------------------------------------------------- #
# 阻塞步骤：admin + 主题域 + 字典（同一事务，整体提交）
# --------------------------------------------------------------------------- #
async def _seed_core() -> tuple[StepResult, StepResult]:
    """预置管理员与主题域/字典；共享会话与事务，任一失败整体回滚。"""
    from scripts.seed_admin import seed_admin
    from scripts.seed_domains_dicts import seed_dicts, seed_domains
    from sqlalchemy.exc import IntegrityError

    async with async_session_factory() as db:
        started = time.monotonic()
        try:
            admin_summary = await seed_admin(db)
            admin_id = int(admin_summary["admin_id"])
            domain_count = await seed_domains(db, owner_id=admin_id)
            dict_count = await seed_dicts(db)
            await db.commit()
        except IntegrityError as exc:
            # 多副本并发首启：唯一约束（subject_domain.code / uk_dict_type_code）冲突
            # → 回滚后重试，此时数据已由先到副本写入，各 seed 走「已存在则跳过」分支。
            await db.rollback()
            logger.warning("bootstrap_seed_conflict_retry", error=str(exc))
            admin_summary = await seed_admin(db)
            admin_id = int(admin_summary["admin_id"])
            domain_count = await seed_domains(db, owner_id=admin_id)
            dict_count = await seed_dicts(db)
            await db.commit()
        elapsed = int((time.monotonic() - started) * 1000)

    admin_result = StepResult(
        "admin",
        "ok"
        if admin_summary.get("org_created") or admin_summary.get("admin_created")
        else "skipped",
        dict(admin_summary),
        elapsed,
    )
    domains_result = StepResult(
        "domains",
        "ok" if (domain_count or dict_count) else "skipped",
        {"domains_created": domain_count, "dict_items_created": dict_count, "owner_id": admin_id},
        elapsed,
    )
    return admin_result, domains_result


# --------------------------------------------------------------------------- #
# 尽力步骤：Elasticsearch 索引 ensure + sync
# --------------------------------------------------------------------------- #
async def _es_doc_count(es: Any, index: str) -> int | None:
    """读取索引文档数；索引不存在/查询失败返回 None（交由调用方判定）。"""
    try:
        resp = await es.search(index, {"query": {"match_all": {}}}, size=0)
    except Exception:  # noqa: BLE001 - 索引不存在会以异常形式暴露
        return None
    hits = (resp or {}).get("hits") or {}
    total = hits.get("total")
    if isinstance(total, dict):
        return int(total.get("value") or 0)
    return int(total or 0)


async def _step_es(pool: Any) -> StepResult:
    """确保 ES 索引存在；仅在「本次新建」或「索引为空」时全量同步。"""
    from app.core.es_client import get_es_client

    es = get_es_client()
    if not es.enabled or not await es.health():
        return StepResult("es", "skipped", {"reason": "es_unavailable"})

    from app.services.search.es_indexer import EsIndexer

    async with async_session_factory() as db:
        indexer = EsIndexer(db)
        created = await indexer.ensure_indexes()
        detail: dict[str, Any] = {"indexes_created": created}

        if not _env_flag("UNISENSE_BOOTSTRAP_ES_SYNC", True):
            return StepResult("es", "ok", {**detail, "sync": "disabled"})

        counts = {
            name: await _es_doc_count(es, name)
            for name in ("metric_idx", "term_idx")
        }
        detail["doc_counts"] = counts

        # 重建过 → 索引必为空，必须灌（跳不得，否则检索全空）；否则仅当某索引
        # 为空时才需补偿（可由其它副本代劳）。
        rebuilt = any(created.values())
        if not rebuilt and all((c or 0) > 0 for c in counts.values()):
            return StepResult("es", "ok", {**detail, "sync": "skipped_indexes_populated"})

        max_docs = _env_int("UNISENSE_BOOTSTRAP_ES_SYNC_MAX_DOCS", 50000)
        pending = await _count_pending_docs(db)
        if pending > max_docs:
            logger.warning(
                "bootstrap_es_sync_skipped_too_large",
                pending=pending,
                max_docs=max_docs,
                hint="改用离线批量任务初始化检索索引",
            )
            return StepResult(
                "es",
                "ok",
                {**detail, "sync": "skipped_too_large", "pending": pending},
            )

        async with _guard_once(pool, "es-sync", ttl=600) as should_run:
            if not should_run:
                return StepResult("es", "ok", {**detail, "sync": "skipped_other_replica"})
            synced = await indexer.sync_all()
        return StepResult("es", "ok", {**detail, "synced": synced, "rebuilt": rebuilt})


async def _count_pending_docs(db: Any) -> int:
    """统计待灌索引的源数据条数（用于大库保护阈值判断）。"""
    from sqlalchemy import func, select

    from app.models.metric import Metric
    from app.models.term import Term

    metric_count = await db.scalar(
        select(func.count()).select_from(Metric).where(Metric.deleted_at.is_(None))
    )
    term_count = await db.scalar(
        select(func.count()).select_from(Term).where(Term.deleted_at.is_(None))
    )
    return int(metric_count or 0) + int(term_count or 0)


# --------------------------------------------------------------------------- #
# 尽力步骤：Neo4j 资产/血缘首日同步
# --------------------------------------------------------------------------- #
async def _step_neo4j() -> StepResult:
    """首日补齐 Neo4j 资产属性与血缘边（后续漂移由 02:30 cron 兜底）。"""
    if not _env_flag("UNISENSE_BOOTSTRAP_NEO4J_SYNC", True):
        return StepResult("neo4j", "skipped", {"reason": "disabled"})

    from app.services.lineage.graph import LineageGraphClient
    from app.services.lineage.neo4j_sync import run_sync

    async with async_session_factory() as db:
        graph = LineageGraphClient()
        try:
            stats = await run_sync(db, graph)
        finally:
            await graph.dispose()
    return StepResult("neo4j", "ok", {"stats": {k: int(v) for k, v in stats.items()}})


async def _release_resources() -> None:
    """进程退出前释放引擎/ES 连接（独立进程，释放全局资源安全）。

    自举是 uvicorn 之前的独立进程，不复用这些连接池；不释放会在退出时打印
    ``Unclosed client session`` / ``Event loop is closed`` 噪音。
    """
    from app.db.mysql import engine

    with contextlib.suppress(Exception):
        await engine.dispose()

    if "es" not in _enabled_steps():
        return
    from app.core.es_client import get_es_client

    es = get_es_client()
    with contextlib.suppress(Exception):
        await es.close()


# --------------------------------------------------------------------------- #
# 编排入口
# --------------------------------------------------------------------------- #
async def main(argv: list[str] | None = None) -> int:
    """执行自举，返回进程退出码（0=可启动，1=阻塞步骤失败）。

    Returns:
        ``0`` 全部步骤 ok/skipped 或仅尽力步骤失败；``1`` 存在阻塞步骤失败。
    """
    configure_logging()
    argv = list(argv or sys.argv[1:])
    dry_run = "--dry-run" in argv
    steps = _enabled_steps()

    if dry_run:
        logger.info("bootstrap_dry_run_plan", steps=list(steps), blocking=BLOCKING_STEPS)
        return 0
    if not steps:
        logger.info("bootstrap_disabled")
        return 0

    timeout = _env_int("UNISENSE_BOOTSTRAP_TIMEOUT", 120)
    results: list[StepResult] = []

    async with _bootstrap_lock() as pool:
        # ---- 阻塞步骤：admin + domains（同一事务）----
        if "admin" in steps or "domains" in steps:
            started = time.monotonic()
            try:
                admin_result, domains_result = await asyncio.wait_for(
                    _seed_core(), timeout=timeout
                )
            except TimeoutError:
                elapsed = int((time.monotonic() - started) * 1000)
                admin_result = StepResult(
                    "admin", "failed", {"error": f"timeout {timeout}s"}, elapsed
                )
                domains_result = StepResult(
                    "domains", "failed", {"error": f"timeout {timeout}s"}, elapsed
                )
            except Exception as exc:  # noqa: BLE001 - 归一化后按阻塞性决定退出码
                logger.exception("bootstrap_seed_core_exception", error=str(exc))
                elapsed = int((time.monotonic() - started) * 1000)
                admin_result = StepResult("admin", "failed", {"error": str(exc)}, elapsed)
                domains_result = StepResult("domains", "failed", {"error": str(exc)}, elapsed)
            for item in (admin_result, domains_result):
                logger.info("bootstrap_step", **item.as_dict())
                if item.name in steps:
                    results.append(item)

        # ---- 尽力步骤 ----
        if "es" in steps:
            results.append(await _execute_step("es", lambda: _step_es(pool), timeout))
        if "neo4j" in steps:
            results.append(await _execute_step("neo4j", _step_neo4j, timeout))

    await _release_resources()

    failures = [r for r in results if r.status == "failed"]
    blocking_failed = [r for r in failures if BLOCKING_STEPS.get(r.name, False)]
    exit_code = 1 if blocking_failed else 0

    logger.info(
        "bootstrap_summary",
        steps=[r.as_dict() for r in results],
        failed=[r.name for r in failures],
        exit_code=exit_code,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
