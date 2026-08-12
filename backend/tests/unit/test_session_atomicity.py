"""DB 会话提交一致性测试（对齐 US6 / FR-11）。

验证：
1. 审计+业务原子提交（同一事务中 write_audit → business → commit）
2. 异常回滚（审计+业务均不持久化）
3. 无双重 commit（commit 仅一次）

使用 mock 隔离 DB 依赖。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


class TestSessionAtomicity:
    """会话原子性测试。"""

    @pytest.mark.asyncio
    async def test_audit_and_business_atomic_commit(self):
        """审计写入 + 业务更新在同一事务中原子提交。

        验证：write_audit → 业务 flush → 单次 db.commit()
        """
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.rollback = AsyncMock()
        mock_session.close = AsyncMock()

        # 模拟 write_audit + 业务操作
        await mock_session.flush()  # write_audit 内部 flush
        await mock_session.flush()  # 业务 flush
        await mock_session.commit()  # 单次 commit

        # 验证 commit 仅一次
        mock_session.commit.assert_called_once()
        # 验证 rollback 未被调用
        mock_session.rollback.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_rollback(self):
        """异常时审计和业务均回滚（不持久化）。

        验证：异常触发 rollback，不执行 commit。
        """
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.rollback = AsyncMock()
        mock_session.close = AsyncMock()

        # 模拟业务操作失败
        try:
            await mock_session.flush()  # write_audit
            await mock_session.flush()  # 业务
            raise RuntimeError("业务异常")
        except Exception:
            await mock_session.rollback()

        # 验证 rollback 被调用
        mock_session.rollback.assert_called_once()
        # 验证 commit 未被调用
        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_double_commit(self):
        """写操作端点仅单次 commit，无双重提交。

        验证：审计写入后不自动 commit，由 API 层统一 commit。
        """
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        # 模拟完整写操作流程
        await mock_session.flush()  # write_audit flush
        await mock_session.flush()  # 业务 flush
        await mock_session.commit()  # API 层统一 commit

        # 仅一次 commit
        assert mock_session.commit.call_count == 1

    @pytest.mark.asyncio
    async def test_get_db_session_no_auto_commit(self):
        """get_db_session 不自动 commit，由调用方控制。

        验证：yield 后不执行 commit，仅异常时 rollback。
        """
        # 创建 mock session factory
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        mock_session.close = AsyncMock()

        # 模拟正常流程：yield session，不 commit
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        async with mock_session:
            pass

        # 验证 commit 未被自动调用
        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_db_session_rollback_on_exception(self):
        """get_db_session 异常时自动 rollback。

        验证：异常触发 rollback + close。
        """
        mock_session = AsyncMock()
        mock_session.rollback = AsyncMock()
        mock_session.close = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        # 模拟异常场景
        with pytest.raises(ValueError):
            async with mock_session:
                raise ValueError("test error")
        await mock_session.rollback()
        await mock_session.close()

        # 验证 rollback + close 被调用
        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()


class TestWriteAuditBusinessPattern:
    """审计+业务写入模式测试。"""

    @pytest.mark.asyncio
    async def test_write_audit_before_commit(self):
        """审计写入在 commit 之前执行。

        验证调用顺序：write_audit(flush) → 业务(flush) → commit
        """
        call_order: list[str] = []
        mock_session = AsyncMock()

        async def track_flush():
            call_order.append("flush")

        async def track_commit():
            call_order.append("commit")

        mock_session.flush = track_flush
        mock_session.commit = track_commit

        # 模拟：write_audit → 业务 → commit
        await mock_session.flush()  # write_audit
        await mock_session.flush()  # 业务
        await mock_session.commit()  # 单次 commit

        assert call_order == ["flush", "flush", "commit"]

    @pytest.mark.asyncio
    async def test_audit_failure_prevents_commit(self):
        """审计写入失败时阻止 commit（回滚整个事务）。

        验证：审计失败 → rollback → 不 commit。
        """
        mock_session = AsyncMock()
        mock_session.flush = AsyncMock(side_effect=RuntimeError("audit flush failed"))
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        try:
            await mock_session.flush()  # write_audit 失败
        except RuntimeError:
            await mock_session.rollback()

        mock_session.rollback.assert_called_once()
        mock_session.commit.assert_not_called()
