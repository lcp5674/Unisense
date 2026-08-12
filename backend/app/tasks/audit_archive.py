"""审计归档定时任务（P2: US13）。

Arq 定时任务：查询 audit_log 中 created_at < 30天前且 archived=False 的记录，
批量导出为 JSONL，上传至 MinIO（S3 兼容），更新 archived 标志，记录 AuditArchiveLog。

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

from sqlalchemy import select, update

from app.core.config import settings
from app.models.audit import AuditLog
from app.models.audit_archive import AuditArchiveLog

logger = logging.getLogger(__name__)

ARCHIVE_RETENTION_DAYS = 30
ARCHIVE_BATCH_SIZE = 1000


async def audit_archive_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """Arq 定时任务：审计日志归档。

    步骤：
    1. 查询 created_at < 30天前且 archived=False 的 audit_log
    2. 批量导出为 JSONL 格式
    3. 上传至 MinIO
    4. 更新 archived=True
    5. 记录 AuditArchiveLog
    """
    db = ctx.get("db")
    if db is None:
        logger.error("audit_archive_task: db session not provided in context")
        return {"status": "FAILED", "error": "no db session"}

    cutoff = datetime.now(UTC) - timedelta(days=ARCHIVE_RETENTION_DAYS)
    archive_date = datetime.now(UTC)

    # 1. 查询待归档记录
    stmt = (
        select(AuditLog)
        .where(AuditLog.created_at < cutoff, AuditLog.archived.is_(False))
        .limit(ARCHIVE_BATCH_SIZE)
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()

    if not rows:
        logger.info("audit_archive_task: no rows to archive")
        return {"status": "SUCCESS", "rows_archived": 0}

    # 2. 导出为 JSONL
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

    jsonl_bytes = jsonl_data.getvalue()
    date_prefix = archive_date.strftime('%Y/%m/%d')
    date_stamp = archive_date.strftime('%Y%m%d%H%M%S')
    s3_key = f"audit-archive/{date_prefix}/audit_log_{date_stamp}.jsonl"

    # 3. 上传至 MinIO
    s3_size = len(jsonl_bytes)
    upload_ok = await _upload_to_minio(s3_key, jsonl_bytes)
    if not upload_ok:
        # 记录失败日志
        archive_log = AuditArchiveLog(
            archive_date=archive_date,
            rows_archived=len(rows),
            s3_key=s3_key,
            s3_size_bytes=s3_size,
            status="FAILED",
            error_message="MinIO upload failed",
        )
        db.add(archive_log)
        await db.commit()
        return {"status": "FAILED", "error": "MinIO upload failed", "rows": len(rows)}

    # 4. 更新 archived 标志
    row_ids = [row.id for row in rows]
    await db.execute(
        update(AuditLog).where(AuditLog.id.in_(row_ids)).values(archived=True)
    )

    # 5. 记录 AuditArchiveLog
    archive_log = AuditArchiveLog(
        archive_date=archive_date,
        rows_archived=len(rows),
        s3_key=s3_key,
        s3_size_bytes=s3_size,
        status="SUCCESS",
        completed_at=datetime.now(UTC),
    )
    db.add(archive_log)
    await db.commit()

    logger.info(
        "audit_archive_task: archived %d rows to %s (%d bytes)",
        len(rows), s3_key, s3_size,
    )
    return {
        "status": "SUCCESS",
        "rows_archived": len(rows),
        "s3_key": s3_key,
        "s3_size_bytes": s3_size,
    }


async def _upload_to_minio(key: str, data: bytes) -> bool:
    """上传数据到 MinIO（S3 兼容）。

    Args:
        key: S3 对象键。
        data: 文件内容（字节）。

    Returns:
        上传是否成功。
    """
    try:
        import httpx

        endpoint = settings.minio_endpoint
        access_key = settings.minio_access_key
        secret_key = settings.minio_secret_key
        bucket = settings.minio_bucket

        if not endpoint or not access_key or not secret_key:
            logger.warning("MinIO not configured, skipping upload")
            return False

        # 简化上传：使用 MinIO S3 兼容 API
        # 生产环境应使用 minio-py 或 boto3
        url = f"http://{endpoint}/{bucket}/{key}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            # 简化：直接 PUT（生产应使用正确的 S3 签名）
            resp = await client.put(
                url,
                content=data,
                headers={
                    "Content-Type": "application/jsonl",
                    "X-Auth-Type": "minio",
                },
            )
            if resp.status_code in (200, 201, 204):
                return True
            logger.warning("MinIO upload failed: %d %s", resp.status_code, resp.text[:200])
            return False
    except Exception as exc:
        logger.warning("MinIO upload error: %s", exc)
        return False


# Arq worker 配置
async def startup(ctx: dict[str, Any]) -> None:
    """Arq worker 启动钩子。"""
    logger.info("audit_archive_worker started")


async def shutdown(ctx: dict[str, Any]) -> None:
    """Arq worker 关闭钩子。"""
    logger.info("audit_archive_worker stopped")


# Arq 定时任务配置（每天凌晨 2 点执行）
class AuditArchiveSettings:
    """Arq 定时任务配置。"""

    functions = [audit_archive_task]
    on_startup = startup
    on_shutdown = shutdown
    cron_jobs = [
        # 每天凌晨 2 点执行
        {"function": audit_archive_task, "cron": "0 2 * * *"},
    ]
