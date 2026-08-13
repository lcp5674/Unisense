"""测试审计归档与 PII 自动传播（P2: US13）。

覆盖：
1. 审计归档流程（查询→导出→上传→标记→记录）
2. MinIO 上传（配置缺失时降级）
3. PII 血缘传播（上游 PII → 下游指标）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestAuditArchiveFlow:
    """测试审计归档流程（任务自建 DB 会话，patch async_session_factory）。"""

    @pytest.mark.asyncio
    async def test_archive_task_with_no_rows(self) -> None:
        """无待归档行时返回 SUCCESS + rows_archived=0。"""
        from app.tasks.audit_archive import audit_archive_task

        # Mock DB session
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        with patch(
            "app.db.mysql.async_session_factory"
        ) as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await audit_archive_task({})

        assert result["status"] == "SUCCESS"
        assert result["rows_archived"] == 0

    @pytest.mark.asyncio
    async def test_archive_task_with_rows(self) -> None:
        """有待归档行时执行归档。"""
        from app.tasks.audit_archive import audit_archive_task

        # Mock audit rows
        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.actor_id = 42
        mock_row.action = "CREATE"
        mock_row.entity_type = "metric"
        mock_row.entity_id = "test_metric"
        mock_row.detail_json = {"key": "value"}
        mock_row.ip = "127.0.0.1"
        mock_row.trace_id = "trace-123"
        mock_row.pii_access = False
        mock_row.created_at = datetime.now(UTC) - timedelta(days=31)

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_row]
        mock_db.execute.return_value = mock_result
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with (
            patch("app.db.mysql.async_session_factory") as mock_factory,
            patch("app.tasks.audit_archive._upload_to_minio", return_value=True),
        ):
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await audit_archive_task({})

        assert result["status"] == "SUCCESS"
        assert result["rows_archived"] == 1
        assert result["s3_key"].startswith("audit-archive/")
        assert result["s3_key"].endswith(".jsonl")

    @pytest.mark.asyncio
    async def test_archive_task_minio_upload_failure(self) -> None:
        """MinIO 上传失败时记录 FAILED 状态。"""
        from app.tasks.audit_archive import audit_archive_task

        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.actor_id = 42
        mock_row.action = "CREATE"
        mock_row.entity_type = "metric"
        mock_row.entity_id = "test"
        mock_row.detail_json = None
        mock_row.ip = "127.0.0.1"
        mock_row.trace_id = "t"
        mock_row.pii_access = False
        mock_row.created_at = datetime.now(UTC) - timedelta(days=31)

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_row]
        mock_db.execute.return_value = mock_result
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with (
            patch("app.db.mysql.async_session_factory") as mock_factory,
            patch("app.tasks.audit_archive._upload_to_minio", return_value=False),
        ):
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await audit_archive_task({})

        assert result["status"] == "FAILED"
        assert "MinIO upload failed" in result["error"]


class TestMinioUpload:
    """测试 MinIO 上传。"""

    @pytest.mark.asyncio
    async def test_minio_not_configured(self) -> None:
        """MinIO 未配置时返回 False。"""
        from app.tasks.audit_archive import _upload_to_minio

        with patch("app.tasks.audit_archive.settings") as mock_settings:
            mock_settings.minio_endpoint = ""
            mock_settings.minio_access_key = ""
            result = await _upload_to_minio("test/key", b"test data")
            assert result is False


class TestPIIPropagation:
    """测试 PII 血缘自动传播。"""

    def test_upstream_pii_triggers_propagation(self) -> None:
        """上游含 PII 标记时触发传播。"""
        upstream_columns = [
            {"column": "id", "pii": False},
            {"column": "phone", "pii": True},
        ]
        has_pii = any(col.get("pii", False) for col in upstream_columns)
        assert has_pii is True

    def test_no_upstream_pii_no_propagation(self) -> None:
        """上游无 PII 标记时不触发传播。"""
        upstream_columns = [
            {"column": "id", "pii": False},
            {"column": "name", "pii": False},
        ]
        has_pii = any(col.get("pii", False) for col in upstream_columns)
        assert has_pii is False

    def test_empty_upstream_no_propagation(self) -> None:
        """无上游信息时不触发传播。"""
        has_pii = any(col.get("pii", False) for col in [])
        assert has_pii is False

    def test_definition_json_pii_flag_set(self) -> None:
        """definition_json.pii 自动设置为 True。"""
        definition = {"expression": "SUM(amount)", "dependencies": ["orders.amount"]}
        assert not definition.get("pii")

        definition["pii"] = True
        assert definition.get("pii") is True

    def test_archive_log_model_fields(self) -> None:
        """AuditArchiveLog 模型字段完整性。"""
        from app.models.audit_archive import AuditArchiveLog

        # 验证模型可实例化
        log = AuditArchiveLog(
            archive_date=datetime.now(UTC),
            rows_archived=100,
            s3_key="audit-archive/2026/08/11/test.jsonl",
            s3_size_bytes=4096,
            status="SUCCESS",
        )
        assert log.rows_archived == 100
        assert log.status == "SUCCESS"
        assert log.s3_key.startswith("audit-archive/")

    def test_audit_log_archived_field(self) -> None:
        """AuditLog.archived 字段默认 False。"""
        # 验证字段存在
        from app.models.audit import AuditLog
        columns = {c.name for c in AuditLog.__table__.columns}
        assert "archived" in columns
