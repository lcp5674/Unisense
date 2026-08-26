"""可观测性 Repository 单测（补齐覆盖率）。

针对 observability/repository.py 的 50% 覆盖率补充。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.feedback import Feedback
from app.services.observability.repository import ObservabilityRepository


@pytest.fixture
def db() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture
def repo(db: MagicMock) -> ObservabilityRepository:
    return ObservabilityRepository(db)


class TestObservabilityRepository:
    async def test_save_feedback(self, repo: ObservabilityRepository) -> None:
        fb = Feedback(target_type="metric", rating=5)
        result = await repo.save_feedback(fb)
        assert result is fb

    async def test_list_feedback_no_filter(self, repo: ObservabilityRepository) -> None:
        mock_count = MagicMock()
        mock_count.scalar.return_value = 7
        mock_items = MagicMock()
        mock_items.scalars.return_value.all.return_value = [Feedback(id=1)]
        repo._session.execute = AsyncMock(side_effect=[mock_count, mock_items])
        items, total = await repo.list_feedback(
            target_type=None, status=None, page=1, page_size=20
        )
        assert len(items) == 1
        assert total == 7

    async def test_list_feedback_with_filter(self, repo: ObservabilityRepository) -> None:
        mock_count = MagicMock()
        mock_count.scalar.return_value = 1
        mock_items = MagicMock()
        mock_items.scalars.return_value.all.return_value = [Feedback(id=1, target_type="metric")]
        repo._session.execute = AsyncMock(side_effect=[mock_count, mock_items])
        items, total = await repo.list_feedback(
            target_type="metric", status="pending", page=2, page_size=10
        )
        assert len(items) == 1
        assert total == 1

    async def test_list_feedback_filters_soft_deleted(
        self, repo: ObservabilityRepository
    ) -> None:
        """列表查询（count + 明细）都携带软删过滤，已删除反馈不展示。"""
        captured: list[Any] = []
        mock_count = MagicMock()
        mock_count.scalar.return_value = 0
        mock_items = MagicMock()
        mock_items.scalars.return_value.all.return_value = []

        async def fake_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            captured.append(stmt)
            return mock_count if len(captured) == 1 else mock_items

        repo._session.execute = fake_execute
        items, total = await repo.list_feedback(None, None, 1, 20)
        assert items == []
        assert total == 0
        assert len(captured) == 2
        for stmt in captured:
            assert "deleted_at" in str(stmt)
            assert "IS NULL" in str(stmt)

    async def test_resolve_target_names(self, repo: ObservabilityRepository) -> None:
        """批量解析指标名：存在返回名称，不存在/非 metric/无 target_id 为 None。"""
        items = [
            Feedback(id=1, target_type="metric", target_id="sales_gmv"),
            Feedback(id=2, target_type="metric", target_id="ghost_metric"),
            Feedback(id=3, target_type="dashboard", target_id="d1"),
            Feedback(id=4, target_type="metric", target_id=None),
        ]
        mock_result = MagicMock()
        mock_result.all.return_value = [("sales_gmv", "销售GMV")]
        repo._session.execute = AsyncMock(return_value=mock_result)
        names = await repo.resolve_target_names(items)
        assert names[1] == "销售GMV"
        assert names[2] is None  # 指标不存在 → 前端标记「已失效」
        assert names[3] is None  # 非 metric 类型不解析
        assert names[4] is None

    async def test_resolve_target_names_empty(
        self, repo: ObservabilityRepository
    ) -> None:
        assert await repo.resolve_target_names([]) == {}
        repo._session.execute.assert_not_awaited()

    async def test_resolve_target_names_no_metric(
        self, repo: ObservabilityRepository
    ) -> None:
        """全部非 metric 类反馈不触发 DB 查询。"""
        items = [Feedback(id=1, target_type="dashboard", target_id="d1")]
        assert await repo.resolve_target_names(items) == {1: None}
        repo._session.execute.assert_not_awaited()

    async def test_nps_stats(self, repo: ObservabilityRepository) -> None:
        def scalar(v: int) -> MagicMock:
            m = MagicMock()
            m.scalar.return_value = v
            return m

        repo._session.execute = AsyncMock(
            side_effect=[
                scalar(100),  # total
                scalar(60),  # promoters >=9
                scalar(20),  # passives 7-8
                scalar(20),  # detractors <=6
            ]
        )
        stats = await repo.nps_stats()
        assert stats == {
            "total": 100,
            "promoters": 60,
            "passives": 20,
            "detractors": 20,
            "score": 40.0,
        }

    async def test_nps_stats_empty(self, repo: ObservabilityRepository) -> None:
        def scalar0() -> MagicMock:
            m = MagicMock()
            m.scalar.return_value = 0
            return m

        repo._session.execute = AsyncMock(
            side_effect=[scalar0(), scalar0(), scalar0(), scalar0()]
        )
        stats = await repo.nps_stats()
        assert stats["total"] == 0
        assert stats["score"] == 0.0

    async def test_quality_events(self, repo: ObservabilityRepository) -> None:
        """质量事件明细应补全指标名/域/处理人用户名，且批量查询避免 N+1。"""
        from decimal import Decimal

        event = MagicMock(
            id=1,
            level=MagicMock(value="P0"),
            status=MagicMock(value="OPEN"),
            rule_type=MagicMock(value="ACCURACY"),
            obs_value=Decimal("85.2"),
            threshold=Decimal("99.0"),
            metric_id=5,
            ack_by=3,
            resolved_by=None,
            closed_by=None,
            ack_note="已确认处理",
            ack_at=None,
            resolved_at=None,
            closed_at=None,
            repair_suggestion=None,
            created_at=None,
        )
        mock_events = MagicMock()
        mock_events.scalars.return_value = iter([event])
        mock_metrics = MagicMock()
        mock_metrics.all.return_value = [(5, "销售GMV", "sales_gmv", "交易域")]
        mock_users = MagicMock()
        mock_users.all.return_value = [(3, "李仲裁", "lzc")]
        repo._session.execute = AsyncMock(
            side_effect=[mock_events, mock_metrics, mock_users]
        )
        events = await repo.quality_events(20)
        assert events == [
            {
                "id": 1,
                "level": "P0",
                "status": "OPEN",
                "rule_type": "ACCURACY",
                "obs_value": 85.2,
                "threshold": 99.0,
                "metric_id": 5,
                "metric_name": "销售GMV",
                "metric_code": "sales_gmv",
                "metric_domain": "交易域",
                "ack_note": "已确认处理",
                "ack_by": 3,
                "ack_by_name": "李仲裁",
                "ack_at": None,
                "resolved_by": None,
                "resolved_by_name": None,
                "resolved_at": None,
                "closed_by": None,
                "closed_by_name": None,
                "closed_at": None,
                "repair_suggestion": None,
                "created_at": None,
            }
        ]

    async def test_quality_stats(self, repo: ObservabilityRepository) -> None:
        mock_result = MagicMock()
        # 第一次 execute：by_level；第二次 execute：by_status
        mock_result.all.return_value = [("P0", 2), ("P1", 1)]
        repo._session.execute = AsyncMock(return_value=mock_result)
        stats = await repo.quality_stats()
        assert stats["by_level"] == {"P0": 2, "P1": 1}
        assert stats["by_status"] == {"P0": 2, "P1": 1}
        assert stats["total"] == 3

    async def test_api_stats(self, repo: ObservabilityRepository) -> None:
        mock_result = MagicMock()
        mock_result.all.return_value = [("CREATE", 5), ("UPDATE", 3)]
        repo._session.execute = AsyncMock(return_value=mock_result)
        stats = await repo.api_stats()
        assert stats == {"CREATE": 5, "UPDATE": 3}

    async def test_notification_stats(self, repo: ObservabilityRepository) -> None:
        mock_by_status = MagicMock()
        mock_by_status.all.return_value = [("SENT", 4), ("FAILED", 1)]
        mock_scalar1 = MagicMock()
        mock_scalar1.scalar.return_value = 10
        mock_scalar2 = MagicMock()
        mock_scalar2.scalar.return_value = 3
        repo._session.execute = AsyncMock(side_effect=[mock_by_status, mock_scalar1, mock_scalar2])
        stats = await repo.notification_stats()
        assert stats["by_status"] == {"SENT": 4, "FAILED": 1}
        assert stats["event_total"] == 10
        assert stats["event_notified"] == 3

    async def test_lineage_stats(self, repo: ObservabilityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar.return_value = 42
        repo._session.execute = AsyncMock(return_value=mock_result)
        stats = await repo.lineage_stats()
        assert stats == {"edges": 42}

    async def test_overview_stats(self, repo: ObservabilityRepository) -> None:
        def rows(*items: tuple[Any, int]) -> MagicMock:
            m = MagicMock()
            m.all.return_value = list(items)
            return m

        def scalar(v: int) -> MagicMock:
            m = MagicMock()
            m.scalar.return_value = v
            return m

        def one(v: Any) -> MagicMock:
            m = MagicMock()
            m.one.return_value = v
            return m

        def dep_obj(**kw: Any) -> MagicMock:
            m = MagicMock()
            for k, v in kw.items():
                setattr(m, k, v)
            return m

        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        # 按 overview_stats 执行顺序：assets(11) → system(3) → quality(4) → risks(3) → trends(2)
        repo._session.execute = AsyncMock(
            side_effect=[
                rows(("healthy", 2), ("unknown", 1)),  # sources by health
                scalar(1),  # open conflicts
                scalar(2),  # pending quality events
                scalar(3),  # review metrics
                scalar(0),  # open escalations
                rows(("PUBLISHED", 5), ("DRAFT", 2)),  # metrics by status
                scalar(4),  # terms
                scalar(3),  # dimensions
                scalar(2),  # domains
                scalar(1),  # clients total
                scalar(1),  # clients active
                # ---- system ----
                MagicMock(  # dependency_health scalars().all()
                    scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
                ),
                rows(("COMPLETED", 6), ("RUNNING", 2)),  # collection_run by status
                scalar(now),  # watermark max last_collected_at
                # ---- quality ----
                rows(
                    (1, 70, "GOOD", None, "指标A", "metric_a"),
                    (2, 65, "WARNING", ["sla"], "指标B", "metric_b"),
                ),  # health rows: (mid, score, level, missing, name, code)
                scalar(58),  # lineage edges
                scalar(0),  # stale edges
                one((58, now)),  # ingest (count, max run_at)
                # ---- risks ----
                scalar(305),  # pii review pending
                scalar(0),  # grants expiring
                scalar(3),  # schema drift 7d
                # ---- trends ----
                rows((now.date() - timedelta(days=1), 1), (now.date(), 2)),  # metric trend
                rows((now.date(), 3)),  # collection trend
            ]
        )
        stats = await repo.overview_stats()
        # 资产存量快照（原有字段保持不变）
        assert stats["sources"]["by_health"] == {"healthy": 2, "unknown": 1}
        assert stats["sources"]["total"] == 3
        assert stats["backlog"] == {
            "open_conflicts": 1,
            "pending_quality_events": 2,
            "review_metrics": 3,
            "open_escalations": 0,
        }
        assert stats["assets"]["metrics_by_status"] == {"PUBLISHED": 5, "DRAFT": 2}
        assert stats["assets"]["terms"] == 4
        assert stats["assets"]["dimensions"] == 3
        assert stats["assets"]["domains"] == 2
        assert stats["assets"]["sources"] == 3
        assert stats["clients"] == {"total": 1, "active": 1}
        # 系统健康：依赖聚合 + 采集链路
        assert stats["system"]["dependencies"] == {
            "by_status": {},
            "circuit_open": 0,
            "total": 0,
            "items": [],
        }
        assert stats["system"]["collection"]["by_status"] == {
            "COMPLETED": 6,
            "RUNNING": 2,
        }
        assert stats["system"]["collection"]["total"] == 8
        assert stats["system"]["collection"]["running"] == 2
        assert stats["system"]["collection"]["failed"] == 0
        assert stats["system"]["collection"]["success_rate_pct"] == 100.0
        assert stats["system"]["collection"]["last_collected_at"] == now
        # 资产质量：指标健康度 + 血缘
        assert stats["quality"]["metric_health"]["by_level"] == {"GOOD": 1, "WARNING": 1}
        assert stats["quality"]["metric_health"]["total_scored"] == 2
        assert stats["quality"]["metric_health"]["avg_score"] == 68
        assert stats["quality"]["metric_health"]["coverage_pct"] == round(2 / 7 * 100, 1)
        assert stats["quality"]["metric_health"]["top_risk"][0]["metric_id"] == 2
        assert stats["quality"]["metric_health"]["top_risk"][0]["metric_name"] == "指标B"
        assert stats["quality"]["metric_health"]["top_risk"][0]["metric_code"] == "metric_b"
        assert stats["quality"]["metric_health"]["top_risk"][0]["missing_dimensions"] == ["sla"]
        assert stats["quality"]["lineage"]["edges"] == 58
        assert stats["quality"]["lineage"]["stale"] == 0
        assert stats["quality"]["lineage"]["ingest_success"] == 58
        assert stats["quality"]["lineage"]["last_ingest_at"] == now
        # 风险雷达
        assert stats["risks"] == {
            "pii_review_pending": 305,
            "grants_expiring_soon": 0,
            "schema_drift_7d": 3,
        }
        # 近 7 天趋势（缺失日期补 0）
        assert len(stats["trends"]["metrics_created"]) == 7
        assert stats["trends"]["collections"][-1]["count"] == 3

    async def test_overview_stats_filters_soft_deleted(
        self, repo: ObservabilityRepository
    ) -> None:
        """平台概览所有计数均过滤软删数据，且冲突未决含 ESCALATED（对齐冲突模块口径）。

        回归守护：此前数据源健康聚合漏过滤 deleted_at，把已软删数据源计入，
        导致平台概览数据源数（8）与数据源管理页真实数（2）不一致。
        新增的 system/quality/risks 查询同样须带软删过滤。
        """

        def rows(*items: tuple[Any, int]) -> MagicMock:
            m = MagicMock()
            m.all.return_value = list(items)
            return m

        def scalar(v: int) -> MagicMock:
            m = MagicMock()
            m.scalar.return_value = v
            return m

        def one(v: Any) -> MagicMock:
            m = MagicMock()
            m.one.return_value = v
            return m

        def dep_empty() -> MagicMock:
            return MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            )

        captured: list[str] = []

        async def fake_execute(stmt, *a, **kw):  # type: ignore[no-untyped-def]
            captured.append(str(stmt))
            results = [
                rows(("healthy", 1)),  # 1 数据源健康（仅存活）
                scalar(1),  # 2 冲突未决
                scalar(1),  # 3 质量事件
                scalar(1),  # 4 指标 REVIEW
                scalar(0),  # 5 升级
                rows(("PUBLISHED", 7)),  # 6 指标状态
                scalar(3),  # 7 术语
                scalar(5),  # 8 维度
                scalar(10),  # 9 域
                scalar(1),  # 10 客户端总数
                scalar(1),  # 11 客户端活跃
                dep_empty(),  # 12 dependency_health
                rows(("COMPLETED", 2)),  # 13 采集运行状态
                scalar(None),  # 14 watermark max
                rows((1, 70, "GOOD", None, "指标A", "metric_a")),  # 15 指标健康度
                scalar(58),  # 16 血缘边
                scalar(0),  # 17 失效边
                one((58, None)),  # 18 血缘接入
                scalar(305),  # 19 PII 待复核
                scalar(0),  # 20 授权到期
                scalar(3),  # 21 schema 漂移
                rows(("2026-08-11", 1)),  # 22 指标趋势
                rows(("2026-08-11", 1)),  # 23 采集趋势
            ][len(captured) - 1]
            return results

        repo._session.execute = AsyncMock(side_effect=fake_execute)
        stats = await repo.overview_stats()

        assert stats["sources"]["by_health"] == {"healthy": 1}
        assert stats["sources"]["total"] == 1
        assert stats["assets"]["sources"] == 1

        # 数据源健康查询必须过滤软删（修复核心）
        src_sql = next(s for s in captured if "data_source" in s.lower())
        assert "deleted_at IS NULL" in src_sql
        # 冲突未决查询过滤软删；ESCALATED 口径从实现源码守护
        # （IN 列表在 str(stmt) 为 POSTCOMPILE 占位，无法从编译串断言）
        conflict_sql = next(s for s in captured if "conflict" in s.lower())
        assert "deleted_at IS NULL" in conflict_sql
        import inspect

        from app.services.observability.repository import (
            ObservabilityRepository as _Repo,
        )

        overview_src = inspect.getsource(_Repo.overview_stats) + inspect.getsource(
            _Repo._overview_assets
        )
        assert "ConflictStatus.ESCALATED" in overview_src
        assert "QualityEventStatus.OPEN" in overview_src
        # 质量事件 / 升级计数过滤软删
        quality_sql = next(s for s in captured if "quality_event" in s.lower())
        assert "deleted_at IS NULL" in quality_sql
        escalation_sql = next(s for s in captured if "escalation_record" in s.lower())
        assert "deleted_at IS NULL" in escalation_sql
        # 新增 system/quality/risks 查询同样带软删过滤
        collection_run_sql = next(
            s for s in captured if "collection_run" in s.lower() and "count" in s.lower()
        )
        assert "deleted_at IS NULL" in collection_run_sql
        metric_health_sql = next(
            s for s in captured if "metric_health_score" in s.lower()
        )
        assert "deleted_at IS NULL" in metric_health_sql
        db_catalog_sql = next(s for s in captured if "db_catalog" in s.lower())
        assert "deleted_at IS NULL" in db_catalog_sql
        grants_sql = next(s for s in captured if "grants" in s.lower())
        assert "deleted_at IS NULL" in grants_sql
        schema_drift_sql = next(
            s for s in captured if "schema_drift_log" in s.lower()
        )
        assert "deleted_at IS NULL" in schema_drift_sql

    async def test_commit(self, repo: ObservabilityRepository) -> None:
        await repo.commit()
        repo._session.commit.assert_called_once()

    async def test_overview_risks_filters_pii_by_org(self, repo) -> None:
        """PII 待复核按 org_id 隔离（join data_source.org_id），授权到期/漂移保持平台级。"""
        from sqlalchemy import Select

        def scalar(v: int) -> MagicMock:
            m = MagicMock()
            m.scalar.return_value = v
            return m

        repo._session.execute = AsyncMock(
            side_effect=[scalar(3), scalar(1), scalar(2)]
        )
        out = await repo._overview_risks(org_id=7)
        assert out["pii_review_pending"] == 3

        selects = [
            c.args[0] for c in repo._session.execute.call_args_list
            if isinstance(c.args[0], Select)
        ]
        pii_sql = str(selects[0].compile(compile_kwargs={"literal_binds": True}))
        assert "org_id = 7" in pii_sql
        # expiring / drift 为平台级治理项，不按组织过滤
        assert "org_id" not in str(selects[1].compile())
        assert "org_id" not in str(selects[2].compile())
