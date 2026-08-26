"""审计归档定时任务（P2: US13）。

Arq 定时任务：查询 audit_log 中 created_at < 30天前且 archived=False 的记录，
批量导出为 JSONL，上传至 MinIO（S3 兼容），更新 archived 标志，记录 AuditArchiveLog。

L-1 治理（第九轮）：归档为「物理搬迁」闭环——
- 上传成功并标记 archived=True 后，物理删除热表行（冷热分离由"复制+标记"升级为"搬迁"，
  热表不再无限增长）；
- 每批 ARCHIVE_BATCH_SIZE 条循环处理，直到清空积压或达到 MAX_BATCHES_PER_RUN 上限
  （防一次任务无限循环占满 worker）；日写入 > 批量的积压可在多次任务后消化。

依赖：
- MinIO (S3 兼容对象存储) 通过 settings.minio_* 配置
- Arq worker 调度执行
"""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update

from app.core.config import settings
from app.models.audit import AuditLog
from app.models.audit_archive import AuditArchiveLog
from app.tasks.lock import task_locked

logger = logging.getLogger(__name__)

ARCHIVE_RETENTION_DAYS = 30
ARCHIVE_BATCH_SIZE = 1000
# L-1：单次任务最多处理的批次数（1000×20 = 2 万条/次，防无限循环）
MAX_BATCHES_PER_RUN = 20
# OPS-06 (T075): 审计表容量预警阈值，超过此行数触发告警
_AUDIT_CAPACITY_WARNING = 5_000_000


