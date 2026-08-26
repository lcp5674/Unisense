"""测试数据生命周期治理定时任务（第九轮 L-3/L-4）。

覆盖：
1. purge_retained_records：软删记录超期物理清理 + 评测运行保留期清理
2. check_table_growth：通用表大小/行数巡检 + 超阈值告警事件
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_db(execute_results: list) -> AsyncMock:
    """构造 fake db：execute 按调用序列返回结果。"""
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=execute_results)
    return mock_db


def _empty_result() -> MagicMock:
    """空查询结果（无行）。"""
    r = MagicMock()
    r.scalars.return_value.all.return_value = []
    return r


def _rows_result(ids: list) -> MagicMock:
    """查询结果（含 id 列表）。"""
    r = MagicMock()
    r.scalars.return_value.all.return_value = ids
    return r


class TestPurgeRetainedRecords:
    """L-3 软删记录超期物理清理。"""

    @pytest.mark.asyncio
    async def test_no_rows_returns_zero_stats(self) -> None:
        """无超期记录时返回 SUCCESS + 各项 0。"""
        from app.tasks.data_retention import purge_retained_records

        mock_db = _mock_db([_empty_result(), _empty_result(), _empty_result(), _empty_result()])

        with patch("app.db.mysql.async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await purge_retained_records({})

        assert result["status"] == "SUCCESS"
        assert result["ruling_record"] == 0
        assert result["conflict"] == 0
        assert result["escalation_record"] == 0
        assert result["sql_infer_eval_run"] == 0
        # 无行时不发 delete
        assert mock_db.commit.called

    @pytest.mark.asyncio
    async def test_purges_expired_soft_deleted_rows(self) -> None:
        """四表均有超期行时物理删除并返回正确计数。"""
        from sqlalchemy import Delete

        from app.tasks.data_retention import purge_retained_records

        # 每表 select 后紧跟 delete（交错执行）：select→delete→select→delete...
        mock_db = _mock_db(
            [
                _rows_result([11, 12]),  # select ruling_record
                MagicMock(),  # delete ruling
                _rows_result([21]),  # select conflict
                MagicMock(),  # delete conflict
                _rows_result([31, 32, 33]),  # select escalation_record
                MagicMock(),  # delete escalation
                _rows_result([41]),  # select sql_infer_eval_run
                MagicMock(),  # delete eval
            ]
        )

        with patch("app.db.mysql.async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await purge_retained_records({})

        assert result["status"] == "SUCCESS"
        assert result["ruling_record"] == 2
        assert result["conflict"] == 1
        assert result["escalation_record"] == 3
        assert result["sql_infer_eval_run"] == 1
        # 4 条 delete 语句都已执行
        deletes = [
            c.args[0]
            for c in mock_db.execute.call_args_list
            if isinstance(c.args[0], Delete)
        ]
        assert len(deletes) == 4
        assert mock_db.commit.called

    @pytest.mark.asyncio
    async def test_only_expired_rows_selected(self) -> None:
        """select 查询按超期截止时间过滤（deleted_at < cutoff / ran_at < eval_cutoff）。"""
        from sqlalchemy import Select

        from app.tasks.data_retention import purge_retained_records

        mock_db = _mock_db([_empty_result(), _empty_result(), _empty_result(), _empty_result()])

        with patch("app.db.mysql.async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            await purge_retained_records({})

        selects = [
            c.args[0]
            for c in mock_db.execute.call_args_list
            if isinstance(c.args[0], Select)
        ]
        assert len(selects) == 4
        # 全部为 limit 受限查询（防大表一次删爆）
        for s in selects:
            assert s._limit is not None


class TestCheckTableGrowth:
    """L-4 通用表大小/行数巡检。"""

    def _info_schema_result(self, rows: list) -> MagicMock:
        r = MagicMock()
        r.mappings.return_value = rows
        return r

    @pytest.mark.asyncio
    async def test_no_oversized_tables(self) -> None:
        """全部核心表在阈值内 → 不发告警事件。"""
        from app.tasks.data_retention import check_table_growth

        rows = [
            {"table_name": "metric", "table_rows": 1000, "data_length": 1024 * 1024},
            {"table_name": "audit_log", "table_rows": 500000, "data_length": 512 * 1024 * 1024},
            {
                "table_name": "other_table",
                "table_rows": 9_000_000,
                "data_length": 99 * 1024 * 1024 * 1024,
            },
        ]
        mock_db = _mock_db([self._info_schema_result(rows)])

        with patch("app.db.mysql.async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("app.core.eventbus.get_eventbus") as mock_eb:
                mock_eb.return_value.publish = AsyncMock()
                result = await check_table_growth({})

        assert result["status"] == "SUCCESS"
        assert result["oversized"] == []
        # 非核心表即使超大也不告警
        mock_eb.return_value.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_oversized_table_publishes_alert(self) -> None:
        """行数/大小超阈值的核心表发布 storage.table_oversized 事件。"""
        from app.tasks.data_retention import check_table_growth

        rows = [
            {"table_name": "metric", "table_rows": 1000, "data_length": 1024 * 1024},
            {
                "table_name": "audit_log",
                "table_rows": 2_000_000,
                "data_length": 3 * 1024 * 1024 * 1024,
            },
        ]
        mock_db = _mock_db([self._info_schema_result(rows)])

        with patch("app.db.mysql.async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("app.core.eventbus.get_eventbus") as mock_eb:
                mock_eb.return_value.publish = AsyncMock()
                result = await check_table_growth({})

        assert result["status"] == "SUCCESS"
        assert len(result["oversized"]) == 1
        assert result["oversized"][0]["table"] == "audit_log"
        mock_eb.return_value.publish.assert_awaited_once()
        args = mock_eb.return_value.publish.await_args.args
        assert args[0] == "storage.table_oversized"
        assert args[1]["tables"][0]["table"] == "audit_log"

    @pytest.mark.asyncio
    async def test_publish_failure_is_best_effort(self) -> None:
        """告警事件发布失败不阻断巡检（best-effort）。"""
        from app.tasks.data_retention import check_table_growth

        rows = [
            {
                "table_name": "audit_log",
                "table_rows": 2_000_000,
                "data_length": 3 * 1024 * 1024 * 1024,
            },
        ]
        mock_db = _mock_db([self._info_schema_result(rows)])

        with patch("app.db.mysql.async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("app.core.eventbus.get_eventbus") as mock_eb:
                mock_eb.return_value.publish = AsyncMock(side_effect=RuntimeError("redis down"))
                result = await check_table_growth({})

        assert result["status"] == "SUCCESS"
        assert len(result["oversized"]) == 1  # 巡检结果仍返回
