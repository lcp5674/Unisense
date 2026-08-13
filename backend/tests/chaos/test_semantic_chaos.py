"""语义模块混沌测试（缓存/依赖/版本/状态机边界）。

验证：
1. 缓存 Redis 宕机 → 熔断降级到 DB，查询仍 200
2. 依赖循环 → BusinessError 阻断
3. PENDING_VERSION 超时默认接受
4. 状态机非法跃迁 → 拒绝
5. 破坏性变更 PUBLISHED 指标 → 走 PENDING_VERSION
6. 紧急发布跳 REVIEW 但不跳 PII
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import BusinessError
from app.core.resilience import CircuitBreaker
from app.models.metric import Metric
from app.services.semantic.cache import MetricCache
from app.services.semantic.state_machine import MetricStateMachine

# ---- Fixtures ----


@pytest.fixture
def mock_db() -> AsyncMock:
    return AsyncMock()


def _make_metric(
    *,
    status: str = "DRAFT",
    pii_flag: bool = False,
    compliance_reviewed: bool = True,
    version: int = 1,
    **overrides: object,
) -> MagicMock:
    m = MagicMock(spec=Metric)
    m.id = 1
    m.metric_code = "chaos_metric"
    m.name = "混沌测试指标"
    m.status = status
    m.pii_flag = pii_flag
    m.compliance_reviewed = compliance_reviewed
    m.version = version
    m.owner_id = 10
    m.backup_owner_id = None
    m.row_version = 1
    m.definition_json = {"dependencies": ["t1"], "expression": "SUM(x)"}
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


# ---- 1. 缓存宕机降级 ----


class TestCacheFallback:
    @pytest.mark.asyncio
    async def test_get_with_redis_down(self) -> None:
        """Redis 宕机 → get 返回 None（降级到 DB），不抛异常。"""
        redis = MagicMock()
        redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
        cache = MetricCache(redis=redis, breaker=CircuitBreaker())
        result = await cache.get("chaos_metric")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalidate_with_redis_down(self) -> None:
        """Redis 宕机 → invalidate 不抛异常（静默失败）。"""
        redis = MagicMock()
        redis.scan = AsyncMock(side_effect=ConnectionError("redis down"))
        redis.delete = AsyncMock(side_effect=ConnectionError("redis down"))
        cache = MetricCache(redis=redis, breaker=CircuitBreaker())
        # invalidate 应静默（不抛异常）
        await cache.invalidate("chaos_metric")


# ---- 2. 状态机非法跃迁 ----


class TestStateMachineChaos:
    def test_deprecated_to_published_illegal(self) -> None:
        """DEPRECATED → PUBLISHED 非法跃迁。"""
        # validate_transition 返回拒绝原因字符串（None 表示合法），不抛异常
        err = MetricStateMachine.validate_transition("DEPRECATED", "PUBLISHED")
        assert err is not None

    def test_data_source_dropped_to_review_illegal(self) -> None:
        """DATA_SOURCE_DROPPED → REVIEW 非法。"""
        err = MetricStateMachine.validate_transition("DATA_SOURCE_DROPPED", "REVIEW")
        assert err is not None

    def test_published_to_draft_illegal(self) -> None:
        """PUBLISHED → DRAFT 非法（不能回退）。"""
        err = MetricStateMachine.validate_transition("PUBLISHED", "DRAFT")
        assert err is not None


# ---- 3. PENDING_VERSION 超时 ----


class TestPendingVersionTimeout:
    @pytest.mark.asyncio
    async def test_timeout_defaults_to_accept(self, mock_db: AsyncMock) -> None:
        """超时 14 天 → 默认接受（TIMEOUT_ACCEPTED）并切换 CURRENT。"""
        from app.services.semantic.pending_version_manager import (
            PendingVersionManager,
        )

        mgr = PendingVersionManager(mock_db)
        mgr._repo = AsyncMock()
        expired = MagicMock()
        expired.metric_id = 1
        expired.version = 2
        expired.consumer_id = 100
        expired.status = "PENDING"
        expired.deadline = datetime.now(UTC) - timedelta(days=1)
        expired.id = 1

        mgr._repo.get_timeout_pending_confirmations = AsyncMock(return_value=[expired])
        mgr._repo.get_pending_confirmations = AsyncMock(return_value=[expired])

        async def _mark(record_id: int, status: str) -> None:
            expired.status = status  # 模拟真实落库后状态变化

        mgr._repo.update_confirmation_status = AsyncMock(side_effect=_mark)

        switch_ids = await mgr.check_timeouts()
        # 超时记录被标记 TIMEOUT_ACCEPTED，且全部完成 → 返回待切换 metric_id
        assert mgr._repo.update_confirmation_status.called
        assert switch_ids == [1]


# ---- 4. 破坏性变更 PUBLISHED → PENDING_VERSION ----


class TestBreakingChangeOnPublished:
    @pytest.mark.asyncio
    async def test_breaking_creates_pending_not_direct_update(self, mock_db: AsyncMock) -> None:
        """PUBLISHED 指标破坏性变更 → 创建 PENDING_VERSION 而非直接生效。"""
        from app.services.semantic.service import MetricService

        svc = MetricService(mock_db)
        metric = _make_metric(status="PUBLISHED", version=3)
        metric.definition_json = {"dependencies": ["t1"], "expression": "SUM(x)"}

        # mock 依赖方法
        svc._repo = AsyncMock()
        svc._repo.get_by_code = AsyncMock(return_value=metric)
        svc._repo.update_with_optimistic_lock = AsyncMock(return_value=metric)
        svc._repo.create_version = AsyncMock()
        svc._cache = AsyncMock()
        svc._cache.invalidate = AsyncMock()

        from app.services.semantic.schemas import MetricUpdateRequest

        request = MetricUpdateRequest(
            definition_json={"dependencies": ["t2"], "expression": "SUM(y)"},
            change_reason="口径变更",
        )

        # mock PendingVersionManager.create_pending（service 在函数内局部导入，
        # 运行时从 pending_version_manager 模块解析，patch 该模块属性即可拦截）
        with patch(
            "app.services.semantic.pending_version_manager.PendingVersionManager"
        ) as mock_pvm_cls:
            mock_pvm_instance = AsyncMock()
            mock_pvm_cls.return_value = mock_pvm_instance

            await svc.update_metric("chaos_metric", request, actor_id=10, role="domain_admin")

            # 验证 PendingVersionManager.create_pending 被调用（消费方=owner）
            mock_pvm_instance.create_pending.assert_called_once()
            _, _, consumer_ids = mock_pvm_instance.create_pending.call_args.args
            assert consumer_ids == [10]


# ---- 5. 紧急发布 PII 门禁 ----


class TestEmergencyPublishPIIGate:
    @pytest.mark.asyncio
    async def test_emergency_publish_blocks_unreviewed_pii(self, mock_db: AsyncMock) -> None:
        """紧急发布跳过 REVIEW，但 PII 未合规 → BusinessError。"""
        from app.services.semantic.schemas import MetricEmergencyPublishRequest
        from app.services.semantic.service import MetricService

        svc = MetricService(mock_db)
        metric = _make_metric(status="DRAFT", pii_flag=True, compliance_reviewed=False)
        svc._repo = AsyncMock()
        svc._repo.get_by_code = AsyncMock(return_value=metric)
        # 存在活跃合规官 → 直接 COMPLIANCE_BLOCKED（FR-024 主路径）
        svc._has_active_compliance_officer = AsyncMock(return_value=True)

        request = MetricEmergencyPublishRequest(reason="紧急业务需求导致必须立即发布修复口径数据")

        with pytest.raises(BusinessError, match="PII"):
            await svc.emergency_publish_metric(
                "chaos_metric", request, actor_id=10, role="domain_admin"
            )


# ---- 6. 依赖循环检测 ----


class TestDependencyCycleDetection:
    @pytest.mark.asyncio
    async def test_circular_dependency_blocked(self, mock_db: AsyncMock) -> None:
        """A → B → A 循环依赖 → detect_cycle 返回环路径。"""
        from app.services.semantic.dependency_checker import DependencyChecker

        checker = DependencyChecker(mock_db)
        # 4 段式指标码：sales_gmv_sum_day 依赖 sales_gmv_cnt_day，后者又依赖前者 → 环
        checker._get_metric_by_code = AsyncMock(
            side_effect=lambda code: _make_metric(
                metric_code=code,
                definition_json=(
                    {"dependencies": ["sales_gmv_sum_day"]}
                    if code == "sales_gmv_cnt_day"
                    else {"dependencies": ["sales_gmv_cnt_day"]}
                ),
            )
        )

        cycle = await checker.detect_cycle(
            "sales_gmv_sum_day",
            {"dependencies": ["sales_gmv_cnt_day"]},
        )
        assert cycle is not None
        assert cycle[0] == cycle[-1]  # 环路径首尾相同