def _export_jsonl(rows: list[AuditLog]) -> bytes:
    """将审计行导出为 JSONL 字节流（MinIO 冷存格式，完整保留 WORM 记录）。"""
    jsonl_data = io.BytesIO()
    for row in rows:
        record = {
            "id": row.id,
            "actor_id": row.actor_id,
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "detail_json": row.detail_json,
            "ip": row.ip,
            "trace_id": row.trace_id,
            "pii_access": row.pii_access,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        jsonl_data.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
    return jsonl_data.getvalue()


@task_locked("audit-archive")
async def audit_archive_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """Arq 定时任务：审计日志归档（物理搬迁 + 积压循环）。

    步骤（每批）：
    1. 查询 created_at < 30天前且 archived=False 的 audit_log（至多 ARCHIVE_BATCH_SIZE）
    2. 导出为 JSONL，上传至 MinIO（真实 SigV4 签名）
    3. 标记 archived=True → **物理删除热表行**（MinIO 冷存已含完整记录）
    4. 记录 AuditArchiveLog；循环处理下一批直到清空或达 MAX_BATCHES_PER_RUN

    任务自建 DB 会话（对齐 quality/semantic tasks 模式），不依赖 ctx 注入 db。
    """
    from app.db.mysql import async_session_factory

    async with async_session_factory() as db:
        # OPS-06 (T075): 审计表容量预警
        await _check_audit_capacity(db)

        cutoff = datetime.now(UTC) - timedelta(days=ARCHIVE_RETENTION_DAYS)
        archive_date = datetime.now(UTC)

        total_archived = 0
        last_s3_key: str | None = None
        last_s3_size = 0
        batches = 0
        for _ in range(MAX_BATCHES_PER_RUN):
            stmt = (
                select(AuditLog)
                .where(AuditLog.created_at < cutoff, AuditLog.archived.is_(False))
                .limit(ARCHIVE_BATCH_SIZE)
            )
            result = await db.execute(stmt)
            rows = result.scalars().all()
            if not rows:
                break

            # 导出 + 上传
            jsonl_bytes = _export_jsonl(rows)
            date_prefix = archive_date.strftime("%Y/%m/%d")
            date_stamp = archive_date.strftime("%Y%m%d%H%M%S")
            s3_key = f"audit-archive/{date_prefix}/audit_log_{date_stamp}.jsonl"
            s3_size = len(jsonl_bytes)
            upload_ok = await _upload_to_minio(s3_key, jsonl_bytes)
            if not upload_ok:
                db.add(
                    AuditArchiveLog(
                        archive_date=archive_date,
                        rows_archived=len(rows),
                        s3_key=s3_key,
                        s3_size_bytes=s3_size,
                        status="FAILED",
                        error_message="MinIO upload failed",
                    )
                )
                await db.commit()
                return {
                    "status": "FAILED",
                    "error": "MinIO upload failed",
                    "rows": len(rows),
                    "batches_done": batches,
                }

            # 标记 + 物理删除（L-1：先标记再删，DELETE 失败也不丢——已归档且标记的行不会被重复导出）
            row_ids = [row.id for row in rows]
            await db.execute(
                update(AuditLog).where(AuditLog.id.in_(row_ids)).values(archived=True)
            )
            await db.execute(delete(AuditLog).where(AuditLog.id.in_(row_ids)))

            db.add(
                AuditArchiveLog(
                    archive_date=archive_date,
                    rows_archived=len(rows),
                    s3_key=s3_key,
                    s3_size_bytes=s3_size,
                    status="SUCCESS",
                    completed_at=datetime.now(UTC),
                )
            )
            await db.commit()

            total_archived += len(rows)
            last_s3_key = s3_key
            last_s3_size = s3_size
            batches += 1

        if total_archived == 0:
            logger.info("audit_archive_task: no rows to archive")
            return {"status": "SUCCESS", "rows_archived": 0}

        logger.info(
            "audit_archive_task: archived %d rows in %d batches (last %s, %d bytes)",
            total_archived,
            batches,
            last_s3_key,
            last_s3_size,
        )
        return {
            "status": "SUCCESS",
            "rows_archived": total_archived,
            "s3_key": last_s3_key,
            "s3_size_bytes": last_s3_size,
            "batches": batches,
        }


async def _upload_to_minio(key: str, data: bytes) -> bool:
    """上传数据到 MinIO（S3 兼容，minio-py SigV4 签名）。

    Args:
        key: S3 对象键。
        data: 文件内容（字节）。

    Returns:
        上传是否成功。
    """
    try:
        from minio import Minio

        endpoint = settings.minio_endpoint
        access_key = settings.minio_access_key
        secret_key = settings.minio_secret_key
        bucket = settings.minio_bucket

        if not endpoint or not access_key or not secret_key:
            logger.warning("MinIO not configured, skipping upload")
            return False

        client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=endpoint.startswith("https://"),
        )
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)

        client.put_object(
            bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type="application/jsonl",
        )
        return True
    except Exception as exc:
        logger.warning("MinIO upload error: %s", exc)
        return False


# 注意：audit_archive_task 已在统一 worker（app/services/collector/worker.py）注册，
# 此处不再定义独立 AuditArchiveSettings，避免 cron 定义分裂（dict 格式不受 arq 支持）。


async def _check_audit_capacity(db: Any) -> None:
    """OPS-06 (T075): 检查审计表行数，超过阈值发布容量预警事件。

    Args:
        db: 异步数据库会话。
    """
    try:
        from sqlalchemy import func, select

        from app.models.audit import AuditLog

        stmt = select(func.count(AuditLog.id))
        result = await db.execute(stmt)
        total = result.scalar() or 0

        if total > _AUDIT_CAPACITY_WARNING:
            logger.warning(
                "audit_capacity_warning: audit_log rows=%d exceeds threshold=%d; %s",
                total,
                _AUDIT_CAPACITY_WARNING,
                "consider_reducing_retention_or_scaling_storage",
            )
            # 发布容量预警事件（best-effort）
            try:
                from app.core.eventbus import get_eventbus

                await get_eventbus().publish(
                    "audit.capacity_warning",
                    {
                        "total_rows": total,
                        "threshold": _AUDIT_CAPACITY_WARNING,
                        "recommendation": "reduce_retention_or_scale_storage",
                    },
                )
            except Exception:
                pass  # best-effort
    except Exception:
        logger.warning("audit_capacity_check_failed", exc_info=True)
