"""语义服务单元测试（对齐 DEV_GUIDE §8b：纯逻辑、可独立运行）。

使用 mock 替换 MetricRepository，覆盖状态机、乐观锁、PII 合规闸门、
破坏性变更判定与分页计算。无数据库依赖。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.conftest import make_create_payload, make_metric

from app.core.exceptions import (
    AuthError,
    BusinessError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.models.metric import Metric
from app.services.governance.policy import Decision
from app.services.semantic.schemas import (
    MetricApproveRequest,
    MetricConsumptionGuideUpdateRequest,
    MetricCreateRequest,
    MetricDescriptionUpdateRequest,
    MetricEmergencyPublishRequest,
    MetricListParams,
    MetricRejectRequest,
    MetricSubmitRequest,
    MetricUpdateRequest,
    SqlBatchCreateCandidate,
)
from app.services.semantic.service import MetricService


def _svc_with_repo() -> tuple[MetricService, MagicMock]:
    """构造服务并替换其仓库为 mock，返回 (service, mock_repo_instance)。

    修复后：update_metric / submit_metric / approve_metric 调用
    GovernanceService.check_metric_permission，
    此处通过构造函数注入 mock 返回 allow=True，不阻断非 PDP 测试路径。
    """
    mock_gov_svc = MagicMock()
    mock_gov_svc.check_metric_permission = AsyncMock(
        return_value=Decision(allow=True, reason="mocked_allowed")
    )
    # deprecate 被引用拦截（TD §12.3）默认走「无引用者」路径：db.execute 返回
    # 空结果集，避免单元测试连库；有引用场景由专项测试用 patch 覆盖。
    _db = MagicMock()
    _db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    with patch("app.services.semantic.service.MetricRepository") as mock_repo_cls:
        svc = MetricService(db=_db, governance_svc=mock_gov_svc)
        # create_metric 3a 步 OneData 校验：原子指标关联的逻辑度量须存在且 PUBLISHED。
        # 默认 mock 为「已发布」度量（default_unit=元），happy path 不阻断；缺度量/未发布
        # 场景由专项测试替换 svc._measure_repo 覆盖（如 measure_missing / measure_draft）。
        measure_repo = MagicMock()
        fake_measure = MagicMock()
        fake_measure.status = "PUBLISHED"
        fake_measure.default_unit = "元"
        measure_repo.get_by_id = AsyncMock(return_value=fake_measure)
        svc._measure_repo = measure_repo
        # submit 路径会经 _notify_metric_stakeholders 开独立 DB 会话做定向通知，
        # 单元测试中会跨事件循环连库产生 RuntimeError——统一 mock 掉（通知行为另有集成测试）。
        svc._notify_metric_stakeholders = AsyncMock(return_value=None)
        # PENDING 确认期创建后定向通知消费方（同样跨事件循环，统一 mock）
        svc._notify_pending_consumers = AsyncMock(return_value=None)
        # PENDING 确认期检查（update_metric 破坏性变更前置）默认无待确认版本，
        # 个别测试覆盖为 True 验证防叠加。
        mock_repo_cls.return_value.has_pending_version = AsyncMock(return_value=False)
        # P2-14 owner 名称映射：默认空（对比/治理路径不依赖 owner 解析）；个别测试覆盖
        mock_repo_cls.return_value.get_user_display_names = AsyncMock(return_value={})
        return svc, mock_repo_cls.return_value


async def test_create_metric_happy_path():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    result = await svc.create_metric(MetricCreateRequest(**make_create_payload()), owner_id=1)

    repo.get_by_code.assert_awaited_once_with("sales_gmv_amount_daily")
    repo.create.assert_awaited_once()
    repo.create_version.assert_awaited_once()
    assert result.status == "DRAFT"
    assert result.version == 1
    assert result.row_version == 1


async def test_create_atomic_passes_measure_id():
    """OneData：原子指标创建透传 measure_id（逻辑度量引用）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.create_metric(
        MetricCreateRequest(**make_create_payload(measure_id=7)), owner_id=1
    )

    captured = repo.create.call_args[0][0]
    assert captured.measure_id == 7


async def test_create_atomic_without_measure_rejected():
    """方案 B：原子指标缺 measure_id → 422（原子只从逻辑度量目录创建，SQL 推断
    不再产原子；即使带旧式物理来源（source_table+measure_column 通过 schema 兼容
    校验），service 层仍拒绝——旧式来源不能替代逻辑度量）。"""
    from app.core.exceptions import ValidationError

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    with pytest.raises(ValidationError) as ei:
        await svc.create_metric(
            MetricCreateRequest(
                **make_create_payload(
                    measure_id=None,
                    source_table="dwd.sales_detail",
                    measure_column="gmv",
                )
            ),
            owner_id=1,
        )
    assert ei.value.error_code == "MEASURE_REQUIRED"
    assert repo.create.call_count == 0


async def test_create_atomic_with_mount_rejected():
    """方案 B：原子指标携带挂载实体 → 拒绝（挂载是派生指标变体载体，原子不绑物理表）。

    schema 层 model_validator 已先拦截（「仅派生指标可挂载，当前类型 atomic」），
    service 层 ATOMIC_NO_MOUNT 为纵深防御——此处断言 schema 层错误信息。"""
    from pydantic import ValidationError as PydanticValidationError

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    with pytest.raises(PydanticValidationError) as ei:
        await svc.create_metric(
            MetricCreateRequest(
                **make_create_payload(
                    measure_id=1,
                    mounts=[
                        {
                            "source_table": "dwd.sales_detail",
                            "source_column": "gmv",
                            "granularity": "日",
                            "default_period": "day",
                            "domain": "sales",
                        }
                    ],
                )
            ),
            owner_id=1,
        )
    assert "仅派生指标可挂载" in str(ei.value)
    assert repo.create.call_count == 0


async def test_create_persists_description_and_term():
    """创建透传业务描述与关联术语：description（manual 来源）+ term_id 落 Metric ORM。

    详情页「业务描述」卡片与「关联术语」卡片注册时即可填写，与注册后补录同构。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())
    # term_id 校验：mock DB 返回存在的术语（scalar_one_or_none 非 None）
    svc._db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=7))
    )

    await svc.create_metric(
        MetricCreateRequest(
            **make_create_payload(
                description="每日成交总金额（口径：支付成功订单的实付金额汇总）",
                term_id=7,
            )
        ),
        owner_id=1,
    )

    captured = repo.create.call_args[0][0]
    assert captured.description == "每日成交总金额（口径：支付成功订单的实付金额汇总）"
    assert captured.description_source == "manual"
    assert captured.term_id == 7


async def test_create_rejects_missing_term():
    """创建时关联术语不存在 → 404（NotFoundError），不落库。"""
    from app.core.exceptions import NotFoundError

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())
    # term_id 校验：mock DB 返回无术语（scalar_one_or_none=None）
    svc._db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )

    with pytest.raises(NotFoundError) as ei:
        await svc.create_metric(
            MetricCreateRequest(**make_create_payload(term_id=999)), owner_id=1
        )
    assert ei.value.error_code == "NOT_FOUND"
    assert repo.create.call_count == 0


async def test_create_single_dw_developer_required():
    """注册门禁：单条/向导创建必须指定数仓开发责任方（PRD 4.5 口径落地责任人）。

    ``dw_developer_id``（平台用户）与 ``dw_developer_name``（外部人员）皆空 → schema
    层 422，``create_metric`` 不落库。外部人员名称兜底（name 非空）视为已指定。"""
    from pydantic import ValidationError as PydanticValidationError

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    with pytest.raises(PydanticValidationError) as ei:
        await svc.create_metric(
            MetricCreateRequest(
                **make_create_payload(dw_developer_id=None, dw_developer_name=None)
            ),
            owner_id=1,
        )
    assert "数仓开发责任方为必填" in str(ei.value)
    assert repo.create.call_count == 0

    # 外部人员名称兜底：name 非空视为已指定，放行
    repo.create.reset_mock()
    await svc.create_metric(
        MetricCreateRequest(
            **make_create_payload(dw_developer_id=None, dw_developer_name="外部数仓D")
        ),
        owner_id=1,
    )
    assert repo.create.call_count == 1


async def test_create_batch_without_dw_developer_allowed():
    """批量注册（batch_id 非空）不强制数仓开发——整批共享责任方由候选可带透传，
    避免单候选缺省导致整批 422（批量责任方治理沿用候选级可选 + 后续补录）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    req = MetricCreateRequest(
        **make_create_payload(dw_developer_id=None, dw_developer_name=None, batch_id="batch_abc")
    )
    assert req.dw_developer_id is None  # schema 放行（不抛异常）


async def test_create_metric_persists_responsibility_names():
    """创建时透传外部人员名称（id 为空，纯文本兜底——责任方非平台用户）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.create_metric(
        MetricCreateRequest(
            **make_create_payload(
                product_owner_id=None,
                product_owner_name="外部需求方A",
                tech_owner_name="外部技术方B",
                dw_developer_name="外部数仓C",
            )
        ),
        owner_id=1,
    )

    captured = repo.create.call_args[0][0]
    assert captured.product_owner_id is None
    assert captured.product_owner_name == "外部需求方A"
    assert captured.tech_owner_name == "外部技术方B"
    assert captured.dw_developer_name == "外部数仓C"


async def test_create_derived_with_mount_creates_mount_and_backfills_granularity():
    """OneData：派生指标携带 mount → 自动建 metric_mount + 粒度回填 metric.granularity。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric(type="derived")
    created.id = 1
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    with patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls:
        mrepo_cls.return_value.save = AsyncMock(side_effect=lambda m: m)
        await svc.create_metric(
            MetricCreateRequest(
                **make_create_payload(
                    type="derived",
                    measure_id=None,
                    granularity=None,
                    definition_json={
                        "dependencies": ["sales_gmv_amount_daily"],
                        "expression": "sales_gmv_amount_daily",
                    },
                    mount={
                        "source_table": "dwd.sales_detail",
                        "source_column": "gmv",
                        "granularity": "日",
                        "default_period": "day",
                        "domain": "sales",
                    },
                )
            ),
            owner_id=1,
        )

    # 粒度由 mount 回填到 metric 主表（冗余供列表展示）
    captured = repo.create.call_args[0][0]
    assert captured.granularity == "日"
    # mount 的 source_table/measure_column 并入 definition_json（血缘等旧读者读取）
    assert captured.definition_json["source_table"] == "dwd.sales_detail"
    assert captured.definition_json["measure_column"] == "gmv"
    # metric_mount 落库（同事务 flush），metric_id 取新建指标 id
    saved = mrepo_cls.return_value.save.await_args.args[0]
    assert saved.metric_id == 1
    assert saved.source_table == "dwd.sales_detail"
    assert saved.granularity == "日"


async def test_create_derived_without_mount_no_mount_created():
    """派生指标未携带 mount → 不创建 metric_mount（挂载可后续经挂载 API 补充）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric(type="derived"))
    repo.create_version = AsyncMock(return_value=MagicMock())

    with patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls:
        mrepo_cls.return_value.save = AsyncMock()
        await svc.create_metric(
            MetricCreateRequest(
                **make_create_payload(
                    type="derived",
                    measure_id=None,
                    definition_json={
                        "dependencies": ["sales_gmv_amount_daily"],
                        "expression": "sales_gmv_amount_daily",
                    },
                )
            ),
            owner_id=1,
        )
    mrepo_cls.return_value.save.assert_not_awaited()


async def test_create_derived_base_atomic_validated():
    """派生指标 base_atomic：存在且为原子类型 → 创建通过（OneData 基础原子绑定）。"""
    svc, repo = _svc_with_repo()
    base = make_metric(metric_code="active_doctor_daily", type="atomic")
    repo.get_by_code = AsyncMock(
        side_effect=lambda code: base if code == "active_doctor_daily" else None
    )
    repo.create = AsyncMock(return_value=make_metric(type="derived"))
    repo.create_version = AsyncMock(return_value=MagicMock())

    with patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls:
        mrepo_cls.return_value.save = AsyncMock()
        created = await svc.create_metric(
            MetricCreateRequest(
                **make_create_payload(
                    type="derived",
                    measure_id=None,
                    definition_json={
                        "expression": "SUM(gmv)",
                        "base_atomic": "active_doctor_daily",
                    },
                )
            ),
            owner_id=1,
        )
    assert created is not None


async def test_create_derived_base_atomic_not_atomic_rejected():
    """base_atomic 指向非原子类型指标 → 422 拒绝（BASE_ATOMIC_NOT_ATOMIC）。"""
    svc, repo = _svc_with_repo()
    base = make_metric(metric_code="some_derived", type="derived")
    repo.get_by_code = AsyncMock(
        side_effect=lambda code: base if code == "some_derived" else None
    )
    repo.create = AsyncMock(return_value=make_metric(type="derived"))
    repo.create_version = AsyncMock(return_value=MagicMock())

    with pytest.raises(BusinessError) as ei, patch(
        "app.services.metric_mount.repository.MetricMountRepository"
    ):
        await svc.create_metric(
            MetricCreateRequest(
                **make_create_payload(
                    type="derived",
                    measure_id=None,
                    definition_json={
                        "expression": "SUM(gmv)",
                        "base_atomic": "some_derived",
                    },
                )
            ),
            owner_id=1,
        )
    assert ei.value.error_code == "BASE_ATOMIC_NOT_ATOMIC"


async def test_create_derived_base_atomic_not_found_rejected():
    """base_atomic 指向不存在的指标 → 422 拒绝（BASE_ATOMIC_NOT_FOUND）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric(type="derived"))
    repo.create_version = AsyncMock(return_value=MagicMock())

    with pytest.raises(BusinessError) as ei, patch(
        "app.services.metric_mount.repository.MetricMountRepository"
    ):
        await svc.create_metric(
            MetricCreateRequest(
                **make_create_payload(
                    type="derived",
                    measure_id=None,
                    definition_json={
                        "expression": "SUM(gmv)",
                        "base_atomic": "ghost_atomic",
                    },
                )
            ),
            owner_id=1,
        )
    assert ei.value.error_code == "BASE_ATOMIC_NOT_FOUND"


async def test_create_metric_merges_source_table_into_definition():
    """top-level source_table/measure_column 须合入 definition_json（血缘差异同步的消费键）。

    修复前：批量注册/后端构造路径只传 top-level source_table，definition_json 无
    source_table/measure_column → register_metric_from_definition 建不出「指标↔落地表」边
    （与前端单条 buildDefinitionJson 合入 ②源表/度量列的行为不一致）。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.create_metric(
        MetricCreateRequest(
            **make_create_payload(
                source_table="dwd.sales_detail",
                measure_column="order_amount",
                definition_json={"expression": "SUM(order_amount)", "dependencies": []},
            )
        ),
        owner_id=1,
    )

    captured = repo.create.call_args[0][0]
    defn = captured.definition_json
    assert defn["source_table"] == "dwd.sales_detail"
    assert defn["measure_column"] == "order_amount"
    # 显式声明的 source_tables 不被覆盖（保留调用方提供的源表集）
    assert "source_tables" not in defn


async def test_create_atomic_inherits_unit_from_measure():
    """OneData（界限文档 §2.3）：原子指标关联逻辑度量且未传 unit 时，从度量目录 default_unit 继承。

    原子 = 逻辑度量 + 基础统计粒度（日），不绑物理表；单位是逻辑度量的固有属性，注册原子指标
    时无需手选，由度量目录 default_unit 继承。派生/复合缺省物理属性取默认值。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    fake_measure = MagicMock()
    fake_measure.status = "PUBLISHED"
    fake_measure.default_unit = "元"
    svc._measure_repo.get_by_id = AsyncMock(return_value=fake_measure)
    await svc.create_metric(
        MetricCreateRequest(
            **make_create_payload(measure_id=7, unit=None),
        ),
        owner_id=1,
    )

    captured = repo.create.call_args[0][0]
    assert captured.unit == "元"
    # 未显式传的物理属性取默认值
    assert captured.time_semantics == "PERIOD"
    assert captured.freshness == "T1"
    assert captured.dw_layer == "DWD"


async def test_create_unit_defaults_to_times_when_measure_has_no_unit():
    """逻辑度量无 default_unit 时，原子指标 unit 兜底字典合法 TIMES（修复前为非法 cnt）。

    修复：auto_fill 计数推断与 create_metric/批量注册兜底 unit 从 COUNT/cnt 统一改为
    TIMES——unit 字典无 COUNT/cnt，此前批量注册计数列（visit_cnt 等）报
    「字典值不存在: unit/COUNT」。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    fake_measure = MagicMock()
    fake_measure.status = "PUBLISHED"
    fake_measure.default_unit = None
    svc._measure_repo.get_by_id = AsyncMock(return_value=fake_measure)
    await svc.create_metric(
        MetricCreateRequest(
            **make_create_payload(measure_id=7, unit=None),
        ),
        owner_id=1,
    )

    captured = repo.create.call_args[0][0]
    assert captured.unit == "TIMES"


async def test_create_atomic_measure_missing_raises():
    """原子关联的逻辑度量不存在 → 422 拦截（防 FK 500，而非静默回退 unit=cnt）。

    修复前：measure_id 指向不存在的度量时静默回退 unit="cnt"，随后 flush 撞
    fk_metric_measure 抛 IntegrityError → 500。现在 create_metric 3a 步显式校验
    度量存在且已发布，未命中直接 ValidationError。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    svc._measure_repo.get_by_id = AsyncMock(return_value=None)
    with pytest.raises(ValidationError) as exc:
        await svc.create_metric(
            MetricCreateRequest(
                **make_create_payload(measure_id=999, unit=None),
            ),
            owner_id=1,
        )
    assert exc.value.error_code == "MEASURE_NOT_FOUND"


async def test_create_atomic_measure_not_published_raises():
    """原子关联的逻辑度量未发布（DRAFT/REVIEW/DEPRECATED）→ 422 拦截。

    度量是原子指标的权威继承源（单位/格式/小数位/口径直接传播到下游），
    草稿/已废弃度量不应被新指标引用（对齐 measure 状态机：PUBLISHED 才可用）。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    for status in ("DRAFT", "REVIEW", "DEPRECATED"):
        fake_measure = MagicMock()
        fake_measure.status = status
        fake_measure.default_unit = "元"
        svc._measure_repo.get_by_id = AsyncMock(return_value=fake_measure)
        with pytest.raises(ValidationError) as exc:
            await svc.create_metric(
                MetricCreateRequest(
                    **make_create_payload(measure_id=7, unit=None),
                ),
                owner_id=1,
            )
        assert exc.value.error_code == "MEASURE_NOT_PUBLISHED"


async def test_create_metric_duplicate_code_raises_conflict():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())

    with pytest.raises(ConflictError):
        await svc.create_metric(MetricCreateRequest(**make_create_payload()), owner_id=1)


async def test_create_metric_marks_pending_conflict_on_precheck_hit():
    """真实预检命中相似口径 → 落 conflict 表 OPEN 记录并挂 pending_conflict 标记。"""
    from app.models.conflict import Conflict, ConflictStatus, ConflictType

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())
    # 已存在口径相同但编码不同 → 预检命中 SAME_DEF_DIFF_NAME（软冲突）。
    # P1-D 口径要素归一：同义口径的 definition_json 要素（dependencies）须对称，
    # 富文本比对（口径+依赖）一致才判同义——不同依赖属不同口径，不误判重复建设。
    existing = make_metric(
        id=9,
        metric_code="sales_gmv_amount_day",
        definition_json={"expression": "SUM(order_amount)", "dependencies": ["fct_order"]},
    )
    repo.list_active_for_conflict = AsyncMock(return_value=[existing])
    updated = make_metric(
        pending_conflict=True,
        pending_conflict_detail={"conflict_type": "same_def_diff_name"},
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)

    open_conflict = Conflict(
        conflict_id="CF-AUTO",
        type=ConflictType.SAME_DEF_DIFF_NAME,
        status=ConflictStatus.OPEN,
        metric_codes={"candidate": created.metric_code, "existing": "sales_gmv_amount_day"},
        similarity_score=0.9,
        severity="soft",
        source="auto",
        reason="口径实质相同但命名各异，建议合并",
        block_publish=False,
        metric_b=9,
    )
    captured: list[Conflict] = []
    with (
        patch(
            "app.services.conflict.repository.ConflictRepository.count_open_for_pair",
            AsyncMock(return_value=0),
        ),
        patch(
            "app.services.conflict.repository.ConflictRepository.create",
            AsyncMock(side_effect=lambda c: (captured.append(c), c)[1]),
        ),
        patch(
            "app.services.conflict.repository.ConflictRepository.get_first_open_for_metric",
            AsyncMock(return_value=open_conflict),
        ),
    ):
        result = await svc.create_metric(MetricCreateRequest(**make_create_payload()), owner_id=1)

    # 自动落库：软冲突也落 conflict 表 OPEN 记录（source=auto）
    assert len(captured) == 1
    assert captured[0].severity == "soft"
    assert captured[0].source == "auto"
    assert captured[0].block_publish is False
    # 按冲突表实际记录挂标记，detail 携带 conflict_id 供定位
    repo.update_with_optimistic_lock.assert_awaited_once()
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["pending_conflict"] is True
    assert kwargs["pending_conflict_detail"]["conflict_type"] == "same_def_diff_name"
    assert kwargs["pending_conflict_detail"]["conflict_id"] == "CF-AUTO"
    assert kwargs["pending_conflict_detail"]["source"] == "auto"
    assert result.pending_conflict is True


async def test_create_metric_no_flag_when_no_open_conflict():
    """预检无未决冲突 → 不落库也不挂标记（杜绝「有标记无记录」孤儿态）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())
    repo.list_active_for_conflict = AsyncMock(return_value=[make_metric(id=9)])

    captured: list[object] = []
    with (
        patch(
            "app.services.conflict.repository.ConflictRepository.count_open_for_pair",
            AsyncMock(return_value=0),
        ),
        patch(
            "app.services.conflict.repository.ConflictRepository.create",
            AsyncMock(side_effect=lambda c: (captured.append(c), c)[1]),
        ),
        patch(
            "app.services.conflict.repository.ConflictRepository.get_first_open_for_metric",
            AsyncMock(return_value=None),
        ),
    ):
        result = await svc.create_metric(MetricCreateRequest(**make_create_payload()), owner_id=1)

    assert result is created
    assert captured == []
    repo.update_with_optimistic_lock.assert_not_called()


async def test_create_metric_precheck_failure_is_best_effort():
    """预检依赖加载失败（list_active_for_conflict 抛错）→ 不阻断创建，也不抛异常。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())
    repo.list_active_for_conflict = AsyncMock(side_effect=RuntimeError("catalog down"))

    result = await svc.create_metric(MetricCreateRequest(**make_create_payload()), owner_id=1)

    assert result is created
    # 未挂冲突标记
    repo.update_with_optimistic_lock.assert_not_called()


async def test_load_conflict_existing_merges_term_synonyms():
    """缺口1：存量侧把指标绑定的术语同义词并入比对对象（对齐注释承诺，术语表等价生效）。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(id=9, metric_code="outp_register_person_cnt_day", term_id=7)
    repo.list_active_for_conflict = AsyncMock(return_value=[existing])

    async def fake_execute(stmt, *args, **kwargs):
        return MagicMock(all=MagicMock(return_value=[(7, ["门诊挂号人次", "门诊挂号人数"])]))

    svc._db.execute = fake_execute

    result = await svc.load_conflict_existing()
    assert len(result) == 1
    assert result[0].metric_code == "outp_register_person_cnt_day"
    assert "门诊挂号人次" in result[0].synonyms
    assert "门诊挂号人数" in result[0].synonyms


async def test_precheck_merges_term_synonyms_and_uses_llm():
    """缺口1+3：候选侧并入术语同义词 + 创建路径启用 LLM 补位（use_llm=True）。

    创建时候选 term_id 为 None（术语在创建后单独绑定），但更新口径路径（P2-I）
    候选已绑术语——候选侧对称接入，与存量侧共同杜绝术语等价的检测盲区。
    """
    from app.services.conflict.schemas import ConflictCheckResult, MetricInput

    svc, repo = _svc_with_repo()
    metric = make_metric(term_id=7)
    definition = {"expression": "count(register_id)", "source_tables": []}
    repo.list_active_for_conflict = AsyncMock(return_value=[])

    async def fake_execute(stmt, *args, **kwargs):
        # SQLAlchemy Result 的 scalar_one_or_none/all 是同步方法
        return MagicMock(
            scalar_one_or_none=lambda: ["门诊挂号人次", "门诊挂号人数"],
            all=lambda: [],
        )

    svc._db.execute = fake_execute

    captured: list[MetricInput] = []
    captured_use_llm: list[bool] = []

    async def fake_check(self, candidate, existing, *, use_llm=False, source="auto"):
        captured.append(candidate)
        captured_use_llm.append(use_llm)
        return ConflictCheckResult(detections=[], blocked=False)

    with patch("app.services.conflict.service.ConflictService.check", fake_check):
        await svc._detect_and_mark_conflicts(metric, definition)

    assert len(captured) == 1
    assert "门诊挂号人次" in captured[0].synonyms
    assert "门诊挂号人数" in captured[0].synonyms
    # 缺口3：创建/更新路径启用 LLM 语义补位（对齐人工预检）
    assert captured_use_llm == [True]


async def test_get_metric_not_found():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError):
        await svc.get_metric("missing")


async def test_update_metric_creates_version_and_bumps():
    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=1, version=1)
    repo.get_by_code = AsyncMock(return_value=existing)
    updated = make_metric(status="DRAFT", row_version=2, version=2)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    repo.create_version = AsyncMock(return_value=MagicMock())

    result = await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            definition_json=existing.definition_json,
            change_reason="口径不变",
        ),
        actor_id=1,
        role="metric_owner",
    )

    # 乐观锁使用原始 row_version
    call_args = repo.update_with_optimistic_lock.call_args
    assert call_args.args[1] == 1
    # 提供 definition_json 时创建版本快照，change_type 为 UPDATE（非破坏性）
    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.change_type == "UPDATE"
    assert version_arg.version == 2
    assert result.row_version == 2
    assert result.version == 2


async def test_update_metric_rejects_missing_measure():
    """OneData 校验：更新关联逻辑度量不存在 → 拒绝（防 FK 500）。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=1, version=1)
    repo.get_by_code = AsyncMock(return_value=existing)
    measure_repo = MagicMock()
    measure_repo.get_by_id = AsyncMock(return_value=None)
    svc._measure_repo = measure_repo

    with pytest.raises(ValidationError) as exc:
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(measure_id=999, change_reason="更换逻辑度量"),
            actor_id=1,
            role="metric_owner",
        )
    assert exc.value.error_code == "MEASURE_NOT_FOUND"


async def test_update_metric_rejects_unpublished_measure():
    """OneData 校验：原子指标关联的逻辑度量未发布 → 拒绝（权威继承源须已发布）。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=1, version=1)
    repo.get_by_code = AsyncMock(return_value=existing)
    measure_repo = MagicMock()
    draft_measure = MagicMock()
    draft_measure.status = "DRAFT"
    measure_repo.get_by_id = AsyncMock(return_value=draft_measure)
    svc._measure_repo = measure_repo

    with pytest.raises(ValidationError) as exc:
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(measure_id=2, change_reason="更换逻辑度量"),
            actor_id=1,
            role="metric_owner",
        )
    assert exc.value.error_code == "MEASURE_NOT_PUBLISHED"


async def test_update_metric_accepts_published_measure():
    """OneData 校验：关联逻辑度量存在且已发布 → 正常更新并收集 measure_id（破坏性口径变更）。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=1, version=1)
    repo.get_by_code = AsyncMock(return_value=existing)
    updated = make_metric(status="DRAFT", row_version=2, version=2)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    repo.create_version = AsyncMock(return_value=MagicMock())

    result = await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(measure_id=1, change_reason="更换逻辑度量"),
        actor_id=1,
        role="metric_owner",
    )
    # measure_id 被收集进更新字段（后端 BREAKING_TOP_LEVEL_FIELDS 含 measure_id）
    call_args = repo.update_with_optimistic_lock.call_args
    assert call_args.kwargs.get("measure_id") == 1
    assert result.row_version == 2


async def test_update_metric_definition_change_triggers_conflict_recheck():
    """P2-I：口径变更后触发冲突重检（best-effort，不阻断更新）。

    原实现仅在创建时检测一次——指标改口径后与其它指标"后来变得同义"无法发现。
    """
    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=1, version=1)
    repo.get_by_code = AsyncMock(return_value=existing)
    updated = make_metric(status="DRAFT", row_version=2, version=2)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    repo.create_version = AsyncMock(return_value=MagicMock())
    svc._detect_and_mark_conflicts = AsyncMock(return_value=updated)

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            definition_json={"expression": "SUM(order_amount)", "dependencies": ["fct_order"]},
            change_reason="口径更新",
        ),
        actor_id=1,
        role="metric_owner",
    )
    # 重检被触发：候选为更新后的指标（含新口径）
    svc._detect_and_mark_conflicts.assert_awaited_once()
    args = svc._detect_and_mark_conflicts.await_args
    assert args.args[0].metric_code == "sales_gmv_daily"
    assert args.args[1]["expression"] == "SUM(order_amount)"


async def test_update_metric_no_definition_change_skips_conflict_recheck():
    """P2-I：非口径变更（仅责任方调整）不触发冲突重检，避免无谓扫描。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=1, version=1, product_owner_id=3)
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=1)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    svc._detect_and_mark_conflicts = AsyncMock(return_value=existing)

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            product_owner_id=None,
            product_owner_name="外部需求方A",
            change_reason="责任方调整",
        ),
        actor_id=1,
        role="metric_owner",
    )
    svc._detect_and_mark_conflicts.assert_not_called()


async def test_load_conflict_existing_returns_active_metrics_with_features():
    """P0-A/P1-F/G/P2-K：load_conflict_existing 返回活动指标（含 definition_json + 同义词）。

    供手动预检（existing 为空时服务端自动加载）与创建/更新自动预检复用；
    不再走 ``list_metrics(limit=1000)``（修复 DEPRECATED 参与比对与截断漏检）。
    """
    svc, repo = _svc_with_repo()
    m = make_metric(
        id=9,
        metric_code="sales_gmv_amount_day",
        measure_id=1,
        definition_json={"expression": "SUM(order_amount)", "dependencies": ["fct_order"]},
    )
    repo.list_active_for_conflict = AsyncMock(return_value=[m])
    rows = await svc.load_conflict_existing()
    assert len(rows) == 1
    assert rows[0].metric_code == "sales_gmv_amount_day"
    # P1-D：完整 definition_json 透传供富文本比对
    assert rows[0].definition_json == {
        "expression": "SUM(order_amount)",
        "dependencies": ["fct_order"],
    }
    # P2-K：同义词字段默认空（无度量目录同义词时）
    assert rows[0].synonyms == []
    # P1-F/G：走专用全量加载（非 list_metrics limit=1000）
    repo.list_active_for_conflict.assert_awaited_once()


# ---- 口径三方责任：外部人员名称兜底 + 显式置空（PRD 4.5 补充）----
# 白名单循环 `if val is not None` 会跳过显式 null，故 id/name 成对走 model_fields_set 专用块。
# 覆盖三态：平台用户→外部人员切换（置空 id 写 name）、完全解除（双 null）、未提交（保留旧值）。


async def test_update_metric_switch_responsibility_to_external_name():
    """平台用户 → 外部人员切换：显式置空 id、写入 name（白名单循环会跳过 null，须专用块生效）。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=1, version=1, product_owner_id=3)
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=1)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            product_owner_id=None,
            product_owner_name="外部需求方A",
            change_reason="责任方改为外部人员",
        ),
        actor_id=1,
        role="metric_owner",
    )

    lock_kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert lock_kwargs["product_owner_id"] is None
    assert lock_kwargs["product_owner_name"] == "外部需求方A"


async def test_update_metric_clear_responsibility():
    """完全解除责任方：id/name 显式置空 → 两者都写入 None（旧值不残留）。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="DRAFT",
        row_version=1,
        version=1,
        product_owner_id=3,
        product_owner_name="外部需求方A",
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=1)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            product_owner_id=None,
            product_owner_name=None,
            change_reason="解除产品需求方责任",
        ),
        actor_id=1,
        role="metric_owner",
    )

    lock_kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert lock_kwargs["product_owner_id"] is None
    assert lock_kwargs["product_owner_name"] is None


async def test_update_metric_untouched_responsibility_kept():
    """未提交责任方字段 → 不进入 updates（保留旧值）。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="DRAFT",
        row_version=1,
        version=1,
        product_owner_id=3,
        product_owner_name="外部需求方A",
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=1)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(change_reason="仅改名称"),
        actor_id=1,
        role="metric_owner",
    )

    lock_kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert "product_owner_id" not in lock_kwargs
    assert "product_owner_name" not in lock_kwargs


async def test_update_metric_published_non_breaking_syncs_effective_version():
    """P8 非破坏性 PUBLISHED 编辑：主表 version 与 effective_version 同步。

    修复前非破坏编辑只递增 version、不写 effective_version → 版本号与生效版本
    矛盾（出现永不转正的 DRAFT 版本）。非破坏性口径变更直接生效，生效版本=最新。
    """
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED", row_version=1, version=2, effective_version=2
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", version=3, effective_version=3)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            # 非破坏性：改 source_fields（不在 BREAKING_DEF_FIELDS），expression 不变
            definition_json={**existing.definition_json, "source_fields": ["gmv", "channel"]},
            change_reason="补充来源字段（非破坏性）",
        ),
        actor_id=1,
        role="metric_owner",
    )

    lock_kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    # 非破坏性直接生效 → effective_version 同步到最新版本号
    assert lock_kwargs["effective_version"] == 3
    assert lock_kwargs["version"] == 3


async def test_update_metric_blocked_by_pdp_decision():
    """PDP 拒绝（check_metric_permission allow=False）→ 写操作被阻断，不落库。

    场景：actor 为 owner（通过 _assert_owner_or_admin），但 PDP 以跨域越权拒绝——
    验证 PDP 作为独立安全闸门在 owner 校验之上再拦截。
    """
    mock_gov_svc = MagicMock()
    mock_gov_svc.check_metric_permission = AsyncMock(
        return_value=Decision(allow=False, reason="跨域越权", error_code="FORBIDDEN_DOMAIN")
    )
    with patch("app.services.semantic.service.MetricRepository") as mock_repo_cls:
        svc = MetricService(db=MagicMock(), governance_svc=mock_gov_svc)
        repo = mock_repo_cls.return_value
        repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", row_version=1))
        repo.update_with_optimistic_lock = AsyncMock()

        with pytest.raises(BusinessError) as exc:
            await svc.update_metric(
                "sales_gmv_daily",
                MetricUpdateRequest(
                    definition_json={"expression": "SUM(order_amount)"},
                    change_reason="跨域尝试",
                ),
                actor_id=1,  # owner：通过 _assert_owner_or_admin
                role="metric_owner",
                user_domain="other_domain",
            )

        assert exc.value.error_code == "FORBIDDEN_DOMAIN"
        mock_gov_svc.check_metric_permission.assert_awaited_once()
        repo.update_with_optimistic_lock.assert_not_awaited()


async def test_update_metric_breaking_change():
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="DRAFT",
        row_version=1,
        version=1,
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    updated = make_metric(status="DRAFT", row_version=2, version=2)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            definition_json={"expression": "SUM(refund_amount)"},
            change_reason="口径变更",
        ),
        actor_id=1,
        role="metric_owner",
    )

    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.change_type == "BREAKING"
    assert version_arg.version == 2


# ---- OneData 挂载层：PUBLISHED 派生指标改挂载粒度/源表 → PENDING 确认流（Phase 4 接入）----


def _mount_with(granularity: str = "日", source_table: str = "dwd.sales_detail") -> MagicMock:
    """构造已有 metric_mount mock（默认日粒度挂载 dwd.sales_detail）。"""
    m = MagicMock()
    m.id = 1
    m.granularity = granularity
    m.source_table = source_table
    m.source_column = "gmv"
    m.default_period = "day"
    m.domain = "sales"
    m.business_filter = None
    # 显式粒度维度（组合粒度，方案 B）：None = 纯时间粒度——不设会触发 MagicMock
    # 自动属性，在 _sync_mounts 的 granularity_dims 比较中误判为破坏性变更
    m.granularity_dims = None
    return m


def _mount_update(granularity: str = "月") -> MetricUpdateRequest:
    """构造派生指标挂载更新请求（默认把粒度从日改月）。"""
    return MetricUpdateRequest(
        mount={
            "source_table": "dwd.sales_detail",
            "source_column": "gmv",
            "granularity": granularity,
            "default_period": "month",
            "domain": "sales",
        },
        change_reason="挂载粒度从日改月",
    )


async def test_update_published_derived_mount_granularity_pending():
    """PUBLISHED 派生指标改挂载粒度 → 创建 PENDING_CONFIRMATION 版本，不直接生效。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="PUBLISHED", type="derived", granularity="日", version=3,
        definition_json={"dependencies": ["sales_gmv_amount_daily"], "expression": "x"},
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    existing_mount = _mount_with()
    repo.create_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=metric)
    svc._cache.invalidate = AsyncMock()

    with (
        patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls,
        patch("app.services.semantic.pending_version_manager.PendingVersionManager") as pvm_cls,
    ):
        mrepo_cls.return_value.list_by_metric = AsyncMock(return_value=[existing_mount])
        pvm_cls.return_value.create_pending = AsyncMock()
        await svc.update_metric(
            "sales_gmv_daily", _mount_update(), actor_id=1, role="metric_owner"
        )

    # 创建 BREAKING + PENDING_CONFIRMATION 版本，diff_json 携带 mount_change
    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.change_type == "BREAKING"
    assert version_arg.status == "PENDING_CONFIRMATION"
    assert version_arg.version == 4
    assert version_arg.diff_json["granularity"]["after"] == "月"
    assert version_arg.diff_json["granularity"]["mount_change"] is True
    # PendingVersionManager.create_pending 被调用（消费方确认流）
    pvm_cls.return_value.create_pending.assert_awaited_once()
    # 不直接更新主表 granularity / mount 实体（等确认后生效）
    lock_kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert "granularity" not in lock_kwargs
    assert existing_mount.granularity == "日"


async def test_update_published_derived_mount_granularity_dims_change_breaking():
    """PUBLISHED 派生指标挂载粒度维度变化（组合粒度唯一性构成，方案 B）→ 破坏性 PENDING。

    与改粒度/源表同级：粒度维度是唯一性构成者（消费 SQL 固定进 GROUP BY），
    变更须经消费方确认（PENDING_VERSION，14 天）。
    """
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="PUBLISHED", type="derived", granularity="月", version=3,
        definition_json={"dependencies": ["sales_gmv_amount_daily"], "expression": "x"},
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    existing_mount = _mount_with()
    repo.create_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=metric)
    svc._cache.invalidate = AsyncMock()

    with (
        patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls,
        patch("app.services.semantic.pending_version_manager.PendingVersionManager") as pvm_cls,
    ):
        mrepo_cls.return_value.list_by_metric = AsyncMock(return_value=[existing_mount])
        pvm_cls.return_value.create_pending = AsyncMock()
        req = MetricUpdateRequest(
            mount={
                "source_table": "dwd.sales_detail",
                "source_column": "gmv",
                "granularity": "月",
                "granularity_dims": ["hospital"],
                "default_period": "month",
                "domain": "sales",
            },
            change_reason="挂载粒度维度从无变医院",
        )
        await svc.update_metric("sales_gmv_daily", req, actor_id=1, role="metric_owner")

    # 创建 BREAKING + PENDING_CONFIRMATION 版本，diff_json 携带 granularity_dims
    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.change_type == "BREAKING"
    assert version_arg.status == "PENDING_CONFIRMATION"
    assert version_arg.diff_json["granularity_dims"]["after"] == ["hospital"]
    assert version_arg.diff_json["granularity_dims"]["mount_change"] is True
    pvm_cls.return_value.create_pending.assert_awaited_once()
    # 不直接更新 mount 实体（等确认后生效）
    assert existing_mount.granularity_dims is None


async def test_update_published_derived_mount_pending_exists():
    """PENDING 确认期内再次改挂载粒度 → 拒绝（防叠加破坏性变更）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="PUBLISHED", type="derived", granularity="日", version=3,
        definition_json={"dependencies": ["sales_gmv_amount_daily"], "expression": "x"},
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.has_pending_version = AsyncMock(return_value=True)
    repo.create_version = AsyncMock(return_value=MagicMock())

    with (
        patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls,
        pytest.raises(ConflictError) as exc,
    ):
        mrepo_cls.return_value.list_by_metric = AsyncMock(return_value=[_mount_with()])
        await svc.update_metric(
            "sales_gmv_daily", _mount_update(), actor_id=1, role="metric_owner"
        )
    assert exc.value.error_code == "METRIC_PENDING_VERSION_EXISTS"
    repo.create_version.assert_not_awaited()


async def test_update_draft_derived_mount_granularity_applies():
    """DRAFT 派生指标改挂载粒度 → 直接应用（不触发 PENDING）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="DRAFT", type="derived", granularity="日", version=1,
        definition_json={"dependencies": ["sales_gmv_amount_daily"], "expression": "x"},
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    existing_mount = _mount_with()
    repo.create_version = AsyncMock()
    repo.update_with_optimistic_lock = AsyncMock(return_value=metric)
    svc._cache.invalidate = AsyncMock()
    svc._db.flush = AsyncMock()

    with patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls:
        mrepo_cls.return_value.list_by_metric = AsyncMock(return_value=[existing_mount])
        await svc.update_metric(
            "sales_gmv_daily", _mount_update(), actor_id=1, role="metric_owner"
        )

    # 直接更新主表 granularity 与 mount 实体（无 PENDING 版本创建）
    lock_kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert lock_kwargs["granularity"] == "月"
    assert existing_mount.granularity == "月"
    assert existing_mount.default_period == "month"
    repo.create_version.assert_not_awaited()


async def test_update_published_derived_mount_unchanged_applies():
    """PUBLISHED 派生指标 mount 粒度/源表不变 → 直接应用（不触发 PENDING）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="PUBLISHED", type="derived", granularity="日", version=3,
        definition_json={"dependencies": ["sales_gmv_amount_daily"], "expression": "x"},
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    existing_mount = _mount_with()
    repo.create_version = AsyncMock()
    repo.update_with_optimistic_lock = AsyncMock(return_value=metric)
    svc._cache.invalidate = AsyncMock()
    svc._db.flush = AsyncMock()

    with patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls:
        mrepo_cls.return_value.list_by_metric = AsyncMock(return_value=[existing_mount])
        await svc.update_metric(
            "sales_gmv_daily", _mount_update(granularity="日"), actor_id=1, role="metric_owner"
        )

    # 直接应用，未创建 PENDING 版本
    lock_kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert lock_kwargs["granularity"] == "日"
    repo.create_version.assert_not_awaited()


async def test_promote_pending_version_applies_mount_change():
    """PENDING 确认转正：主表 granularity 回写 + metric_mount 同步（挂载变更确认后生效）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(status="PUBLISHED", type="derived", granularity="日", row_version=5)
    version_obj = MagicMock()
    version_obj.definition_json = metric.definition_json
    version_obj.diff_json = {
        "granularity": {
            "before": "日",
            "after": "月",
            "change_type": "BREAKING",
            "mount_change": True,
        },
    }
    repo.get_version = AsyncMock(return_value=version_obj)
    updated = make_metric(status="PUBLISHED", type="derived", granularity="月", row_version=6)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()
    svc._register_metric_lineage_full = AsyncMock()
    svc._db.flush = AsyncMock()

    mount = _mount_with()
    with patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls:
        mrepo_cls.return_value.get_default_mount = AsyncMock(return_value=mount)
        result = await svc._promote_pending_version(
            metric, version=6, trigger="consumer_confirm", actor_id=1
        )

    # 主表 granularity 回写（BREAKING_TOP_LEVEL_FIELDS 机制）
    lock_kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert lock_kwargs["granularity"] == "月"
    # metric_mount.granularity 同步（挂载变更确认后生效）
    assert mount.granularity == "月"
    assert result is not None


async def test_promote_pending_version_multi_mount_uses_default_mount():
    """多变体（0105 放开一指标多挂载）存量单字段变更转正：走 get_default_mount，
    回写默认变体，不再因 MultipleResultsFound 500。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(status="PUBLISHED", type="derived", granularity="日", row_version=5)
    version_obj = MagicMock()
    version_obj.definition_json = metric.definition_json
    version_obj.diff_json = {
        "source_table": {
            "before": "dwd.sales_detail",
            "after": "dwd.sales_detail_v2",
            "change_type": "BREAKING",
            "mount_change": True,
        },
    }
    repo.get_version = AsyncMock(return_value=version_obj)
    updated = make_metric(status="PUBLISHED", type="derived", granularity="日", row_version=6)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()
    svc._register_metric_lineage_full = AsyncMock()
    svc._db.flush = AsyncMock()

    default_mount = _mount_with(source_table="dwd.sales_detail")
    with patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls:
        mrepo_cls.return_value.get_default_mount = AsyncMock(return_value=default_mount)
        await svc._promote_pending_version(
            metric, version=6, trigger="consumer_confirm", actor_id=1
        )

    # 默认变体挂载行 source_table 回写（多挂载下不再抛 MultipleResultsFound）
    mrepo_cls.return_value.get_default_mount.assert_awaited_once_with(metric.id)
    assert default_mount.source_table == "dwd.sales_detail_v2"


async def test_create_derived_multi_mount_persists_all():
    """2026-08-27 多变体：派生指标一次创建传 mounts 列表 → 每行落 metric_mount（粒度/限定各异）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric(type="derived")
    created.id = 1
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    with patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls:
        mrepo_cls.return_value.save = AsyncMock(side_effect=lambda m: m)
        await svc.create_metric(
            MetricCreateRequest(
                **make_create_payload(
                    type="derived",
                    measure_id=None,
                    granularity=None,
                    definition_json={
                        "dependencies": ["sales_gmv_amount_daily"],
                        "expression": "sales_gmv_amount_daily",
                    },
                    mounts=[
                        {
                            "source_table": "dwd.doctor_fee_daily",
                            "source_column": "fee",
                            "granularity": "医生",
                            "default_period": "day",
                            "domain": "medical",
                        },
                        {
                            "source_table": "dwd.hospital_fee",
                            "source_column": "fee",
                            "granularity": "医院",
                            "default_period": "day",
                            "domain": "medical",
                            "business_filter": "场景=住院",
                        },
                    ],
                )
            ),
            owner_id=1,
        )

    # 默认变体粒度回填：default_period 行优先（医生行在前）
    captured = repo.create.call_args[0][0]
    assert captured.granularity == "医生"
    # 每行均落库（2 个变体），business_filter 透传
    saved = [c.args[0] for c in mrepo_cls.return_value.save.await_args_list]
    assert len(saved) == 2
    assert [s.granularity for s in saved] == ["医生", "医院"]
    assert saved[1].business_filter == "场景=住院"
    assert all(s.metric_id == 1 for s in saved)


async def test_update_draft_derived_mounts_diff_align():
    """DRAFT 派生指标 mounts 全量 diff：带 id 更新 + 无 id 新增 + 未出现软删。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="DRAFT", type="derived", granularity="日", version=1,
        definition_json={"dependencies": ["sales_gmv_amount_daily"], "expression": "x"},
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    m1 = _mount_with()
    m2 = MagicMock()
    m2.id = 2
    m2.source_table = "dwd.hospital_fee"
    m2.source_column = "fee"
    m2.granularity = "医院"
    m2.default_period = "day"
    m2.domain = "medical"
    m2.business_filter = None
    repo.update_with_optimistic_lock = AsyncMock(return_value=metric)
    svc._cache.invalidate = AsyncMock()
    svc._db.flush = AsyncMock()
    repo.create_version = AsyncMock()

    with patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls:
        mrepo_cls.return_value.list_by_metric = AsyncMock(return_value=[m1, m2])
        mrepo_cls.return_value.soft_delete = AsyncMock()
        mrepo_cls.return_value.save = AsyncMock(side_effect=lambda m: m)
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                mounts=[
                    {
                        "id": 1, "source_table": "dwd.sales_detail", "source_column": "gmv",
                        "granularity": "月", "default_period": "month", "domain": "sales",
                    },
                    {
                        "source_table": "dwd.drug_fee", "source_column": "fee",
                        "granularity": "药品", "default_period": "day", "domain": "medical",
                    },
                ],
                change_reason="多变体对齐",
            ),
            actor_id=1,
            role="metric_owner",
        )
    # m1 更新（粒度月）、m2 软删、新增药品行
    assert m1.granularity == "月"
    mrepo_cls.return_value.soft_delete.assert_awaited_once_with(2)
    assert mrepo_cls.return_value.save.await_args.args[0].granularity == "药品"
    # 默认变体粒度回填（month 行优先）
    lock_kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert lock_kwargs["granularity"] == "月"


async def test_update_published_derived_mounts_add_variant_non_breaking():
    """PUBLISHED 派生指标新增变体（新挂载行无 id）→ 非破坏，直接应用不触发 PENDING。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="PUBLISHED", type="derived", granularity="日", version=3,
        definition_json={"dependencies": ["sales_gmv_amount_daily"], "expression": "x"},
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    existing = _mount_with()
    repo.create_version = AsyncMock()
    repo.update_with_optimistic_lock = AsyncMock(return_value=metric)
    svc._cache.invalidate = AsyncMock()
    svc._db.flush = AsyncMock()

    with patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls:
        mrepo_cls.return_value.list_by_metric = AsyncMock(return_value=[existing])
        mrepo_cls.return_value.save = AsyncMock(side_effect=lambda m: m)
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                mounts=[
                    {
                        "id": 1, "source_table": "dwd.sales_detail", "source_column": "gmv",
                        "granularity": "日", "default_period": "day", "domain": "sales",
                    },
                    {
                        "source_table": "dwd.hospital_fee", "source_column": "fee",
                        "granularity": "医院", "default_period": "day", "domain": "medical",
                        "business_filter": "场景=住院",
                    },
                ],
                change_reason="新增医院变体",
            ),
            actor_id=1,
            role="metric_owner",
        )
    # 新增变体非破坏：无 PENDING 版本，直接落库
    repo.create_version.assert_not_awaited()
    lock_kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert lock_kwargs["granularity"] == "日"  # 默认变体仍是医生行
    assert mrepo_cls.return_value.save.await_args.args[0].business_filter == "场景=住院"


async def test_update_published_derived_mounts_remove_variant_breaking():
    """PUBLISHED 派生指标删除变体（挂载行不在请求）→ 破坏性 PENDING + diff_json
    携带 mounts 快照。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="PUBLISHED", type="derived", granularity="医生", version=3,
        definition_json={"dependencies": ["sales_gmv_amount_daily"], "expression": "x"},
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    m1 = _mount_with(granularity="医生")
    m2 = MagicMock()
    m2.id = 2
    m2.source_table = "dwd.hospital_fee"
    m2.source_column = "fee"
    m2.granularity = "医院"
    m2.default_period = "day"
    m2.domain = "medical"
    m2.business_filter = None
    repo.create_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=metric)
    repo.has_pending_version = AsyncMock(return_value=False)
    svc._cache.invalidate = AsyncMock()

    with (
        patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls,
        patch("app.services.semantic.pending_version_manager.PendingVersionManager") as pvm_cls,
    ):
        mrepo_cls.return_value.list_by_metric = AsyncMock(return_value=[m1, m2])
        pvm_cls.return_value.create_pending = AsyncMock()
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                mounts=[
                    {
                        "id": 1, "source_table": "dwd.sales_detail", "source_column": "gmv",
                        "granularity": "医生", "default_period": "day", "domain": "sales",
                    },
                ],
                change_reason="下线医院变体",
            ),
            actor_id=1,
            role="metric_owner",
        )
    # 删除 m2 → 破坏性：创建 BREAKING + PENDING_CONFIRMATION 版本，diff_json 携带 mounts 快照
    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.change_type == "BREAKING"
    assert version_arg.status == "PENDING_CONFIRMATION"
    assert "mounts" in version_arg.diff_json
    assert len(version_arg.diff_json["mounts"]["before"]) == 2
    assert version_arg.diff_json["mounts"]["after"][0]["id"] == 1
    pvm_cls.return_value.create_pending.assert_awaited_once()
    # 不直接改挂载（等确认后转正）——未执行软删
    mrepo_cls.return_value.soft_delete.assert_not_called()


async def test_promote_pending_version_applies_mounts_snapshot():
    """PENDING 转正按 diff_json["mounts"] 快照全量对齐挂载列表（多变体破坏性变更确认后生效）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(status="PUBLISHED", type="derived", granularity="医生", row_version=5)
    version_obj = MagicMock()
    version_obj.definition_json = metric.definition_json
    version_obj.diff_json = {
        "mounts": {
            "before": [
                {
                    "id": 1, "source_table": "dwd.sales_detail", "source_column": "gmv",
                    "granularity": "医生", "default_period": "day", "domain": "sales",
                    "business_filter": None,
                },
                {
                    "id": 2, "source_table": "dwd.hospital_fee", "source_column": "fee",
                    "granularity": "医院", "default_period": "day", "domain": "medical",
                    "business_filter": "场景=住院",
                },
            ],
            "after": [
                {
                    "id": 1, "source_table": "dwd.sales_detail", "source_column": "gmv",
                    "granularity": "医生", "default_period": "day", "domain": "sales",
                    "business_filter": None,
                },
                {
                    "id": 3, "source_table": "dwd.drug_fee", "source_column": "fee",
                    "granularity": "药品", "default_period": "day", "domain": "medical",
                    "business_filter": "场景=门特",
                },
            ],
            "change_type": "BREAKING",
            "mount_change": True,
        },
    }
    repo.get_version = AsyncMock(return_value=version_obj)
    updated = make_metric(status="PUBLISHED", type="derived", row_version=6)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()
    svc._register_metric_lineage_full = AsyncMock()
    svc._db.flush = AsyncMock()

    m1 = _mount_with(granularity="医生")
    m2 = MagicMock()
    m2.id = 2
    m2.source_table = "dwd.hospital_fee"
    m2.source_column = "fee"
    m2.granularity = "医院"
    m2.default_period = "day"
    m2.domain = "medical"
    m2.business_filter = "场景=住院"
    with patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls:
        mrepo_cls.return_value.list_by_metric = AsyncMock(return_value=[m1, m2])
        mrepo_cls.return_value.soft_delete = AsyncMock()
        mrepo_cls.return_value.save = AsyncMock(side_effect=lambda m: m)
        result = await svc._promote_pending_version(
            metric, version=6, trigger="consumer_confirm", actor_id=1
        )
    # 快照对齐：m2 软删、新增 id=3 药品行、m1 保持
    mrepo_cls.return_value.soft_delete.assert_awaited_once_with(2)
    new_mount = mrepo_cls.return_value.save.await_args.args[0]
    assert new_mount.source_table == "dwd.drug_fee"
    assert new_mount.business_filter == "场景=门特"
    # 目标粒度回填主表（第二次 update_with_optimistic_lock）
    assert repo.update_with_optimistic_lock.call_count == 2
    assert result is not None


async def test_update_metric_invalid_status_rejected():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DEPRECATED"))

    with pytest.raises(BusinessError):
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(change_reason="修正名称"),
            actor_id=1,
            role="metric_owner",
        )


async def test_approve_metric_clears_reject_reason():
    """指标一经发布清空历史驳回原因（生命周期闭环，approve_metric 统一路径）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="REVIEW",
            pii_flag=False,
            reject_reason="粒度与口径不符",
            reject_reviewer_id=3,
            rejected_at="2026-08-01 10:00:00",
        )
    )
    published = make_metric(status="PUBLISHED")
    repo.update_with_optimistic_lock = AsyncMock(return_value=published)
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()
    svc._notify_metric_stakeholders = AsyncMock()

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(),
        actor_id=1,
        role="domain_admin",
    )

    assert result.status == "PUBLISHED"
    called = repo.update_with_optimistic_lock.call_args.kwargs
    assert called["reject_reason"] is None
    assert called["reject_reviewer_id"] is None
    assert called["rejected_at"] is None


async def test_approve_metric_invalid_status_rejected():
    """DRAFT 直接发布非法（须先 submit→REVIEW，再 approve→PUBLISHED）。"""
    svc, repo = _svc_with_repo()
    # DRAFT 直接发布非法（须先 submit→REVIEW，再 approve→PUBLISHED）
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT"))

    with pytest.raises(BusinessError):
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(),
            actor_id=1,
            role="metric_owner",
        )


async def test_deprecate_metric_success_sets_sunset():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    deprecated = make_metric(status="DEPRECATED", successor_code="sales_gmv_v2")
    repo.update_with_optimistic_lock = AsyncMock(return_value=deprecated)

    result = await svc.deprecate_metric(
        "sales_gmv_daily", "sales_gmv_v2", actor_id=1, role="metric_owner"
    )

    assert result.status == "DEPRECATED"
    called = repo.update_with_optimistic_lock.call_args.kwargs
    assert called["successor_code"] == "sales_gmv_v2"
    assert called["sunset_until"] is not None


async def test_deprecate_metric_self_successor_rejected():
    """自废弃防护：替代指标为指标自身时拒绝（废弃链不应指向自身，语义矛盾）。"""
    import pytest as _pytest

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))

    with _pytest.raises(BusinessError) as exc:
        await svc.deprecate_metric(
            "sales_gmv_daily", "sales_gmv_daily", actor_id=1, role="metric_owner"
        )
    assert exc.value.error_code == "VALIDATION_ERROR"
    # 自废弃在替代指标查询之前拦截：get_by_code 仅被调用一次（取指标自身），未查替代
    repo.get_by_code.assert_awaited_once()


async def test_deprecate_metric_blocked_by_pdp_cross_domain():
    """domain_admin 跨域废弃被 PDP 拒绝（deprecate 补 PDP 域校验，修复域隔离漏洞）。

    场景：domain_admin 已通过 _assert_owner_or_admin（admin 放行），但 PDP 以跨域越权
    拒绝——验证 deprecate 与 update/approve 一致的域权限闸门生效。
    """
    mock_gov_svc = MagicMock()
    mock_gov_svc.check_metric_permission = AsyncMock(
        return_value=Decision(
            allow=False, reason="跨域越权，无权废弃他域指标", error_code="FORBIDDEN_DOMAIN"
        )
    )
    with patch("app.services.semantic.service.MetricRepository") as mock_repo_cls:
        svc = MetricService(db=MagicMock(), governance_svc=mock_gov_svc)
        repo = mock_repo_cls.return_value
        repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
        repo.update_with_optimistic_lock = AsyncMock()

        with pytest.raises(BusinessError) as exc:
            await svc.deprecate_metric(
                "sales_gmv_daily",
                "sales_gmv_v2",
                actor_id=2,
                role="domain_admin",
                user_domain="finance",  # 跨域：指标在 sales 域
            )
        assert exc.value.error_code == "FORBIDDEN_DOMAIN"
        repo.update_with_optimistic_lock.assert_not_called()


async def test_deprecate_metric_empty_successor_direct_deprecate():
    """空替代指标（空串/None）直接废弃，不触发「替代指标不存在:（空）」误导错误。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    deprecated = make_metric(status="DEPRECATED", successor_code=None)
    repo.update_with_optimistic_lock = AsyncMock(return_value=deprecated)

    # 空串（前端未填替代指标提交）→ 应视为无替代直接废弃
    result = await svc.deprecate_metric("sales_gmv_daily", "", actor_id=1, role="metric_owner")

    assert result.status == "DEPRECATED"
    called = repo.update_with_optimistic_lock.call_args.kwargs
    assert called["successor_code"] is None  # 空串归一化为 None 落库
    # 未因空串触发替代指标不存在错误
    repo.get_by_code.assert_called_once()  # 仅查被废弃指标自身


async def test_deprecate_metric_none_successor_direct_deprecate():
    """None 替代指标直接废弃（无替代下线合法场景）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    deprecated = make_metric(status="DEPRECATED", successor_code=None)
    repo.update_with_optimistic_lock = AsyncMock(return_value=deprecated)

    result = await svc.deprecate_metric("sales_gmv_daily", None, actor_id=1, role="metric_owner")

    assert result.status == "DEPRECATED"
    called = repo.update_with_optimistic_lock.call_args.kwargs
    assert called["successor_code"] is None


async def test_deprecate_metric_already_deprecated_rejected():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DEPRECATED"))

    with pytest.raises(BusinessError):
        await svc.deprecate_metric("sales_gmv_daily", "x", actor_id=1, role="metric_owner")


async def test_deprecate_metric_referenced_without_successor_blocked():
    """被引用拦截：仍被派生/报表引用且未指定替代 → 废弃被拦。

    返回 METRIC_REFERENCED 并列出引用者；废弃被活跃引用的指标会让下游
    DERIVED_FROM/CONSUMED_BY 引用悬空，无替代指标时须先处理下游，避免
    静默破损（与发布端反向保护互补）。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.update_with_optimistic_lock = AsyncMock()
    with (
        patch(
            "app.services.lineage.repository.LineageRepository.metric_referrers",
            new=AsyncMock(
                return_value=[
                    {"node": "metric:sales_gmv_derived", "edge_type": "DERIVED_FROM"},
                    {"node": "consumer:bi_report", "edge_type": "CONSUMED_BY"},
                ]
            ),
        ),
        pytest.raises(BusinessError) as exc,
    ):
        await svc.deprecate_metric("sales_gmv_daily", None, actor_id=1, role="metric_owner")
    assert exc.value.error_code == "METRIC_REFERENCED"
    assert "sales_gmv_derived" in exc.value.message
    repo.update_with_optimistic_lock.assert_not_called()


async def test_deprecate_metric_referenced_with_successor_allowed():
    """被引用但指定替代指标 → 放行（下游可改绑替代，不悬空）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        side_effect=lambda code: make_metric(status="PUBLISHED", metric_code=code)
    )
    deprecated = make_metric(status="DEPRECATED", successor_code="sales_gmv_v2")
    repo.update_with_optimistic_lock = AsyncMock(return_value=deprecated)
    with patch(
        "app.services.lineage.repository.LineageRepository.metric_referrers",
        new=AsyncMock(
            return_value=[{"node": "metric:sales_gmv_derived", "edge_type": "DERIVED_FROM"}]
        ),
    ):
        result = await svc.deprecate_metric(
            "sales_gmv_daily", "sales_gmv_v2", actor_id=1, role="metric_owner"
        )
    assert result.status == "DEPRECATED"
    assert repo.update_with_optimistic_lock.call_args.kwargs["successor_code"] == "sales_gmv_v2"


async def test_deprecate_metric_not_referenced_allowed():
    """无活跃引用者 → 正常废弃，不触发拦截。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    deprecated = make_metric(status="DEPRECATED", successor_code=None)
    repo.update_with_optimistic_lock = AsyncMock(return_value=deprecated)
    with patch(
        "app.services.lineage.repository.LineageRepository.metric_referrers",
        new=AsyncMock(return_value=[]),
    ):
        result = await svc.deprecate_metric(
            "sales_gmv_daily", None, actor_id=1, role="metric_owner"
        )
    assert result.status == "DEPRECATED"


# ---- P2-1：reactivate_metric（DEPRECATED → DRAFT，对齐维度批量重新启用）----


async def test_reactivate_metric_success_clears_deprecation_fields():
    """恢复成功：DEPRECATED → DRAFT，清空替代指标/废弃时间/日落期。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="DEPRECATED",
            successor_code="sales_gmv_v2",
            deprecated_at=datetime(2026, 8, 1, tzinfo=UTC),
            sunset_until=datetime(2026, 9, 1).date(),
        )
    )
    restored = make_metric(status="DRAFT", successor_code=None)
    repo.update_with_optimistic_lock = AsyncMock(return_value=restored)

    result = await svc.reactivate_metric(
        "sales_gmv_daily", actor_id=1, role="metric_owner"
    )

    assert result.status == "DRAFT"
    called = repo.update_with_optimistic_lock.call_args.kwargs
    assert called["status"] == "DRAFT"
    assert called["successor_code"] is None
    assert called["deprecated_at"] is None
    assert called["sunset_until"] is None


async def test_reactivate_metric_non_deprecated_rejected():
    """仅 DEPRECATED 状态可恢复；DRAFT/REVIEW/PUBLISHED 一律拒绝。"""
    for status in ("DRAFT", "REVIEW", "PUBLISHED"):
        svc, repo = _svc_with_repo()
        repo.get_by_code = AsyncMock(return_value=make_metric(status=status))
        with pytest.raises(ConflictError) as exc:
            await svc.reactivate_metric(
                "sales_gmv_daily", actor_id=1, role="metric_owner"
            )
        assert exc.value.error_code == "INVALID_TRANSITION"


async def test_reactivate_metric_blocked_by_pdp_cross_domain():
    """domain_admin 跨域恢复被 PDP 拒绝（对齐 deprecate 的域权限闸门）。"""
    mock_gov_svc = MagicMock()
    mock_gov_svc.check_metric_permission = AsyncMock(
        return_value=Decision(
            allow=False, reason="跨域越权，无权恢复他域指标", error_code="FORBIDDEN_DOMAIN"
        )
    )
    with patch("app.services.semantic.service.MetricRepository") as mock_repo_cls:
        _db = MagicMock()
        _db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        svc = MetricService(db=_db, governance_svc=mock_gov_svc)
        mock_repo_cls.return_value.get_by_code = AsyncMock(
            return_value=make_metric(status="DEPRECATED")
        )
        with pytest.raises(BusinessError) as exc:
            await svc.reactivate_metric(
                "sales_gmv_daily", actor_id=1, role="domain_admin", user_domain="finance"
            )
        assert exc.value.error_code == "FORBIDDEN_DOMAIN"
        mock_repo_cls.return_value.update_with_optimistic_lock.assert_not_called()


async def test_list_metrics_pagination_offset():
    svc, repo = _svc_with_repo()
    repo.list_metrics = AsyncMock(return_value=([make_metric()], 1))

    await svc.list_metrics(MetricListParams(page=3, page_size=10))

    called = repo.list_metrics.call_args.kwargs
    assert called["limit"] == 10
    assert called["offset"] == 20  # (page-1)*page_size


async def test_list_metrics_passes_lifecycle_date_filters():
    """生命周期快筛日期参数透传（TD §13）：created_after/updated_before 传给 repository。"""
    from datetime import UTC, datetime

    svc, repo = _svc_with_repo()
    repo.list_metrics = AsyncMock(return_value=([make_metric()], 1))
    created_after = datetime(2026, 8, 1, tzinfo=UTC)
    updated_before = datetime(2026, 7, 1, tzinfo=UTC)

    await svc.list_metrics(
        MetricListParams(created_after=created_after, updated_before=updated_before)
    )

    called = repo.list_metrics.call_args.kwargs
    assert called["created_after"] == created_after
    assert called["updated_before"] == updated_before


async def test_list_metrics_passes_metric_type_filter():
    """metric_type 类型过滤透传（派生指标绑定基础原子指标下拉）：params.metric_type
    须原样传给 repository，由服务端按 Metric.type 精确过滤（替代前端页内 filter）。"""
    svc, repo = _svc_with_repo()
    repo.list_metrics = AsyncMock(return_value=([make_metric()], 1))

    await svc.list_metrics(MetricListParams(metric_type="atomic"))

    called = repo.list_metrics.call_args.kwargs
    assert called["metric_type"] == "atomic"


async def test_is_breaking_change_detection():
    svc, _ = _svc_with_repo()
    old = {"expression": "SUM(a)", "dependencies": ["t1"]}
    same = {"expression": "SUM(a)", "dependencies": ["t1"]}
    diff = {"expression": "SUM(b)", "dependencies": ["t1"]}

    assert svc._is_breaking_change(old, same) is False
    assert svc._is_breaking_change(old, diff) is True


async def test_is_breaking_change_sql_mode():
    """SQL 模式口径变更与表达式同级触发破坏性判定（PENDING 确认期）。

    修复前 BREAKING_DEF_FIELDS 不含 sql/etl_sql：SQL 模式指标改口径被当
    非破坏性 UPDATE 直接生效，绕过 14 天消费方确认（治理漏洞）。
    """
    svc, _ = _svc_with_repo()
    old = {"sql": "SELECT SUM(amount) FROM sales", "source_tables": ["sales"]}
    same = {"sql": "SELECT SUM(amount) FROM sales", "source_tables": ["sales"]}
    diff = {
        "sql": "SELECT SUM(amount) FROM sales WHERE channel = 'APP'",
        "source_tables": ["sales"],
    }
    etl_old = {"etl_sql": "SELECT COUNT(*) FROM orders"}

    assert svc._is_breaking_change(old, same) is False
    assert svc._is_breaking_change(old, diff) is True
    assert (
        svc._is_breaking_change(
            etl_old, {"etl_sql": "SELECT COUNT(DISTINCT id) FROM orders"}
        )
        is True
    )


async def test_review_compliance_blocks_owner_self_review():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(owner_id=7))
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(owner_id=7))

    with pytest.raises(BusinessError) as exc:
        await svc.review_compliance("sales_gmv_daily", actor_id=7, role="domain_admin")
    assert exc.value.error_code == "SELF_REVIEW_BLOCKED"
    repo.update_with_optimistic_lock.assert_not_awaited()


async def test_review_compliance_allows_non_owner():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(owner_id=7, pii_flag=True))
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(owner_id=7, compliance_reviewed=True, pii_flag=True)
    )

    result = await svc.review_compliance("sales_gmv_daily", actor_id=99, role="domain_admin")
    assert result.compliance_reviewed is True
    repo.update_with_optimistic_lock.assert_awaited_once()


# ---- T052: compare + batch_register 测试 ----


async def test_compare_metrics_identical():
    """两指标完全相同 → 所有字段 identical。"""
    svc, repo = _svc_with_repo()
    defn = {"expression": "SUM(x)", "dependencies": ["t1"]}
    m1 = make_metric(metric_code="m1", definition_json=defn)
    m2 = make_metric(metric_code="m2", definition_json=defn)
    repo.get_by_code = AsyncMock(side_effect=[m1, m2])

    result = await svc.compare_metrics("m1", "m2")
    # 实现契约（对齐前端 MetricCompare）：result["metrics"] + result["fields"][...]
    assert result["metrics"] == ["m1", "m2"]
    # 同口径应标记 identical
    defn_diff = result["fields"]["definition"]
    assert defn_diff["difference_level"] == "identical"


async def test_compare_metrics_different():
    """两指标口径不同 → 标记 different。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="m1", definition_json={"expression": "SUM(x)"})
    m2 = make_metric(metric_code="m2", definition_json={"expression": "SUM(y)"})
    repo.get_by_code = AsyncMock(side_effect=[m1, m2])

    result = await svc.compare_metrics("m1", "m2")
    defn_diff = result["fields"]["definition"]
    assert defn_diff["difference_level"] == "different"


async def test_compare_metrics_archived_metric_raises_metric_archived():
    """跨服务一致性：对比中关联指标已因仲裁软删作废 → 抛 METRIC_ARCHIVED（携带 successor），
    而非对冲突仲裁弹窗直出裸「指标不存在」。"""
    svc, repo = _svc_with_repo()
    m2 = make_metric(metric_code="m2", definition_json={"expression": "SUM(y)"})
    # get_by_code 对 m1 返回 None（软删后不可见）；get_archived_by_code 命中软删 + successor
    repo.get_by_code = AsyncMock(side_effect=[None, m2])
    archived_m1 = make_metric(
        metric_code="m1",
        successor_code="m2",
    )
    archived_m1.arbitration_mark = {"status": "defeated"}
    repo.get_archived_by_code = AsyncMock(return_value=archived_m1)

    from app.core.error_codes import ErrorCode

    with pytest.raises(NotFoundError) as exc_info:
        await svc.compare_metrics("m1", "m2")
    assert exc_info.value.error_code == ErrorCode.METRIC_ARCHIVED
    assert exc_info.value.ctx["successor_code"] == "m2"
    assert exc_info.value.ctx["arbitration_mark"]["status"] == "defeated"


async def test_compare_metrics_plain_deleted_metric_raises_metric_deleted():
    """跨服务一致性：对比中关联指标被普通软删（无仲裁 successor）→ 抛 METRIC_DELETED
    （携带 deleted_at），供仲裁台提示「指标已删除，建议先处置冲突」而非裸 404。"""
    svc, repo = _svc_with_repo()
    m2 = make_metric(metric_code="m2", definition_json={"expression": "SUM(y)"})
    repo.get_by_code = AsyncMock(side_effect=[None, m2])
    archived_m1 = make_metric(metric_code="m1", deleted_at=datetime.now(UTC))
    archived_m1.successor_code = None
    repo.get_archived_by_code = AsyncMock(return_value=archived_m1)

    from app.core.error_codes import ErrorCode

    with pytest.raises(NotFoundError) as exc_info:
        await svc.compare_metrics("m1", "m2")
    assert exc_info.value.error_code == ErrorCode.METRIC_DELETED
    assert exc_info.value.ctx["deleted_at"] is not None


async def test_compare_matrix_all_identical():
    """多指标矩阵：三指标所有字段一致 → 每行 all_identical。"""
    svc, repo = _svc_with_repo()
    defn = {"expression": "SUM(x)", "dependencies": ["t1"]}
    m1 = make_metric(metric_code="m1", definition_json=defn, granularity="day")
    m2 = make_metric(metric_code="m2", definition_json=defn, granularity="day")
    m3 = make_metric(metric_code="m3", definition_json=defn, granularity="day")
    repo.get_by_code = AsyncMock(side_effect=[m1, m2, m3])

    result = await svc.compare_matrix(["m1", "m2", "m3"])
    assert result["metrics"] == ["m1", "m2", "m3"]
    for field in ("granularity", "unit", "definition", "dependencies"):
        assert result["fields"][field]["difference_level"] == "all_identical"
    # 依赖：全体交集为 t1，各指标无独有
    assert result["fields"]["dependencies"]["intersection"] == ["t1"]
    assert result["fields"]["dependencies"]["only"]["m1"] == []


async def test_compare_matrix_partial_and_different():
    """多指标矩阵：取值部分一致 → partial；取值互异 → all_different。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="m1", granularity="day")
    m2 = make_metric(metric_code="m2", granularity="day")
    m3 = make_metric(metric_code="m3", granularity="week")
    m4 = make_metric(metric_code="m4", granularity="month")
    repo.get_by_code = AsyncMock(side_effect=[m1, m2, m3, m4])

    result = await svc.compare_matrix(["m1", "m2", "m3", "m4"])
    # m1/m2 同为 day、m3=week、m4=month → 3 种取值、4 指标 → partial
    assert result["fields"]["granularity"]["difference_level"] == "partial"
    # unit 全部默认 yuan 一致
    assert result["fields"]["unit"]["difference_level"] == "all_identical"


async def test_compare_matrix_all_different_values():
    """多指标矩阵：每指标取值互异 → all_different。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="m1", granularity="day")
    m2 = make_metric(metric_code="m2", granularity="week")
    m3 = make_metric(metric_code="m3", granularity="month")
    repo.get_by_code = AsyncMock(side_effect=[m1, m2, m3])

    result = await svc.compare_matrix(["m1", "m2", "m3"])
    assert result["fields"]["granularity"]["difference_level"] == "all_different"


async def test_compare_matrix_dependencies():
    """多指标矩阵：依赖给出全体交集 + 各指标独有。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="m1", definition_json={"dependencies": ["t1", "t2"]})
    m2 = make_metric(metric_code="m2", definition_json={"dependencies": ["t1", "t2"]})
    m3 = make_metric(metric_code="m3", definition_json={"dependencies": ["t1"]})
    repo.get_by_code = AsyncMock(side_effect=[m1, m2, m3])

    result = await svc.compare_matrix(["m1", "m2", "m3"])
    deps = result["fields"]["dependencies"]
    assert deps["intersection"] == ["t1"]
    assert deps["only"]["m1"] == ["t2"]
    assert deps["only"]["m2"] == ["t2"]
    assert deps["only"]["m3"] == []
    # 依赖列表仅两种取值（t1/t2 与 t1）、3 指标 → partial
    assert deps["difference_level"] == "partial"


async def test_compare_matrix_dedup_preserves_order():
    """多指标矩阵：重复编码去重且保持首次出现顺序。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="m1")
    m2 = make_metric(metric_code="m2")

    def fake_get(code: str):
        return {"m1": m1, "m2": m2}[code]

    repo.get_by_code = AsyncMock(side_effect=fake_get)

    result = await svc.compare_matrix(["m2", "m1", "m2"])
    assert result["metrics"] == ["m2", "m1"]


async def test_compare_matrix_includes_governance_fields():
    """矩阵对比治理字段补全（P2-14）：PII/合规复核/状态/版本/描述入矩阵，owner 名称可读化。

    治理对比高价值信号（敏感分级不同、责任人不同）此前被挡在矩阵之外，本测试守护
    pii_flag/compliance_reviewed/status/version/description 五类字段行 + owner_names 映射。
    """
    svc, repo = _svc_with_repo()
    m1 = make_metric(
        metric_code="m1", pii_flag=True, compliance_reviewed=True,
        status="PUBLISHED", version=3, description="销售额", owner_id=1,
    )
    m2 = make_metric(
        metric_code="m2", pii_flag=False, compliance_reviewed=False,
        status="PUBLISHED", version=1, description="订单量", owner_id=1,
    )
    repo.get_by_code = AsyncMock(side_effect=[m1, m2])
    # owner 名称映射：owner_id=1 → 张三（走 repository 解析）
    repo.get_user_display_names = AsyncMock(return_value={1: "张三"})

    result = await svc.compare_matrix(["m1", "m2"])
    fields = result["fields"]
    # 五类治理字段全部入矩阵
    assert fields["pii_flag"]["values"] == {"m1": True, "m2": False}
    assert fields["pii_flag"]["difference_level"] == "all_different"
    assert fields["compliance_reviewed"]["values"] == {"m1": True, "m2": False}
    assert fields["status"]["values"] == {"m1": "PUBLISHED", "m2": "PUBLISHED"}
    assert fields["status"]["difference_level"] == "all_identical"
    assert fields["version"]["values"] == {"m1": 3, "m2": 1}
    assert fields["description"]["values"] == {"m1": "销售额", "m2": "订单量"}
    # owner 可读化：owner_names 映射携带责任人姓名
    assert result["owner_names"] == {1: "张三"}


async def test_compare_matrix_owner_names_skip_when_no_owner():
    """矩阵对比 owner 无责任人或无法解析：owner_names 为空映射，不阻断对比（P2-14）。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="m1", owner_id=None)
    m2 = make_metric(metric_code="m2", owner_id=None)
    repo.get_by_code = AsyncMock(side_effect=[m1, m2])

    result = await svc.compare_matrix(["m1", "m2"])
    assert result["owner_names"] == {}
    assert "granularity" in result["fields"]


async def test_compare_matrix_archived_metric_raises():
    """多指标矩阵：任一指标已因仲裁软删作废 → 抛 METRIC_ARCHIVED（携带 successor）。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="m1")
    repo.get_by_code = AsyncMock(side_effect=[None, m1])
    archived_m2 = make_metric(metric_code="m2", successor_code="m1")
    archived_m2.arbitration_mark = {"status": "defeated"}
    repo.get_archived_by_code = AsyncMock(return_value=archived_m2)

    from app.core.error_codes import ErrorCode

    with pytest.raises(NotFoundError) as exc_info:
        await svc.compare_matrix(["m2", "m1"])
    assert exc_info.value.error_code == ErrorCode.METRIC_ARCHIVED
    assert exc_info.value.ctx["successor_code"] == "m1"


async def test_compare_matrix_too_many_raises():
    """多指标矩阵：超过 6 个 → 抛中文 ValidationError（而非裸 Pydantic 422）。"""
    svc, repo = _svc_with_repo()
    with pytest.raises(ValidationError) as exc_info:
        await svc.compare_matrix([f"m{i}" for i in range(7)])
    assert "2~6 个" in exc_info.value.message
    assert "7" in exc_info.value.message
    # 校验发生在查询前：不应访问 repository
    repo.get_by_code.assert_not_called()


async def test_compare_matrix_too_few_raises():
    """多指标矩阵：少于 2 个 → 抛中文 ValidationError。"""
    svc, repo = _svc_with_repo()
    with pytest.raises(ValidationError) as exc_info:
        await svc.compare_matrix(["m1"])
    assert "2~6 个" in exc_info.value.message
    repo.get_by_code.assert_not_called()


async def test_batch_register_success():
    """批量注册：全部成功。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)  # 无重名
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    from app.services.semantic.schemas import MetricBatchRegisterRequest

    request = MetricBatchRegisterRequest(
        source_table="dwd.sales_detail",
        measure_columns=["gmv", "order_cnt"],
        dimension_mapping={"domain": "sales"},
        llm_prefill=True,
        domain="sales",
    )

    result = await svc.batch_register_metrics(request, actor_id=1)

    assert "batch_id" in result
    assert len(result["candidates"]) == 2
    # 实现契约：每条候选 {metric_code, status, validation_errors}，成功为 DRAFT
    assert all(c["status"] == "DRAFT" for c in result["candidates"])


async def test_batch_register_conflict_existing_loaded_once():
    """L3：批量注册冲突预检比对对象只加载一次（循环外预加载 + 逐列增量）。

    修复前每列 create_metric → _detect_and_mark_conflicts 都调 load_conflict_existing
    全量加载（N 列 = N 次全量加载，O(N²) 性能退化）。修复后循环前预加载一次，
    逐列成功后增量追加，保持候选间互相冲突检测。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())
    # mock 全量加载源：返回空活动指标（load_conflict_existing 正常返回 []）
    repo.list_active_for_conflict = AsyncMock(return_value=[])

    from app.services.semantic.schemas import MetricBatchRegisterRequest

    request = MetricBatchRegisterRequest(
        source_table="dwd.sales_detail",
        measure_columns=["gmv", "order_cnt", "order_amt"],
        dimension_mapping={"domain": "sales"},
        llm_prefill=True,
        domain="sales",
    )

    load_spy = AsyncMock(wraps=svc.load_conflict_existing)
    svc.load_conflict_existing = load_spy  # type: ignore[method-assign]

    result = await svc.batch_register_metrics(request, actor_id=1)

    assert len(result["candidates"]) == 3
    assert all(c["status"] == "DRAFT" for c in result["candidates"])
    # 核心断言：3 列只触发 1 次全量加载（循环外），而非每列 1 次（修复前为 3 次）
    assert load_spy.await_count == 1
    assert all(c["validation_errors"] is None for c in result["candidates"])


async def test_batch_register_dimension_mapping_into_definition():
    """批量注册：维度列映射合入每个候选指标的 definition_json。

    - ``dimensions``：维度名数组（对齐单条注册，血缘差异同步据此建「指标↔维度」边）
    - ``dimension_columns``：保留 维度名→源表列 完整映射（血缘/展示可读）
    - 空名/纯空白键过滤、保序去重
    """
    svc, repo = _svc_with_repo()
    captured: list = []

    async def _capture(req, **kw):
        captured.append(req)
        return make_metric()

    svc.create_metric = _capture  # type: ignore[method-assign]

    from app.services.semantic.schemas import MetricBatchRegisterRequest

    request = MetricBatchRegisterRequest(
        source_table="dwd.sales_detail",
        measure_columns=["gmv", "order_cnt"],
        dimension_mapping={"dept": "dept_code", "date": "dt", "": "empty_col", "  ": "space_col"},
        llm_prefill=True,
        domain="sales",
    )
    result = await svc.batch_register_metrics(request, actor_id=1)

    assert len(result["candidates"]) == 2
    assert len(captured) == 2
    for req in captured:
        defn = req.definition_json
        assert defn["dimensions"] == ["dept", "date"]
        assert defn["dimension_columns"] == {"dept": "dept_code", "date": "dt"}


async def test_batch_register_no_dimension_mapping_keeps_plain_definition():
    """批量注册：未传维度映射时 definition_json 保持最简（不注入 dimensions 键）。"""
    svc, repo = _svc_with_repo()
    captured: list = []

    async def _capture(req, **kw):
        captured.append(req)
        return make_metric()

    svc.create_metric = _capture  # type: ignore[method-assign]

    from app.services.semantic.schemas import MetricBatchRegisterRequest

    request = MetricBatchRegisterRequest(
        source_table="dwd.sales_detail",
        measure_columns=["gmv"],
        llm_prefill=True,
        domain="sales",
    )
    result = await svc.batch_register_metrics(request, actor_id=1)

    assert result["candidates"][0]["status"] == "DRAFT"
    assert captured[0].definition_json == {"expression": "SUM(gmv)", "dependencies": []}


async def test_batch_register_partial_failure():
    """批量注册：部分校验失败。"""
    svc, repo = _svc_with_repo()
    # 第二个重名
    repo.get_by_code = AsyncMock(side_effect=[None, make_metric()])
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    from app.services.semantic.schemas import MetricBatchRegisterRequest

    request = MetricBatchRegisterRequest(
        source_table="dwd.sales_detail",
        measure_columns=["gmv", "order_cnt"],
        dimension_mapping={"domain": "sales"},
        llm_prefill=True,
        domain="sales",
    )

    result = await svc.batch_register_metrics(request, actor_id=1)

    assert len(result["candidates"]) == 2
    # 失败信息在候选的 validation_errors 内（实现契约，无顶层 errors 键）
    assert result["candidates"][0]["status"] == "DRAFT"
    assert result["candidates"][1]["status"] == "VALIDATION_ERROR"
    assert result["candidates"][1]["validation_errors"] is not None


async def test_batch_register_db_error_savepoint_continues():
    """P13 批量注册单列 DB 错误：savepoint 隔离，仅该列失败、后续列继续。

    修复前 SQLAlchemyError 走整会话 rollback + 中止剩余列，已 flush 的候选被回滚
    但 candidates 仍记为 DRAFT → 部分结果失真。修复后逐列 begin_nested 隔离。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    from sqlalchemy.exc import IntegrityError

    from app.services.semantic.schemas import MetricBatchRegisterRequest

    request = MetricBatchRegisterRequest(
        source_table="dwd.sales_detail",
        measure_columns=["ok_amount_col", "bad_col"],
        dimension_mapping={"domain": "sales"},
        llm_prefill=True,
        domain="sales",
    )

    real_create = svc.create_metric

    async def _flaky_create(req, **kw):
        # 第 2 列模拟唯一键冲突（DB 级 IntegrityError）
        if getattr(req, "measure_column", None) == "bad_col":
            raise IntegrityError("stmt", {}, Exception("duplicate key"))
        return await real_create(req, **kw)

    svc.create_metric = _flaky_create  # type: ignore[method-assign]

    # savepoint 语义：MagicMock 的 async with 行为不可靠（吞异常），
    # 用真实 asynccontextmanager 模拟 begin_nested——异常从 yield 抛出进外层 except
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_nested():
        yield

    svc._db.begin_nested = _fake_nested  # type: ignore[method-assign]

    result = await svc.batch_register_metrics(request, actor_id=1)

    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["status"] == "DRAFT"  # ok_col 成功
    assert result["candidates"][1]["status"] == "VALIDATION_ERROR"  # bad_col 被 savepoint 隔离捕获
    assert "已跳过该列" in result["candidates"][1]["validation_errors"]


async def test_batch_register_db_error_middle_col_continues():
    """P13 补强：DB 错误发生在中间列时，后续列仍继续处理（修复 break 中止整批 bug）。

    修复前 SQLAlchemyError 分支误用 break，中间列 DB 错误会静默中止剩余列——
    candidates 缺失、前端结果表不显示后续列。修复后逐列独立，仅坏列失败。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    from sqlalchemy.exc import IntegrityError

    from app.services.semantic.schemas import MetricBatchRegisterRequest

    request = MetricBatchRegisterRequest(
        source_table="dwd.sales_detail",
        measure_columns=["ok_amount_col", "bad_col", "ok_amount_col_2"],
        dimension_mapping={"domain": "sales"},
        llm_prefill=True,
        domain="sales",
    )

    real_create = svc.create_metric

    async def _flaky_create(req, **kw):
        # 中间列模拟 DB 级错误（唯一键冲突）
        if getattr(req, "measure_column", None) == "bad_col":
            raise IntegrityError("stmt", {}, Exception("duplicate key"))
        return await real_create(req, **kw)

    svc.create_metric = _flaky_create  # type: ignore[method-assign]

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_nested():
        yield

    svc._db.begin_nested = _fake_nested  # type: ignore[method-assign]

    result = await svc.batch_register_metrics(request, actor_id=1)

    assert len(result["candidates"]) == 3  # 后续列不被中止
    assert result["candidates"][0]["status"] == "DRAFT"  # 第 1 列成功
    assert result["candidates"][1]["status"] == "VALIDATION_ERROR"  # 中间坏列失败
    assert result["candidates"][2]["status"] == "DRAFT"  # 第 3 列继续成功（修复核心断言）
    assert "已跳过该列" in result["candidates"][1]["validation_errors"]


# ---------------------------------------------------------------- SQL 批量注册（场景A/B）


def _sql_derived(
    key: str, code: str, name: str, col: str, agg: str = "SUM", raw_sql: str | None = None
) -> SqlBatchCreateCandidate:
    """构造 SQL 批量注册派生候选（方案 A：SQL 物理口径一律派生，原子只从逻辑度量
    目录创建——批量 SQL 推断候选不再出现 atomic）。"""
    return SqlBatchCreateCandidate(
        key=key,
        metric_code=code,
        name=name,
        type="derived",
        source_table="dwd_order_di",
        measure_column=col,
        aggregation=agg,
        period="day",
        definition_json={
            "expression": f"{agg}({col})",
            "source_fields": [{"table": "dwd_order_di", "column": col}],
        },
        raw_sql=raw_sql,
    )


def _sql_composite(
    key: str, code: str, name: str, deps: list[str]
) -> SqlBatchCreateCandidate:
    """构造 SQL 批量注册复合候选。"""
    return SqlBatchCreateCandidate(
        key=key,
        metric_code=code,
        name=name,
        type="composite",
        definition_json={
            "sql": "SELECT dt, SUM(amount) FROM dwd_order_di GROUP BY dt",
            "dependencies": deps,
            "source_tables": ["dwd_order_di"],
        },
        dependencies=deps,
    )


async def test_sql_batch_register_success_with_composite():
    """SQL 批量注册：原子 + 复合全部成功（复合依赖批内原子）。"""
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[
            _sql_derived("0:amount", "sales_order_amount_day", "日订单金额", "amount", "SUM"),
            _sql_derived(
                "0:user_id", "sales_order_userid_day", "日去重用户", "user_id", "COUNT_DISTINCT"
            ),
            _sql_composite(
                "0:composite",
                "sales_order_amountuserid_day",
                "金额用户复合",
                ["sales_order_amount_day", "sales_order_userid_day"],
            ),
        ],
    )
    result = await svc.batch_register_from_sql(request, actor_id=1)
    assert "batch_id" in result
    assert len(result["candidates"]) == 3
    assert all(c["status"] == "DRAFT" for c in result["candidates"])
    assert all(c["validation_errors"] is None for c in result["candidates"])


async def test_sql_batch_register_derived_no_agg_persists_none():
    """派生比率/条件列候选 aggregation=None 批量创建落库 NULL（不再 or "SUM" 假占位）。

    2026-08-28 aggregation 可空：派生/复合聚合语义由口径表达式/依赖承载
    （客单价 = ROUND(SUM(amount)/NULLIF(COUNT(user_id),0),2) 整体是除法非 SUM），
    详情页据此展示「派生表达式」而非假 SUM。
    """
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    # 返回入参对象（真实 DB 路径 repo.create 返回同一 Metric），以便断言落库字段
    repo.create = AsyncMock(side_effect=lambda m: m)
    repo.create_version = AsyncMock(return_value=MagicMock())

    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[
            SqlBatchCreateCandidate(
                key="0:ratio",
                metric_code="sales_avg_value_day",
                name="日客单价",
                type="derived",
                source_table="dwd_order_di",
                measure_column="amount",
                aggregation=None,
                period="day",
                definition_json={
                    "expression": "ROUND(SUM(amount)/NULLIF(COUNT(user_id),0),2)",
                    "source_fields": [{"table": "dwd_order_di", "column": "amount"}],
                },
            ),
        ],
    )
    result = await svc.batch_register_from_sql(request, actor_id=1)
    assert result["candidates"][0]["status"] == "DRAFT"
    # 落库 Metric.aggregation = None（不再 or "SUM" 假占位，派生语义由表达式承载）
    created_metric = repo.create.call_args[0][0]
    assert created_metric.aggregation is None


async def test_sql_batch_register_composite_missing_dep_skipped():
    """复合候选依赖缺失（批内未创建 + 库中不存在）→ VALIDATION_ERROR 跳过。"""
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)  # 任何依赖都不存在
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[
            _sql_derived("0:amount", "sales_order_amount_day", "日订单金额", "amount"),
            _sql_composite(
                "0:composite",
                "sales_order_comp_day",
                "复合",
                ["sales_order_amount_day", "missing_dep"],
            ),
        ],
    )
    result = await svc.batch_register_from_sql(request, actor_id=1)
    assert result["candidates"][0]["status"] == "DRAFT"
    assert result["candidates"][1]["status"] == "VALIDATION_ERROR"
    assert "missing_dep" in result["candidates"][1]["validation_errors"]
    # 缺依赖的复合不进 savepoint 创建
    assert repo.create.call_count == 1


async def test_sql_batch_register_composite_no_dep_downgraded_to_derived():
    """S2（三轮审查）：表达式内嵌复合候选（无依赖，如自动推断的比率列 SUM(a)/COUNT(b)）
    批量创建时降级为派生——复合门禁要求声明依赖（schemas.py），无依赖复合若按 composite
    创建会被 422 拦死 parse→create 闭环；降级派生（依赖可选、表达式承载运算）保证
    闭环打通，并给出明确提示。"""
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[
            _sql_composite("0:arpu", "sales_order_arpu_day", "客单价复合", []),
        ],
    )
    result = await svc.batch_register_from_sql(request, actor_id=1)
    assert result["candidates"][0]["status"] == "DRAFT"
    assert result["candidates"][0]["validation_errors"] is not None
    assert "已按派生指标创建" in result["candidates"][0]["validation_errors"]
    # 降级派生后仍走 savepoint 创建（不被门禁 422 拦截）
    assert repo.create.call_count == 1
    created_req = repo.create.call_args.args[0]
    assert created_req.type == "derived"


async def test_sql_batch_register_db_error_savepoint_isolation():
    """SQL 批量注册单条 DB 错误：savepoint 隔离，仅该条失败、其余继续。"""
    from sqlalchemy.exc import IntegrityError

    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[
            _sql_derived("0:ok", "sales_order_ok_day", "日订单金额", "ok_col"),
            _sql_derived("0:bad", "sales_order_bad_day", "日用户数", "bad_col"),
        ],
    )
    real_create = svc.create_metric

    async def _flaky_create(req, **kw):
        if getattr(req, "measure_column", None) == "bad_col":
            raise IntegrityError("stmt", {}, Exception("duplicate key"))
        return await real_create(req, **kw)

    svc.create_metric = _flaky_create  # type: ignore[method-assign]
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_nested():
        yield

    svc._db.begin_nested = _fake_nested  # type: ignore[method-assign]

    result = await svc.batch_register_from_sql(request, actor_id=1)
    assert result["candidates"][0]["status"] == "DRAFT"
    assert result["candidates"][1]["status"] == "VALIDATION_ERROR"
    assert "已跳过该条" in result["candidates"][1]["validation_errors"]


async def test_sql_batch_register_domain_gate():
    """域门禁：域管理员仅可批量注册本域指标。"""
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    svc, _ = _svc_with_repo()
    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[_sql_derived("0:amount", "sales_order_amount_day", "日订单金额", "amount")],
    )
    with pytest.raises(BusinessError) as exc:
        await svc.batch_register_from_sql(
            request, actor_id=1, role="domain_admin", user_domain="finance"
        )
    assert exc.value.error_code == "FORBIDDEN"


async def test_sql_batch_register_catches_pydantic_validation_error():
    """P0 兜底：候选构造 MetricCreateRequest 触发 pydantic ValidationError → 逐条标记
    VALIDATION_ERROR，绝不因单个候选 schema 校验失败整批 500（此前非法编码/聚合漏网时
    整批 500）。"""
    from pydantic import ValidationError

    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[
            _sql_derived("0:ok", "sales_order_ok_day", "日订单金额", "ok_col"),
            _sql_derived("0:bad", "sales_order_bad_day", "日用户数", "bad_col"),
        ],
    )
    exc = ValidationError.from_exception_data(
        "MetricCreateRequest",
        [{
            "type": "value_error",
            "loc": ("metric_code",),
            "input": "_bad",
            "ctx": {"error": ValueError("非法编码")},
        }],
    )

    real_create = svc.create_metric
    counter = {"calls": 0}

    async def _failing_create(req, **kw):
        counter["calls"] += 1
        # 第一个候选正常创建；第二个候选在 MetricCreateRequest 构造时抛 ValidationError
        if counter["calls"] >= 2:
            raise exc
        return await real_create(req, **kw)

    svc.create_metric = _failing_create  # type: ignore[method-assign]
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_nested():
        yield

    svc._db.begin_nested = _fake_nested  # type: ignore[method-assign]

    result = await svc.batch_register_from_sql(request, actor_id=1)
    assert result["candidates"][0]["status"] == "DRAFT"
    assert result["candidates"][1]["status"] == "VALIDATION_ERROR"
    assert "候选参数校验失败" in result["candidates"][1]["validation_errors"]


async def test_sql_batch_register_conflict_existing_loaded_once():
    """P0-1：SQL 批量注册冲突预检比对对象只加载一次（循环外预加载 + 逐候选增量）。

    修复前 batch_register_from_sql 每候选 create_metric 都调 load_conflict_existing
    全量加载（N 候选 = N 次全量加载，O(N²) 性能退化；service.py:608-609 注释声明的
    「批量注册场景传入预加载 existing」此前 SQL 批量路径漏接线）。修复后循环前预加载
    一次，逐候选成功后增量追加，保持候选间互相冲突检测。
    """
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())
    # mock 全量加载源：返回空活动指标（load_conflict_existing 正常返回 []）
    repo.list_active_for_conflict = AsyncMock(return_value=[])

    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[
            _sql_derived("0:amount", "sales_order_amount_day", "日订单金额", "amount"),
            _sql_derived("0:user_id", "sales_order_userid_day", "日去重用户", "user_id"),
            _sql_composite(
                "0:composite",
                "sales_order_comp_day",
                "金额用户复合",
                ["sales_order_amount_day", "sales_order_userid_day"],
            ),
        ],
    )

    load_spy = AsyncMock(wraps=svc.load_conflict_existing)
    svc.load_conflict_existing = load_spy  # type: ignore[method-assign]

    result = await svc.batch_register_from_sql(request, actor_id=1)

    assert len(result["candidates"]) == 3
    assert all(c["status"] == "DRAFT" for c in result["candidates"])
    # 核心断言：3 候选（含复合）只触发 1 次全量加载（循环外），而非每候选 1 次
    assert load_spy.await_count == 1


async def test_sql_batch_register_composite_owners_and_unit():
    """P0-2：复合指标批量创建补齐口径三方责任 + 单位（详情页 OwnerChain 完整）。

    修复前 Phase2 只传 aggregation/definition_json/period/granularity，复合指标
    责任方三角缺失、单位取默认——单条创建有、批量路径漏传。修复后候选携带的
    unit/product_owner_id 等透传到 create_metric 的 MetricCreateRequest。
    """
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest, SqlBatchCreateCandidate

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[
            _sql_derived("0:amount", "sales_order_amount_day", "日订单金额", "amount"),
            SqlBatchCreateCandidate(
                key="0:composite",
                metric_code="sales_order_comp_day",
                name="金额用户复合",
                type="composite",
                period="day",
                granularity="day",
                unit="CNY",
                definition_json={
                    "sql": "SELECT dt, SUM(amount) FROM dwd_order_di GROUP BY dt",
                    "dependencies": ["sales_order_amount_day"],
                    "source_tables": ["dwd_order_di"],
                },
                dependencies=["sales_order_amount_day"],
                product_owner_id=10,
                tech_owner_id=11,
                dw_developer_id=12,
                product_owner_name="产品王",
                tech_owner_name="技术李",
                dw_developer_name="数仓赵",
            ),
        ],
    )

    captured: list = []

    async def _capture_create(req, **kw):
        captured.append(req)
        return make_metric()

    real_create = svc.create_metric
    svc.create_metric = _capture_create  # type: ignore[method-assign]
    # 兼容：真实 create_metric 内部会调 repo.create/create_version，直接返回 mock 即可
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    result = await svc.batch_register_from_sql(request, actor_id=1)
    assert all(c["status"] == "DRAFT" for c in result["candidates"])
    # 捕获的复合候选 MetricCreateRequest 携带单位 + 三方责任
    composite_req = next(r for r in captured if r.type == "composite")
    assert composite_req.unit == "CNY"
    assert composite_req.product_owner_id == 10
    assert composite_req.tech_owner_id == 11
    assert composite_req.dw_developer_id == 12
    assert composite_req.product_owner_name == "产品王"
    assert composite_req.tech_owner_name == "技术李"
    assert composite_req.dw_developer_name == "数仓赵"
    del real_create


async def test_review_compliance_rejects_non_pii():
    """非 PII 指标无需合规复核 → PII_FLAG_REQUIRED。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(owner_id=7, pii_flag=False))

    with pytest.raises(BusinessError) as exc:
        await svc.review_compliance("sales_gmv_daily", actor_id=99, role="domain_admin")
    assert exc.value.error_code == "PII_FLAG_REQUIRED"


async def test_sql_batch_register_batch_id_persisted():
    """P0-C：SQL 批量创建的指标携带 batch_id（此前 MetricCreateRequest 无该字段，
    pydantic extra=ignore 丢弃——批量创建的指标无法整批回溯）。"""
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[
            _sql_derived("0:amount", "sales_order_amount_day", "日订单金额", "amount"),
            _sql_composite(
                "0:composite",
                "sales_order_comp_day",
                "金额用户复合",
                ["sales_order_amount_day"],
            ),
        ],
    )
    captured: list = []

    async def _capture_create(req, **kw):
        captured.append(req)
        return make_metric()

    svc.create_metric = _capture_create  # type: ignore[method-assign]

    result = await svc.batch_register_from_sql(request, actor_id=1)
    assert all(c["status"] == "DRAFT" for c in result["candidates"])
    batch_id = result["batch_id"]
    assert batch_id.startswith("sqlbatch_")
    # 原子 + 复合候选的 MetricCreateRequest 都携带同一 batch_id
    assert all(req.batch_id == batch_id for req in captured)
    assert len(captured) == 2


async def test_sql_batch_register_derived_owners_passed():
    """P0-2 方案 A：SQL 批量注册派生候选的三方责任透传（此前仅复合补齐，基础候选
    责任链空——详情页 OwnerChain 不完整；方案 A 后 SQL 候选统一派生）。"""
    from app.services.semantic.schemas import (
        MetricSqlBatchRegisterRequest,
        SqlBatchCreateCandidate,
    )

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[
            SqlBatchCreateCandidate(
                key="0:amount",
                metric_code="sales_order_amount_day",
                name="日订单金额",
                type="derived",
                source_table="dwd_order_di",
                measure_column="amount",
                aggregation="SUM",
                period="day",
                definition_json={
                    "expression": "SUM(amount)",
                    "source_fields": [{"table": "dwd_order_di", "column": "amount"}],
                },
                product_owner_id=10,
                tech_owner_id=11,
                dw_developer_id=12,
                product_owner_name="产品王",
                tech_owner_name="技术李",
                dw_developer_name="数仓赵",
            )
        ],
    )
    captured: list = []

    async def _capture_create(req, **kw):
        captured.append(req)
        return make_metric()

    svc.create_metric = _capture_create  # type: ignore[method-assign]

    result = await svc.batch_register_from_sql(request, actor_id=1)
    assert all(c["status"] == "DRAFT" for c in result["candidates"])
    derived_req = captured[0]
    assert derived_req.product_owner_id == 10
    assert derived_req.tech_owner_id == 11
    assert derived_req.dw_developer_id == 12
    assert derived_req.product_owner_name == "产品王"
    assert derived_req.tech_owner_name == "技术李"
    assert derived_req.dw_developer_name == "数仓赵"


async def test_sql_batch_register_raw_sql_persisted():
    """口径溯源（P2）：SQL 批量创建原子候选携带整句原始 SQL → 透传落 Metric.raw_sql
    （此前候选仅表达式，整句口径原文不持久化，batch_id 无法反查）。"""
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    raw = "SELECT dt, SUM(amount) AS amount FROM dwd_order_di GROUP BY dt"
    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[
            _sql_derived(
                "0:amount", "sales_order_amount_day", "日订单金额", "amount", "SUM", raw_sql=raw
            )
        ],
    )
    captured: list = []

    async def _capture_create(req, **kw):
        captured.append(req)
        return make_metric()

    svc.create_metric = _capture_create  # type: ignore[method-assign]

    result = await svc.batch_register_from_sql(request, actor_id=1)
    assert all(c["status"] == "DRAFT" for c in result["candidates"])
    assert captured[0].raw_sql == raw


async def test_sql_batch_register_notifies_creator():
    """通知闭环（P2）：批量创建成功定向通知创建者本人「已批量创建 N 个 DRAFT」——
    此前批量创建只发 metric.created 事件、不通知任何用户，创建者无下一步送审引导。"""
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())
    svc._notify_metric_stakeholders = AsyncMock(return_value=None)

    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[
            _sql_derived("0:amount", "sales_order_amount_day", "日订单金额", "amount")
        ],
    )
    result = await svc.batch_register_from_sql(request, actor_id=7)
    assert all(c["status"] == "DRAFT" for c in result["candidates"])
    call = svc._notify_metric_stakeholders.call_args
    assert call.args[0] == "metric.batch_created"
    assert call.kwargs["submitter_id"] == 7  # 通知创建者本人
    assert call.kwargs["payload"]["count"] == 1


async def test_sql_batch_register_skips_notify_when_all_failed():
    """通知闭环（P2）：全部失败时**不**通知创建者「已创建」（避免误导——无成功项
    不应宣称批量创建成功）。"""
    from sqlalchemy.exc import SQLAlchemyError

    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    # 全部创建失败（DB 错误 → except SQLAlchemyError 标记 VALIDATION_ERROR）
    repo.create = AsyncMock(side_effect=SQLAlchemyError("boom"))
    svc._notify_metric_stakeholders = AsyncMock(return_value=None)

    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[
            _sql_derived("0:amount", "sales_order_amount_day", "日订单金额", "amount")
        ],
    )
    result = await svc.batch_register_from_sql(request, actor_id=7)
    assert result["candidates"][0]["status"] == "VALIDATION_ERROR"
    assert not svc._notify_metric_stakeholders.called


async def test_sql_batch_register_conflict_llm_budget():
    """P0-2：SQL 批量注册的冲突预检共享批级 LLM 预算——超过预算后 create_metric
    降级纯词法（use_llm=False），防批量路径数百上千次 LLM 调用（成本/超时风险）。"""
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    request = MetricSqlBatchRegisterRequest(
        domain="sales",
        candidates=[
            _sql_derived("0:a", "sales_order_amount_day", "日订单金额", "amount"),
            _sql_derived("0:b", "sales_order_userid_day", "日去重用户", "user_id"),
            _sql_derived("0:c", "sales_order_cnt_day", "日订单数", "order_id", agg="COUNT"),
        ],
    )
    captured: list = []

    async def _capture_create(req, **kw):
        captured.append(kw.get("_conflict_llm_budget"))
        return make_metric()

    svc.create_metric = _capture_create  # type: ignore[method-assign]

    result = await svc.batch_register_from_sql(request, actor_id=1)
    assert all(c["status"] == "DRAFT" for c in result["candidates"])
    # 每个候选都拿到共享预算 dict（同引用、limit=10）——批级共享设计成立
    assert all(isinstance(b, dict) and b["limit"] == 10 for b in captured)
    # 3 个候选共享同一预算对象（create_metric 被替换，used 递增由真实路径内部驱动）
    assert len({id(b) for b in captured}) == 1
    assert captured[-1]["used"] == 0


# ---- 指标编码自动生成（FR-010）----


async def test_create_metric_auto_generates_code_from_measure():
    """metric_code 缺省时按 源表/度量列/周期 自动生成 4 段式编码。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric(metric_code="sales_sales_orderamount_day")
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    payload = make_create_payload(
        metric_code=None,
        source_table="dwd_sales_orders",
        measure_column="order_amount",
        period="day",
    )
    result = await svc.create_metric(MetricCreateRequest(**payload), owner_id=1)

    # 生成编码: sales(域) + sales(业务对象) + orderamount(度量) + day(周期)
    assert result.metric_code == "sales_sales_orderamount_day"


async def test_create_metric_auto_generates_fallback_code():
    """metric_code 缺省且无源表时回退 {domain}_entity_{measure}_day。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric(metric_code="sales_entity_value_day")
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    result = await svc.create_metric(
        MetricCreateRequest(**make_create_payload(metric_code=None)),
        owner_id=1,
    )

    assert result.metric_code == "sales_entity_value_day"


async def test_create_metric_auto_code_conflict_suffix():
    """自动生成的编码冲突时追加 _2 后缀。"""
    svc, repo = _svc_with_repo()
    # 生成时第 1 次查（候选已存在）→ _2；生成时第 2 次查（_2 可用）+ 创建前唯一性检查均不存在
    repo.get_by_code = AsyncMock(side_effect=[make_metric(), None, None])
    created = make_metric(metric_code="sales_entity_value_day_2")
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    result = await svc.create_metric(
        MetricCreateRequest(**make_create_payload(metric_code=None)),
        owner_id=1,
    )

    assert result.metric_code == "sales_entity_value_day_2"


async def test_create_metric_explicit_code_still_validated():
    """显式传入 metric_code 仍走 4 段式校验（ConflictError 之外，非法格式 422）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)

    with pytest.raises(ValueError):
        MetricCreateRequest(**make_create_payload(metric_code="bad_code"))


# ---- approve/reject 自审豁免（管理员可审核自己提交的指标，普通角色仍禁止）----


def _svc_approve_ready(
    svc,
    repo,
    *,
    submitted_by: int = 1,
    status: str = "REVIEW",
    reviewer_id: int | None = None,
    reviewer_type: str | None = None,
    reviewer_domain: str | None = None,
):
    """准备 approve/reject 所需的 mock 环境，返回指标。"""
    metric = make_metric(
        status=status,
        submitted_by=submitted_by,
        owner_id=2,
        reviewer_id=reviewer_id,
        reviewer_type=reviewer_type,
        reviewer_domain=reviewer_domain,
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=MagicMock(id=1, version=1))
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock(return_value=None)
    svc._cache.invalidate = AsyncMock(return_value=None)
    svc._publish_event = AsyncMock(return_value=None)
    return metric


async def test_approve_metric_admin_can_self_review():
    """platform_admin 可审核自己提交的指标（提交人==审核人）。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1)

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="standard", target_version=1),
        actor_id=1,
        role="platform_admin",
    )

    assert result.status == "PUBLISHED"
    repo.update_with_optimistic_lock.assert_awaited_once()


async def test_approve_metric_domain_admin_can_self_review():
    """domain_admin 同样豁免自审。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1)

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="standard", target_version=1),
        actor_id=1,
        role="domain_admin",
    )

    assert result.status == "PUBLISHED"


async def test_approve_metric_non_admin_self_review_blocked():
    """普通角色（reviewer）即便被指派为评审人，自审仍被禁止。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=1, reviewer_type="user")

    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=1,
            role="reviewer",
        )
    assert exc.value.error_code == "SELF_REVIEW_BLOCKED"
    repo.update_with_optimistic_lock.assert_not_awaited()


async def test_approve_metric_no_role_self_review_blocked():
    """role 缺省（None）按严格模式处理——向后兼容不传 role 的调用方。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=1, reviewer_type="user")

    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=1,
        )
    assert exc.value.error_code == "SELF_REVIEW_BLOCKED"


async def test_approve_metric_different_actor_still_allowed():
    """被指派评审人（非提交人）审核通过——评审人校验放行 + 不触发自审。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=99, reviewer_type="user")

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="standard", target_version=1),
        actor_id=99,
        role="reviewer",
    )

    assert result.status == "PUBLISHED"


async def test_approve_metric_gray_publishes_gray_event():
    """灰度发布（mode=experimental）→ 状态 EXPERIMENTAL + 发 metric.gray_published 事件（含租户）。

    灰度仅试点指定租户，通知语义须与标准发布区分，避免 stakeholders 收到
    「指标已通过」却实际仅灰度。R35 修复：灰度发独立事件与标题。
    """
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=99, reviewer_type="user")
    svc._publish_event = AsyncMock()

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="experimental", gray_tenant_ids=[101, 102], target_version=1),
        actor_id=99,
        role="reviewer",
    )

    assert result is not None  # mock 固定返回 PUBLISHED，此处仅验证事件语义
    event_type, payload = svc._publish_event.call_args.args[:2]
    assert event_type == "metric.gray_published"
    assert payload["gray_tenant_ids"] == [101, 102]


async def test_approve_metric_standard_publishes_approved_event():
    """标准发布（mode=standard）→ 发 metric.approved 事件（灰度事件不误发）。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=99, reviewer_type="user")
    svc._publish_event = AsyncMock()

    await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="standard", target_version=1),
        actor_id=99,
        role="reviewer",
    )

    event_type = svc._publish_event.call_args.args[0]
    assert event_type == "metric.approved"


# ---- 评审指派（TD §13）：提交保存指派 + 仅被指派评审人可通过/打回 ----


async def test_submit_metric_saves_user_reviewer():
    """提交评审指定用户评审人：reviewer_id/reviewer_type 落库。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="REVIEW"))
    svc._publish_event = AsyncMock()

    await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="提交审核", reviewer_id=7, reviewer_type="user"),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )
    kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert kwargs["reviewer_id"] == 7
    assert kwargs["reviewer_type"] == "user"
    assert kwargs["reviewer_domain"] is None


async def test_submit_metric_clears_reject_reason():
    """被驳回草稿重新提审时清空历史驳回原因（生命周期闭环）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="DRAFT",
            owner_id=1,
            reject_reason="粒度与口径不符",
            reject_reviewer_id=3,
            rejected_at="2026-08-01 10:00:00",
        )
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="REVIEW"))
    svc._publish_event = AsyncMock()

    await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="已修正粒度，重新提审"),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )
    kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert kwargs["reject_reason"] is None
    assert kwargs["reject_reviewer_id"] is None
    assert kwargs["rejected_at"] is None


async def test_submit_metric_domain_reviewer_defaults_to_metric_domain():
    """域评审组未指定域时缺省用指标自身域。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(status="DRAFT", owner_id=1, domain="sales")
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="REVIEW"))
    svc._publish_event = AsyncMock()

    await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="提交审核", reviewer_type="domain"),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )
    kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert kwargs["reviewer_type"] == "domain"
    assert kwargs["reviewer_domain"] == "sales"


async def test_submit_metric_user_reviewer_without_id_rejected():
    """指定 user 评审类型但未填评审人 → REVIEWER_ASSIGN_INVALID。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))

    with pytest.raises(BusinessError) as exc:
        await svc.submit_metric(
            "sales_gmv_daily",
            MetricSubmitRequest(change_reason="提交审核", reviewer_type="user"),
            actor_id=1,
            role="metric_owner",
        )
    assert exc.value.error_code == "REVIEWER_ASSIGN_INVALID"


async def test_approve_metric_non_assigned_reviewer_rejected():
    """已指派评审用户，非被指派者（且非 platform_admin）通过 → FORBIDDEN_REVIEWER。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=7, reviewer_type="user")

    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=99,
            role="reviewer",
        )
    assert exc.value.error_code == "FORBIDDEN_REVIEWER"
    repo.update_with_optimistic_lock.assert_not_awaited()


async def test_approve_metric_domain_team_same_domain_allowed():
    """域评审组：同域 reviewer 角色可通过。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_type="domain", reviewer_domain="sales")

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="standard", target_version=1),
        actor_id=50,
        role="reviewer",
        user_domain="sales",
    )
    assert result.status == "PUBLISHED"


async def test_approve_metric_domain_team_wrong_domain_rejected():
    """域评审组：非该域用户（即便 reviewer 角色）→ FORBIDDEN_REVIEWER。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_type="domain", reviewer_domain="sales")

    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=50,
            role="reviewer",
            user_domain="finance",
        )
    assert exc.value.error_code == "FORBIDDEN_REVIEWER"


async def test_approve_metric_domain_team_non_reviewer_role_rejected():
    """域评审组：非 domain_admin/reviewer 角色（如 metric_owner）→ FORBIDDEN_REVIEWER。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_type="domain", reviewer_domain="sales")

    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=50,
            role="metric_owner",
            user_domain="sales",
        )
    assert exc.value.error_code == "FORBIDDEN_REVIEWER"


async def test_approve_metric_unassigned_non_domain_admin_rejected():
    """未指派评审人：非 domain_admin（reviewer 角色）→ FORBIDDEN_REVIEWER。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1)

    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=50,
            role="reviewer",
            user_domain="sales",
        )
    assert exc.value.error_code == "FORBIDDEN_REVIEWER"


async def test_reject_metric_non_assigned_reviewer_rejected():
    """已指派评审用户，非被指派者打回 → FORBIDDEN_REVIEWER。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=7, reviewer_type="user")

    with pytest.raises(BusinessError) as exc:
        await svc.reject_metric(
            "sales_gmv_daily",
            MetricRejectRequest(reason="口径需调整"),
            actor_id=99,
            role="reviewer",
        )
    assert exc.value.error_code == "FORBIDDEN_REVIEWER"
    repo.update_with_optimistic_lock.assert_not_awaited()


async def test_reject_metric_admin_can_self_review():
    """platform_admin 可驳回自己提交的指标。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1)
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="DRAFT"))

    result = await svc.reject_metric(
        "sales_gmv_daily",
        MetricRejectRequest(reason="口径需调整"),
        actor_id=1,
        role="platform_admin",
    )

    assert result.status == "DRAFT"


async def test_reject_metric_non_admin_self_review_blocked():
    """普通角色即便被指派为评审人，自驳回仍被禁止。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=1, reviewer_type="user")

    with pytest.raises(BusinessError) as exc:
        await svc.reject_metric(
            "sales_gmv_daily",
            MetricRejectRequest(reason="口径需调整"),
            actor_id=1,
            role="reviewer",
        )
    assert exc.value.error_code == "SELF_REVIEW_BLOCKED"


# ---- 补充覆盖率：辅助函数 / 状态流转 / 版本确认 / 灰度 / 健康 / 消费指南 ----


async def test_redact_definition_recurses():
    """redact_definition 保留键结构、叶子值全部脱敏为 ***。"""
    from app.services.semantic.service import redact_definition

    out = redact_definition({"expr": "SUM(x)", "nested": {"a": 1}, "arr": ["x", "y"], "flag": True})
    assert out == {"expr": "***", "nested": {"a": "***"}, "arr": ["***", "***"], "flag": "***"}


async def test_normalize_pii_syncs_dual_source():
    """_normalize_pii 双源一致：pii_flag 权威，回写/移除 definition.pii。"""
    from app.services.semantic.service import _normalize_pii

    # pii_flag=True 且 definition 无 pii → 回填
    d1, f1 = _normalize_pii({"expr": "x"}, True)
    assert d1["pii"] is True and f1 is True
    # definition 显式 pii=False 覆盖 flag
    d2, f2 = _normalize_pii({"pii": False}, True)
    assert "pii" not in d2 and f2 is False


async def test_submit_metric_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
    updated = make_metric(status="REVIEW")
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)

    result = await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="提交审核"),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )
    assert result.status == "REVIEW"
    assert repo.update_with_optimistic_lock.call_args.kwargs["submitted_by"] == 1


async def test_submit_metric_blocked_when_definition_empty():
    """空心指标（无表达式/无SQL/无源表）提交评审被拦截——口径完整性校验（FR-012）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", owner_id=1, definition_json={})
    )

    with pytest.raises(BusinessError) as exc:
        await svc.submit_metric(
            "sales_gmv_daily",
            MetricSubmitRequest(change_reason="提交审核"),
            actor_id=1,
            role="metric_owner",
            user_domain="sales",
        )
    assert exc.value.error_code == "DEFINITION_INCOMPLETE"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_submit_metric_blocked_by_pdp_decision():
    """PDP 拒绝 → submit_metric 不提交审核。"""
    mock_gov_svc = MagicMock()
    mock_gov_svc.check_metric_permission = AsyncMock(
        return_value=Decision(allow=False, reason="无 write 权限", error_code="FORBIDDEN")
    )
    with patch("app.services.semantic.service.MetricRepository") as mock_repo_cls:
        svc = MetricService(db=MagicMock(), governance_svc=mock_gov_svc)
        repo = mock_repo_cls.return_value
        repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
        repo.update_with_optimistic_lock = AsyncMock()

        with pytest.raises(BusinessError) as exc:
            await svc.submit_metric(
                "sales_gmv_daily",
                MetricSubmitRequest(change_reason="提交审核"),
                actor_id=1,
                role="metric_owner",
                user_domain="other_domain",
            )

        assert exc.value.error_code == "FORBIDDEN"
        repo.update_with_optimistic_lock.assert_not_awaited()


async def test_submit_metric_publishes_event():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="REVIEW"))
    svc._publish_event = AsyncMock()

    await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="提交审核"),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )
    svc._publish_event.assert_awaited_once()
    assert svc._publish_event.call_args.args[0] == "metric.submitted"


async def test_approve_metric_standard():
    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1, pii_flag=False)
    metric.submitted_by = 1
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()

    result = await svc.approve_metric(
        "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="platform_admin"
    )
    assert result.status == "PUBLISHED"
    svc._publish_event.assert_awaited_once()


async def test_approve_metric_hard_conflict_blocked():
    """TD §12.4 硬冲突阻断发布：未决 block_publish=True 冲突时 approve 被拒。

    修复前 pending_conflict 仅用于目录红标展示，评审人可直接放行未经协商的
    冲突口径进入消费方。修复后 REVIEW 审批前置检查未决硬冲突 → CONFLICT_BLOCKED。
    """
    from app.models.conflict import Conflict, ConflictStatus, ConflictType
    from app.services.conflict.repository import ConflictRepository

    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1, pii_flag=False)
    metric.submitted_by = 1
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock()

    hard_conflict = Conflict(
        conflict_id="conf_001",
        type=ConflictType.SAME_DEF_DIFF_NAME,
        status=ConflictStatus.OPEN,
        severity="hard",
        block_publish=True,
        similarity_score=0.95,
        metric_codes={"candidate": "sales_gmv_daily", "existing": "sales_gmv_weekly"},
    )
    with (
        patch.object(
            ConflictRepository,
            "get_first_open_for_metric",
            new=AsyncMock(return_value=hard_conflict),
        ),
        pytest.raises(BusinessError) as exc,
    ):
        await svc.approve_metric(
            "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="platform_admin"
        )
    assert exc.value.error_code == "CONFLICT_BLOCKED"
    repo.update_with_optimistic_lock.assert_not_awaited()
    repo.mark_version_published.assert_not_awaited()


async def test_approve_metric_soft_conflict_allowed():
    """软冲突（block_publish=False）不阻断发布——仅硬冲突需要先协商/裁决。"""
    from app.models.conflict import Conflict, ConflictStatus, ConflictType
    from app.services.conflict.repository import ConflictRepository

    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1, pii_flag=False)
    metric.submitted_by = 1
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()

    soft_conflict = Conflict(
        conflict_id="conf_002",
        type=ConflictType.SAME_DEF_DIFF_NAME,
        status=ConflictStatus.OPEN,
        severity="soft",
        block_publish=False,
        similarity_score=0.7,
        metric_codes={"candidate": "sales_gmv_daily", "existing": "sales_gmv_weekly"},
    )
    with patch.object(
        ConflictRepository, "get_first_open_for_metric", new=AsyncMock(return_value=soft_conflict)
    ):
        result = await svc.approve_metric(
            "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="platform_admin"
        )
    assert result.status == "PUBLISHED"
    svc._publish_event.assert_awaited_once()


async def test_approve_metric_experimental_mode():
    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1, pii_flag=False)
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="EXPERIMENTAL"))
    repo.mark_version_published = AsyncMock()

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="experimental", gray_tenant_ids=[7]),
        actor_id=1,
        role="platform_admin",
    )
    assert result.status == "EXPERIMENTAL"
    assert repo.update_with_optimistic_lock.call_args.kwargs["gray_tenant_ids"] == [7]


async def test_approve_metric_self_review_blocked():
    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1, reviewer_id=1, reviewer_type="user")
    metric.submitted_by = 1
    repo.get_by_code = AsyncMock(return_value=metric)
    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="metric_owner"
        )
    assert exc.value.error_code == "SELF_REVIEW_BLOCKED"


async def test_approve_metric_pii_blocked_without_compliance():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="REVIEW", owner_id=1, pii_flag=True, compliance_reviewed=False
        )
    )
    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="platform_admin"
        )
    assert exc.value.error_code == "COMPLIANCE_BLOCKED"


async def test_reject_metric_success():
    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1)
    metric.submitted_by = 1
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="DRAFT"))
    svc._publish_event = AsyncMock()

    result = await svc.reject_metric(
        "sales_gmv_daily", MetricRejectRequest(reason="口径不符"), actor_id=1, role="platform_admin"
    )
    assert result.status == "DRAFT"
    assert svc._publish_event.call_args.args[0] == "metric.rejected"


async def test_promote_metric_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="EXPERIMENTAL", owner_id=1))
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()

    result = await svc.promote_metric("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert result.status == "PUBLISHED"
    assert svc._publish_event.call_args.args[0] == "metric.promoted"


async def test_rollback_metric_success():
    svc, repo = _svc_with_repo()
    metric = make_metric(status="EXPERIMENTAL", owner_id=1, version=2, effective_version=2)
    repo.get_by_code = AsyncMock(return_value=metric)
    prev_pub = MagicMock(
        version=1, status="PUBLISHED", definition_json={"expression": "sum(amount)"}
    )
    repo.list_versions = AsyncMock(return_value=[prev_pub])
    # 当前灰度版本的 diff_json 记录 granularity 的 before（供回滚恢复 top-level 字段）
    gray_diff = MagicMock(
        version=2, status="EXPERIMENTAL", definition_json={"expression": "avg(amount)"},
        diff_json={"granularity": {"before": "day", "after": "hour", "change_type": "breaking"}},
    )
    repo.get_version = AsyncMock(return_value=gray_diff)
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_archived = AsyncMock()
    svc._db.execute = AsyncMock()
    svc._publish_event = AsyncMock()
    svc._register_metric_lineage_full = AsyncMock()

    result = await svc.rollback_metric("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert result.status == "PUBLISHED"
    assert svc._publish_event.call_args.args[0] == "metric.rolled_back"
    # 回滚恢复上一 PUBLISHED 口径 + 版本号回退 + top-level before 值
    _kw = repo.update_with_optimistic_lock.call_args.kwargs
    assert _kw["definition_json"] == {"expression": "sum(amount)"}
    assert _kw["version"] == 1
    assert _kw["effective_version"] == 1
    assert _kw["granularity"] == "day"
    # 回滚后血缘按恢复口径差异同步
    svc._register_metric_lineage_full.assert_awaited_once()


async def test_delete_metric_success_and_reject():
    svc, repo = _svc_with_repo()
    repo.soft_delete = AsyncMock()
    svc._cleanup_metric_lineage = AsyncMock()

    # 原 Owner（创建者）可删自己的草稿
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
    result = await svc.delete_metric("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert result.status == "DRAFT"
    repo.soft_delete.assert_awaited_once()
    svc._cleanup_metric_lineage.assert_awaited_once_with("sales_gmv_daily")

    # 原 Owner 可删已废弃指标（未对外投入状态，对齐维度/度量决策）
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DEPRECATED", owner_id=1))
    result = await svc.delete_metric("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert result.status == "DEPRECATED"

    # 非 Owner 且非管理员 → FORBIDDEN
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
    with pytest.raises(BusinessError) as exc:
        await svc.delete_metric("sales_gmv_daily", actor_id=2, role="metric_owner")
    assert exc.value.error_code == "FORBIDDEN"

    # 平台/域管理员可删他人草稿
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=2))
    await svc.delete_metric("sales_gmv_daily", actor_id=9, role="domain_admin")

    # 审核中/启用中不可删（INVALID_STATE）
    for status in ("REVIEW", "PUBLISHED"):
        repo.get_by_code = AsyncMock(return_value=make_metric(status=status, owner_id=1))
        with pytest.raises(BusinessError) as exc:
            await svc.delete_metric("sales_gmv_daily", actor_id=1, role="platform_admin")
        assert exc.value.error_code == "INVALID_STATE"


async def test_delete_metric_already_deleted_friendly():
    """已软删记录（回收站）再走软删 → ALREADY_DELETED 而非裸「指标不存在」。

    回归保护：前端批量删除按 status=DRAFT 过滤曾误中软删记录（软删不改 status），
    逐条调软删接口导致整批 404「指标不存在」——后端应区分「重复删除」并给清晰提示。
    """
    svc, repo = _svc_with_repo()
    repo.soft_delete = AsyncMock()
    svc.get_metric = AsyncMock(side_effect=NotFoundError("指标不存在: sales_gmv_daily"))
    repo.get_archived_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", owner_id=1, deleted_at="2026-08-01T00:00:00")
    )

    with pytest.raises(BusinessError) as exc:
        await svc.delete_metric("sales_gmv_daily", actor_id=1, role="platform_admin")
    assert exc.value.error_code == "ALREADY_DELETED"
    assert "已处于删除状态" in str(exc.value)
    # 未误调软删
    repo.soft_delete.assert_not_awaited()


async def test_restore_metric_success_and_guards():
    # 平台管理员恢复已删草稿
    svc, repo = _svc_with_repo()
    repo.get_archived_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", owner_id=2, deleted_at="2026-08-01T00:00:00")
    )
    repo.restore_metric = AsyncMock()
    result = await svc.restore_metric("sales_gmv_daily", actor_id=1, role="platform_admin")
    assert result.status == "DRAFT"
    repo.restore_metric.assert_awaited_once()

    # 原 owner（非管理员）也可恢复
    repo.get_archived_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", owner_id=2, deleted_at="2026-08-01T00:00:00")
    )
    await svc.restore_metric("sales_gmv_daily", actor_id=2, role="metric_owner")

    # 非 owner 非管理员 → 拒绝
    repo.get_archived_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", owner_id=2, deleted_at="2026-08-01T00:00:00")
    )
    with pytest.raises(BusinessError) as e:
        await svc.restore_metric("sales_gmv_daily", actor_id=9, role="metric_owner")
    assert "仅平台管理员或指标原 Owner" in str(e.value)

    # 未删状态 → 拒绝
    repo.get_archived_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
    with pytest.raises(BusinessError):
        await svc.restore_metric("sales_gmv_daily", actor_id=1, role="platform_admin")

    # 非 DRAFT 已删 → 拒绝
    repo.get_archived_by_code = AsyncMock(
        return_value=make_metric(status="PUBLISHED", owner_id=1, deleted_at="2026-08-01T00:00:00")
    )
    with pytest.raises(BusinessError):
        await svc.restore_metric("sales_gmv_daily", actor_id=1, role="platform_admin")


async def test_purge_metric_guards():
    # 平台管理员彻底删除已删指标 → 成功（级联清理 + 缓存失效）
    svc, repo = _svc_with_repo()
    repo.get_archived_by_code = AsyncMock(
        return_value=make_metric(status="PUBLISHED", owner_id=1, deleted_at="2026-08-01T00:00:00")
    )
    repo.purge_metric = AsyncMock()
    await svc.purge_metric("sales_gmv_daily", actor_id=1, role="platform_admin")
    repo.purge_metric.assert_awaited_once_with(1, "sales_gmv_daily")

    # 未删状态 → 拒绝
    repo.get_archived_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
    with pytest.raises(BusinessError) as e:
        await svc.purge_metric("sales_gmv_daily", actor_id=1, role="platform_admin")
    assert "未处于已删除状态" in str(e.value)

    # 非平台管理员 → 拒绝
    repo.get_archived_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", owner_id=2, deleted_at="2026-08-01T00:00:00")
    )
    with pytest.raises(BusinessError) as e:
        await svc.purge_metric("sales_gmv_daily", actor_id=2, role="metric_owner")
    assert "仅平台管理员" in str(e.value)

    # 不存在 → NotFound
    repo.get_archived_by_code = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.purge_metric("sales_gmv_daily", actor_id=1, role="platform_admin")


async def test_get_versions():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.list_versions = AsyncMock(return_value=[MagicMock(version=1)])
    versions = await svc.get_versions("sales_gmv_daily")
    assert len(versions) == 1


async def test_get_version_responses_injects_confirm_progress():
    """版本历史响应注入多消费方确认进度（已确认 X/N）——修复前无进度字段，
    一方确认后另一方未确认、版本迟迟不转正时无法判断还差谁。"""
    from app.models.metric_version import MetricVersion

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    pending = MetricVersion(
        id=1,
        metric_id=5,
        version=2,
        status="PENDING_CONFIRMATION",
        change_type="breaking",
        definition_json={"expression": "SUM(x)"},
        change_reason="口径调整",
        created_by=9,
        created_at=datetime(2026, 8, 1, 10, 0, 0),
    )
    published = MetricVersion(
        id=2,
        metric_id=5,
        version=1,
        status="PUBLISHED",
        change_type="initial",
        definition_json={"expression": "SUM(x)"},
        change_reason="初版",
        created_by=9,
        created_at=datetime(2026, 7, 1, 10, 0, 0),
    )
    repo.list_versions = AsyncMock(return_value=[pending, published])
    repo.count_confirmations_by_versions = AsyncMock(
        return_value={2: (1, 2)}  # 版本 2：1/2 消费方已确认
    )
    responses = await svc.get_version_responses("sales_gmv_daily")
    assert len(responses) == 2
    # PENDING 版本带进度；已发布版本不带
    by_version = {r.version: r for r in responses}
    assert by_version[2].confirmed_count == 1
    assert by_version[2].consumer_count == 2
    assert by_version[1].confirmed_count is None
    assert by_version[1].consumer_count is None


async def test_get_version_responses_no_pending_skips_progress_query():
    """无 PENDING 版本时不做进度查询（避免无谓 DB 调用）。"""
    from app.models.metric_version import MetricVersion

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    published = MetricVersion(
        id=2,
        metric_id=5,
        version=1,
        status="PUBLISHED",
        change_type="initial",
        definition_json={"expression": "SUM(x)"},
        change_reason="初版",
        created_by=9,
        created_at=datetime(2026, 7, 1, 10, 0, 0),
    )
    repo.list_versions = AsyncMock(return_value=[published])
    repo.count_confirmations_by_versions = AsyncMock()
    responses = await svc.get_version_responses("sales_gmv_daily")
    assert len(responses) == 1
    repo.count_confirmations_by_versions.assert_not_called()


async def test_confirm_version_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="PENDING")]
    )
    repo.update_confirmation_status = AsyncMock()
    repo.get_version = AsyncMock(
        return_value=MagicMock(
            definition_json={"expression": "SUM(x)"},
            diff_json={"granularity": {"before": "daily", "after": "hourly"}},
        )
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric())
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()
    svc._publish_event = AsyncMock()

    result = await svc.confirm_version("sales_gmv_daily", version=1, consumer_id=9)
    assert result is not None


async def test_reject_version_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="PENDING")]
    )
    repo.update_confirmation_status = AsyncMock()
    svc._db.execute = AsyncMock()
    svc._publish_event = AsyncMock()

    await svc.reject_version("sales_gmv_daily", version=1, consumer_id=9, reason="口径变更")
    assert repo.update_confirmation_status.await_count == 1
    # 被拒版本置 CANCELLED（DB 更新执行）
    svc._db.execute.assert_awaited_once()


async def test_reject_version_terminates_other_confirmations():
    """拒绝后终结该版本其他消费方的 PENDING 确认记录——修复前其他记录残留
    PENDING，pending_version 计算字段（status==PENDING）持续识别为待确认，
    前端警示最长残留 14 天；修复后全部确认记录终止，警示立即消失。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    # A 拒绝（mine）、B 仍 PENDING（应被终结）、C 已 CONFIRMED（保持）
    repo.get_pending_confirmations = AsyncMock(
        return_value=[
            MagicMock(id=1, consumer_id=9, status="PENDING"),
            MagicMock(id=2, consumer_id=10, status="PENDING"),
            MagicMock(id=3, consumer_id=11, status="CONFIRMED"),
        ]
    )
    repo.update_confirmation_status = AsyncMock()
    svc._db.execute = AsyncMock()
    svc._publish_event = AsyncMock()

    await svc.reject_version("sales_gmv_daily", version=1, consumer_id=9, reason="口径变更")

    # 当前拒绝者(id=1) + 其他 PENDING 记录(id=2) 被终结为 REJECTED；
    # 已 CONFIRMED 的(id=3)保持不动（不覆盖已确认事实）
    assert repo.update_confirmation_status.await_count == 2
    assert repo.update_confirmation_status.await_args.args[0] == 2  # id=2 的记录被终结
    assert repo.update_confirmation_status.await_args.args[1] == "REJECTED"



async def test_extend_version_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[
            MagicMock(id=1, consumer_id=9, status="PENDING", extension_count=0, deadline=None)
        ]
    )
    repo.extend_confirmation_deadline = AsyncMock(return_value=MagicMock(version=1))

    result = await svc.extend_version("sales_gmv_daily", version=1, actor_id=1, role="metric_owner")
    assert result is not None


async def test_assert_owner_or_admin_rules():
    svc, _ = _svc_with_repo()
    metric = make_metric(owner_id=1, backup_owner_id=2)

    # admin 放行
    svc._assert_owner_or_admin(metric, actor_id=99, role="platform_admin")
    # owner 本人放行
    svc._assert_owner_or_admin(metric, actor_id=1, role="metric_owner")
    # backup owner 放行
    svc._assert_owner_or_admin(metric, actor_id=2, role="metric_owner")
    # 越权拒绝
    with pytest.raises(AuthError):
        svc._assert_owner_or_admin(metric, actor_id=3, role="metric_owner")
    # 无权限角色拒绝
    with pytest.raises(AuthError):
        svc._assert_owner_or_admin(metric, actor_id=1, role="viewer")


async def test_compute_diff_detects_changes():
    svc, _ = _svc_with_repo()
    diff = svc._compute_diff({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4})
    assert "b" in diff
    assert diff["b"]["before"] == 2
    assert diff["b"]["after"] == 3


async def test_get_metric_health_critical_emits_event(monkeypatch):
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    # get_metric_health 实时计算后落库（详情/目录一致性）
    repo.save_health_score = AsyncMock(return_value=None)

    class _FakeHealth:
        level = "CRITICAL"
        score = 30
        missing_dimensions = ["region"]

    class _FakeScorer:
        async def calculate(self, metric_id):
            return _FakeHealth()

    monkeypatch.setattr(
        "app.services.semantic.health_scorer.HealthScorer", lambda db: _FakeScorer()
    )
    svc._publish_event = AsyncMock()
    health = await svc.get_metric_health("sales_gmv_daily")
    assert health.level == "CRITICAL"
    assert svc._publish_event.call_args.args[0] == "metric.health_critical"


async def test_get_metric_health_archived_raises_metric_archived():
    """health 直访已作废指标（软删 + successor）→ 结构化 METRIC_ARCHIVED（与详情读路径一致）。"""
    from app.core.error_codes import ErrorCode

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    archived = make_metric(
        metric_code="sales_e2e_conflictb_day",
        successor_code="sales_e2e_conflicta_day",
    )
    archived.deleted_at = None  # 软删态由调用方保证；命中 archived 分支仅需 successor 存在
    archived.arbitration_mark = {"status": "defeated"}
    repo.get_archived_by_code = AsyncMock(return_value=archived)

    with pytest.raises(NotFoundError) as exc_info:
        await svc.get_metric_health("sales_e2e_conflictb_day")
    assert exc_info.value.error_code == ErrorCode.METRIC_ARCHIVED
    assert exc_info.value.ctx["successor_code"] == "sales_e2e_conflicta_day"
    assert exc_info.value.ctx["arbitration_mark"]["status"] == "defeated"


async def test_get_consumption_guide_generates_and_caches():
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get_guide = AsyncMock(return_value=None)
    svc._cache.set_guide = AsyncMock()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))

    guide = await svc.get_consumption_guide("sales_gmv_daily")
    assert guide["metric_code"] == "sales_gmv_daily"
    # 无人工维护指南时推荐用法为空（不再拼装模板文案伪装人工推荐）
    assert guide["recommended_usage"] == []
    assert guide["guide_source"] == "auto"
    svc._cache.set_guide.assert_awaited_once()


async def test_get_consumption_guide_cache_hit():
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get_guide = AsyncMock(return_value={"metric_code": "cached"})
    guide = await svc.get_consumption_guide("sales_gmv_daily")
    assert guide == {"metric_code": "cached"}
    repo.get_by_code.assert_not_called()


async def test_validate_domain_active_degraded(monkeypatch):
    """subject_domain 未配置（NotFoundError）→ 放行，不阻断创建。"""
    svc, _ = _svc_with_repo()

    class _Svc:
        async def validate_domain_active(self, code):
            raise NotFoundError("域未配置")

    monkeypatch.setattr(
        "app.services.subject_domain.service.SubjectDomainService", lambda db: _Svc()
    )
    # 不应抛异常
    await svc._validate_domain_active("sales")


async def test_get_domain_defaults_error_returns_empty(monkeypatch):
    svc, _ = _svc_with_repo()

    class _Boom:
        async def get_defaults(self, code):
            raise RuntimeError("DB down")

    monkeypatch.setattr(
        "app.services.subject_domain.service.SubjectDomainService", lambda db: _Boom()
    )
    assert await svc._get_domain_defaults("sales") == {}


async def test_validate_dict_fields_disabled_value_blocked(monkeypatch):
    """字典项已配置但停用 → BusinessError 拦截（P1-5 语义：类型有 active 项才校验）。"""
    svc, _ = _svc_with_repo()
    req = MetricCreateRequest(**make_create_payload())

    class _DictSvc:
        async def list_by_type(self, dict_type, status="active"):
            return [MagicMock()]  # 类型已配置（有 active 项）

        async def validate_dict_value(self, dict_type, code):
            raise BusinessError("字典项已停用", error_code="DICT_DISABLED")

    monkeypatch.setattr("app.services.system_dict.service.SystemDictService", lambda db: _DictSvc())
    with pytest.raises(BusinessError):
        await svc._validate_dict_fields(req)


async def test_validate_dict_fields_configured_but_value_missing_blocked(monkeypatch):
    """P1-5：类型已配置但值不存在 → NotFoundError 拦截（不再静默放行脏值）。"""
    svc, _ = _svc_with_repo()
    req = MetricCreateRequest(**make_create_payload())

    class _DictSvc:
        async def list_by_type(self, dict_type, status="active"):
            return [MagicMock()]  # 类型已配置

        async def validate_dict_value(self, dict_type, code):
            raise NotFoundError("字典值不存在", error_code="DICT_VALUE_NOT_FOUND")

    monkeypatch.setattr("app.services.system_dict.service.SystemDictService", lambda db: _DictSvc())
    with pytest.raises(NotFoundError):
        await svc._validate_dict_fields(req)


async def test_validate_dict_fields_unconfigured_type_passes(monkeypatch):
    """P1-5：类型完全未配置（空表/未种子，无 active 项）→ 放行，不阻断创建。"""
    svc, _ = _svc_with_repo()
    req = MetricCreateRequest(**make_create_payload())

    class _DictSvc:
        async def list_by_type(self, dict_type, status="active"):
            return []  # 类型未配置 → 该类型放行

        async def validate_dict_value(self, dict_type, code):
            raise AssertionError("类型未配置时不应走到值校验")

    monkeypatch.setattr("app.services.system_dict.service.SystemDictService", lambda db: _DictSvc())
    await svc._validate_dict_fields(req)  # 不应抛异常


async def test_generate_metric_code_with_source_table(monkeypatch):
    """自动生成编码：源表+度量+周期齐全 → 4 段式。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)

    async def _gen(base, exists):
        return base

    monkeypatch.setattr("app.core.codegen.generate_unique_code", _gen)
    monkeypatch.setattr("app.services.semantic.auto_fill.extract_biz_object", lambda t: "order")
    monkeypatch.setattr("app.services.semantic.auto_fill.extract_measure", lambda m: "amount")
    req = MetricCreateRequest(
        **make_create_payload(metric_code=None, source_table="dwd_order", period="day")
    )
    code = await svc._generate_metric_code(req)
    assert code == "sales_order_amount_day"


# ---- 补充覆盖率：紧急发布 / 公共读路径 / 合规官可达性 ----


async def test_get_metric_public_from_db_and_cache():
    """get_metric_public：DB 回源 + 缓存命中双路径。"""
    from app.services.semantic.schemas import MetricResponse

    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()

    # DB 回源
    svc._cache.get = AsyncMock(return_value=None)
    svc._cache.set = AsyncMock()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    resp = await svc.get_metric_public("sales_gmv_daily")
    assert isinstance(resp, MetricResponse)
    svc._cache.set.assert_awaited_once()

    # 缓存命中（完整响应 dict）
    full = MetricResponse.model_validate(make_metric()).model_dump()
    svc._cache.get = AsyncMock(return_value=full)
    resp2 = await svc.get_metric_public("sales_gmv_daily")
    assert resp2.metric_code == "sales_gmv_daily"


async def test_get_metric_public_attaches_measure_info():
    """get_metric_public：measure_id 非空时 best-effort 填充逻辑度量名称/编码（详情页展示）。"""
    from app.services.semantic.schemas import MetricResponse

    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get = AsyncMock(return_value=None)
    svc._cache.set = AsyncMock()
    repo.get_by_code = AsyncMock(return_value=make_metric(measure_id=7))
    # 度量目录命中：返回 (measure_code, name)；_svc_with_repo 默认 _db.execute 为空结果集
    svc._db.execute = AsyncMock(
        return_value=MagicMock(first=MagicMock(return_value=("pay_amt", "支付金额")))
    )

    resp = await svc.get_metric_public("sales_gmv_daily")
    assert isinstance(resp, MetricResponse)
    assert resp.measure_code == "pay_amt"
    assert resp.measure_name == "支付金额"
    # 度量查询失败降级：仅 measure_id，不阻断详情
    svc._db.execute = AsyncMock(side_effect=Exception("db down"))
    resp2 = await svc.get_metric_public("sales_gmv_daily")
    assert resp2.measure_id == 7
    assert resp2.measure_code is None


async def test_get_metric_public_not_found():
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get = AsyncMock(return_value=None)
    repo.get_by_code = AsyncMock(return_value=None)
    repo.get_archived_by_code = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.get_metric_public("missing")


async def test_get_metric_public_archived_raises_metric_archived():
    """详情直访已作废指标（软删 + successor）→ 结构化 METRIC_ARCHIVED，携带胜方指针。"""
    from app.core.error_codes import ErrorCode

    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get = AsyncMock(return_value=None)
    repo.get_by_code = AsyncMock(return_value=None)
    archived = make_metric(
        metric_code="sales_e2e_conflictb_day",
        successor_code="sales_e2e_conflicta_day",
    )
    archived.deleted_at = None  # 触发 archived 分支仅需 successor 存在；软删态由调用方保证
    archived.arbitration_mark = {
        "status": "defeated",
        "conflict_id": "CF-ABC",
        "opposite_code": "sales_e2e_conflicta_day",
    }
    repo.get_archived_by_code = AsyncMock(return_value=archived)

    with pytest.raises(NotFoundError) as exc_info:
        await svc.get_metric_public("sales_e2e_conflictb_day")

    assert exc_info.value.error_code == ErrorCode.METRIC_ARCHIVED
    assert exc_info.value.ctx["successor_code"] == "sales_e2e_conflicta_day"
    assert exc_info.value.ctx["metric_code"] == "sales_e2e_conflictb_day"
    assert exc_info.value.ctx["arbitration_mark"]["status"] == "defeated"


async def test_get_archived_metric_public_returns_detail_with_successor():
    """作废详情端点：返回完整历史口径 + successor 指针 + 裁决标记（供作废引导页展示）。"""
    svc, repo = _svc_with_repo()
    archived = make_metric(
        metric_code="sales_e2e_conflictb_day",
        name="E2E 冲突指标 B",
        successor_code="sales_e2e_conflicta_day",
        definition_json={"expression": "sum(order_amount)", "sql": "SELECT 1"},
    )
    archived.arbitration_mark = {
        "status": "defeated",
        "decision": "merge",
        "conflict_id": "CF-ABC",
        "opposite_code": "sales_e2e_conflicta_day",
    }
    repo.get_archived_by_code = AsyncMock(return_value=archived)

    data = await svc.get_archived_metric_public("sales_e2e_conflictb_day")

    assert data["successor_code"] == "sales_e2e_conflicta_day"
    assert data["arbitration_mark"]["decision"] == "merge"
    assert data["metric"].metric_code == "sales_e2e_conflictb_day"
    assert data["metric"].name == "E2E 冲突指标 B"
    assert data["metric"].definition_json["expression"] == "sum(order_amount)"
    repo.get_archived_by_code.assert_awaited_once_with("sales_e2e_conflictb_day")


async def test_get_archived_metric_public_missing_raises_not_found():
    """作废详情端点：指标不存在或未作废 → 仍抛 NOT_FOUND（非 METRIC_ARCHIVED）。"""
    svc, repo = _svc_with_repo()
    repo.get_archived_by_code = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError) as exc_info:
        await svc.get_archived_metric_public("missing")

    assert exc_info.value.error_code == "NOT_FOUND"


async def test_get_metric_public_deleted_without_successor_still_not_found():
    """软删但无 successor（手动删除/其他作废路径）→ 仍按普通 NOT_FOUND 处理。"""
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get = AsyncMock(return_value=None)
    repo.get_by_code = AsyncMock(return_value=None)
    archived = make_metric(metric_code="gone")
    archived.deleted_at = None  # 软删但无 successor_code（make_metric 默认 None）
    repo.get_archived_by_code = AsyncMock(return_value=archived)

    with pytest.raises(NotFoundError) as exc_info:
        await svc.get_metric_public("gone")
    assert exc_info.value.error_code == "NOT_FOUND"


async def test_emergency_publish_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", pii_flag=False))
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()
    svc._write_audit = AsyncMock()

    result = await svc.emergency_publish_metric(
        "sales_gmv_daily",
        MetricEmergencyPublishRequest(reason="生产系统故障需立即紧急发布处理", target_version=1),
        actor_id=1,
        role="domain_admin",
    )
    assert result.status == "PUBLISHED"
    kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert kwargs["emergency_publish"] is True
    assert kwargs["emergency_reason"] == "生产系统故障需立即紧急发布处理"
    svc._write_audit.assert_awaited_once()


async def test_emergency_publish_forbidden_role():
    svc, repo = _svc_with_repo()
    with pytest.raises(BusinessError):
        await svc.emergency_publish_metric(
            "sales_gmv_daily",
            MetricEmergencyPublishRequest(reason="生产系统故障需立即紧急发布处理"),
            actor_id=1,
            role="metric_owner",
        )


async def test_emergency_publish_pii_blocked_with_officer(monkeypatch):
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", pii_flag=True, compliance_reviewed=False)
    )

    async def _officer_exists(domain):
        return True

    monkeypatch.setattr(svc, "_has_active_compliance_officer", _officer_exists)
    with pytest.raises(BusinessError) as exc:
        await svc.emergency_publish_metric(
            "sales_gmv_daily",
            MetricEmergencyPublishRequest(reason="生产系统故障需立即紧急发布处理"),
            actor_id=1,
            role="domain_admin",
        )
    assert exc.value.error_code == "COMPLIANCE_BLOCKED"


async def test_emergency_publish_pii_internal_downgrade(monkeypatch):
    """合规官不可达：仅 INTERNAL 分级可降级发布。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="DRAFT", pii_flag=True, compliance_reviewed=False, serving_mode="INTERNAL"
        )
    )
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()
    svc._write_audit = AsyncMock()

    async def _no_officer(domain):
        return False

    monkeypatch.setattr(svc, "_has_active_compliance_officer", _no_officer)
    result = await svc.emergency_publish_metric(
        "sales_gmv_daily",
        MetricEmergencyPublishRequest(reason="生产系统故障需立即紧急发布处理"),
        actor_id=1,
        role="domain_admin",
    )
    assert result.status == "PUBLISHED"


async def test_emergency_publish_pii_non_internal_blocked(monkeypatch):
    """合规官不可达且非 INTERNAL 分级 → 拦截。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="DRAFT", pii_flag=True, compliance_reviewed=False, serving_mode="BATCH_ONLY"
        )
    )

    async def _no_officer(domain):
        return False

    monkeypatch.setattr(svc, "_has_active_compliance_officer", _no_officer)
    with pytest.raises(BusinessError) as exc:
        await svc.emergency_publish_metric(
            "sales_gmv_daily",
            MetricEmergencyPublishRequest(reason="生产系统故障需立即紧急发布处理"),
            actor_id=1,
            role="domain_admin",
        )
    assert exc.value.error_code == "COMPLIANCE_UNREACHABLE_DOWNGRADE"


async def test_complete_emergency_review_success():
    """P1-6: 紧急发布补审——写 emergency_reviewed_at，发布事件。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="PUBLISHED", emergency_publish=True, emergency_reviewed_at=None
        )
    )
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", emergency_publish=True)
    )
    svc._publish_event = AsyncMock()

    result = await svc.complete_emergency_review("sales_gmv_daily", actor_id=1, role="domain_admin")

    assert result.status == "PUBLISHED"
    kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert kwargs["emergency_reviewed_at"] is not None
    svc._publish_event.assert_awaited_once()
    event_type = svc._publish_event.await_args.args[0]
    assert event_type == "metric.emergency_reviewed"


async def test_complete_emergency_review_forbidden_role():
    """非管理角色补审 → 拒绝（与紧急发布同角色门禁）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="PUBLISHED", emergency_publish=True)
    )
    with pytest.raises(AuthError) as exc:
        await svc.complete_emergency_review("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert exc.value.error_code == "FORBIDDEN"


async def test_complete_emergency_review_not_emergency():
    """非紧急发布指标 → 拒绝补审。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="PUBLISHED", emergency_publish=False)
    )
    with pytest.raises(ConflictError) as exc:
        await svc.complete_emergency_review("sales_gmv_daily", actor_id=1, role="domain_admin")
    assert exc.value.error_code == "NOT_EMERGENCY_PUBLISHED"


async def test_complete_emergency_review_already_done():
    """已完成补审 → 拒绝重复补审。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="PUBLISHED", emergency_publish=True, emergency_reviewed_at=datetime.now(UTC)
        )
    )
    with pytest.raises(ConflictError) as exc:
        await svc.complete_emergency_review("sales_gmv_daily", actor_id=1, role="domain_admin")
    assert exc.value.error_code == "EMERGENCY_ALREADY_REVIEWED"


async def test_has_active_compliance_officer():
    """_has_active_compliance_officer：有活跃合规官返回 True，否则 False。"""
    svc, _ = _svc_with_repo()
    svc._db.execute = AsyncMock()

    # 无活跃合规官 → False
    svc._db.execute.return_value = MagicMock()
    svc._db.execute.return_value.scalar.return_value = 0
    assert await svc._has_active_compliance_officer("sales") is False

    # 有活跃合规官 → True
    svc._db.execute.return_value.scalar.return_value = 1
    assert await svc._has_active_compliance_officer("sales") is True


# =====================================================================
# 覆盖率补齐：semantic/service.py 84% → 90%+（未覆盖分支）
# =====================================================================

# ---- update_metric 破坏性变更分支（PUBLISHED → PENDING_CONFIRMATION）----


async def test_update_metric_published_breaking_def_creates_pending():
    """PUBLISHED + 口径破坏性变更 → PENDING_CONFIRMATION 版本
    + create_pending（含备份 Owner 消费方）。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        row_version=1,
        version=1,
        backup_owner_id=2,
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    fake_pvm = MagicMock()
    fake_pvm.create_pending = AsyncMock()

    with patch(
        "app.services.semantic.pending_version_manager.PendingVersionManager",
        return_value=fake_pvm,
    ):
        result = await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                definition_json={"expression": "SUM(refund_amount)"},
                change_reason="破坏性口径变更",
            ),
            actor_id=1,
            role="metric_owner",
        )

    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.status == "PENDING_CONFIRMATION"
    assert version_arg.change_type == "BREAKING"
    # create_pending(metric, version, consumer_ids) 位置参数
    assert fake_pvm.create_pending.call_args.args[2] == [1, 2]
    assert result.row_version == 2


async def test_update_metric_published_second_breaking_blocked_during_pending():
    """PENDING 确认期内再次发起破坏性变更 → 拒绝（METRIC_PENDING_VERSION_EXISTS）。

    修复前：多个 PENDING 版本并存，转正低版本号会把主表 version 回退并覆盖
    高版本口径（版本历史倒挂、已确认的高版本变更丢失）。
    """
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        row_version=1,
        version=2,  # 主表 version 已在第一次 PENDING 时递增预留
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    # 已有未转正的 PENDING_CONFIRMATION 版本
    repo.has_pending_version = AsyncMock(return_value=True)

    with pytest.raises(ConflictError) as exc:
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                definition_json={"expression": "SUM(refund_amount)"},
                change_reason="PENDING 期内二次破坏性变更",
            ),
            actor_id=1,
            role="metric_owner",
        )
    assert exc.value.error_code == "METRIC_PENDING_VERSION_EXISTS"
    # 未创建版本记录、未创建新 PENDING（拒绝在创建前）
    repo.create_version.assert_not_called()
    repo.update_with_optimistic_lock.assert_not_called()


async def test_update_metric_pending_notifies_consumers():
    """PUBLISHED 破坏性变更创建 PENDING 后，定向通知消费方（Owner/备份 Owner）。

    修复前：create_pending 只建确认记录不通知（pending_version_manager TODO），
    消费方在 14 天确认期内不知情只能被动等超时——确认期闭环断裂。
    """
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        row_version=1,
        version=1,
        owner_id=2,
        backup_owner_id=3,
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    fake_pvm = MagicMock()
    fake_pvm.create_pending = AsyncMock()

    with patch(
        "app.services.semantic.pending_version_manager.PendingVersionManager",
        return_value=fake_pvm,
    ):
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                definition_json={"expression": "SUM(refund_amount)"},
                change_reason="破坏性口径变更",
            ),
            actor_id=2,  # owner 本人发起变更
            role="metric_owner",
        )

    # 通知消费方（owner=2 + backup=3），跳过发起变更的 actor（2 已发起已知晓）
    svc._notify_pending_consumers.assert_awaited_once_with(
        metric_code="sales_gmv_daily",
        version=2,
        consumer_ids=[2, 3],
        skip_actor=2,
    )


async def test_update_metric_published_breaking_defers_lineage_registration():
    """PUBLISHED + 口径破坏性变更 → PENDING 期不立即注册血缘（延迟到转正后）。

    修复前：update_metric 无条件按新口径注册血缘 → PENDING 期血缘图显示"未来口径"
    （误导影响分析），且消费方拒绝后被拒口径的边已注册/旧边已删（错误残留）。
    """
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        row_version=1,
        version=1,
        backup_owner_id=2,
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    fake_pvm = MagicMock()
    fake_pvm.create_pending = AsyncMock()

    with (
        patch(
            "app.services.semantic.pending_version_manager.PendingVersionManager",
            return_value=fake_pvm,
        ),
        patch.object(
            MetricService, "_register_metric_lineage_full", AsyncMock()
        ) as mock_lineage,
    ):
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                definition_json={"expression": "SUM(refund_amount)"},
                change_reason="破坏性口径变更",
            ),
            actor_id=1,
            role="metric_owner",
        )
    # PENDING 期新口径未生效 → 不应注册血缘（由 _promote_pending_version 转正后注册）
    mock_lineage.assert_not_awaited()


async def test_update_metric_draft_breaking_registers_lineage_immediately():
    """DRAFT + 破坏性口径变更（不触发 PENDING，立即生效）→ 立即注册血缘。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="DRAFT",
        row_version=1,
        version=1,
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    with patch.object(
        MetricService, "_register_metric_lineage_full", AsyncMock()
    ) as mock_lineage:
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                definition_json={"expression": "SUM(refund_amount)"},
                change_reason="草稿口径修正",
            ),
            actor_id=1,
            role="metric_owner",
        )
    # DRAFT 变更立即生效 → 立即注册血缘
    mock_lineage.assert_awaited_once()


async def test_update_metric_published_top_level_breaking_creates_pending():
    """PUBLISHED + top-level 破坏性字段（granularity）变更且无
    definition_json → PENDING + 结构化 diff。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        row_version=1,
        version=1,
        granularity="daily",
        backup_owner_id=2,
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    fake_pvm = MagicMock()
    fake_pvm.create_pending = AsyncMock()

    with patch(
        "app.services.semantic.pending_version_manager.PendingVersionManager",
        return_value=fake_pvm,
    ):
        result = await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(granularity="hourly", change_reason="粒度变更"),
            actor_id=1,
            role="metric_owner",
        )

    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.status == "PENDING_CONFIRMATION"
    assert version_arg.change_type == "BREAKING"
    assert version_arg.diff_json["granularity"]["before"] == "daily"
    assert version_arg.diff_json["granularity"]["after"] == "hourly"
    assert fake_pvm.create_pending.call_args.args[2] == [1, 2]
    assert result.row_version == 2


async def test_update_metric_published_aggregation_breaking_creates_pending():
    """PUBLISHED + 直接改 aggregation（聚合方式）→ 触发 PENDING_CONFIRMATION。

    修复前：aggregation 被误归治理属性（BREAKING_TOP_LEVEL_FIELDS 不含它），
    直接修改静默更新主表、不触发版本确认——而 definition_json 路径的
    BREAKING_DEF_FIELDS 判 aggregation 为 BREAKING，两条路径判定矛盾
    （SUM→AVG 本质是口径变更）。R40 修复后与 granularity/unit 同级触发 PENDING。
    """
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        row_version=1,
        version=1,
        aggregation="SUM",
        backup_owner_id=2,
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    fake_pvm = MagicMock()
    fake_pvm.create_pending = AsyncMock()

    with patch(
        "app.services.semantic.pending_version_manager.PendingVersionManager",
        return_value=fake_pvm,
    ):
        result = await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(aggregation="AVG", change_reason="聚合方式调整"),
            actor_id=1,
            role="metric_owner",
        )

    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.status == "PENDING_CONFIRMATION"
    assert version_arg.change_type == "BREAKING"
    assert version_arg.diff_json["aggregation"]["before"] == "SUM"
    assert version_arg.diff_json["aggregation"]["after"] == "AVG"
    # 主表 aggregation 不应被直写（进入 PENDING，等消费方确认后转正）
    assert repo.update_with_optimistic_lock.await_args.kwargs.get("aggregation") is None
    assert result.row_version == 2



async def test_update_metric_syncs_pii_flag_from_definition():
    """definition_json 显式声明 pii=True 时，pii_flag 以权威源同步到 updates。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="DRAFT",
        row_version=1,
        version=1,
        pii_flag=False,
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            definition_json={"expression": "SUM(order_amount)", "pii": True},
            change_reason="补充 PII 标记",
        ),
        actor_id=1,
        role="metric_owner",
    )
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["pii_flag"] is True


async def test_update_metric_collects_optional_fields():
    """非 None 的可选字段（name/sla/backup_owner_id）进入 updates。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=1, version=1)
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            name="新用户数",
            sla="08:00",
            backup_owner_id=5,
            change_reason="调整元数据",
        ),
        actor_id=1,
        role="metric_owner",
    )
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["name"] == "新用户数"
    assert kwargs["sla"] == "08:00"
    assert kwargs["backup_owner_id"] == 5


async def test_update_metric_applies_governance_fields_without_version():
    """治理字段（dw_layer/freshness/metric_tier/currency 等）更新主表治理列，
    不触发版本递增（非破坏性变更）——修复创建后治理字段不可改的缺口。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=1, version=1)
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            dw_layer="DWS",
            freshness="T0",
            metric_tier="T1",
            currency="CNY",
            change_reason="治理字段调整：分层纠正+时效调整+分级晋升+币种修正",
        ),
        actor_id=1,
        role="metric_owner",
    )
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["dw_layer"] == "DWS"
    assert kwargs["freshness"] == "T0"
    assert kwargs["metric_tier"] == "T1"
    assert kwargs["currency"] == "CNY"
    # 非破坏性治理变更不触发版本递增（不在 BREAKING_TOP_LEVEL_FIELDS）
    assert "version" not in kwargs
    repo.create_version.assert_not_called()


async def test_update_metric_rename_clears_rename_required_mark():
    """仲裁「保留差异+指定改名」的指标，Owner 改名后清除 rename_required 标记（TD §12.4）。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="DRAFT",
        row_version=1,
        version=1,
        name="旧名称",
        arbitration_mark={
            "status": "coexist",
            "conflict_id": "CF-RENAME",
            "rename_required": True,
            "rename_opposite_code": "gmv_total",
        },
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=2, name="新用户数")
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(name="新用户数", change_reason="响应仲裁改名要求"),
        actor_id=1,
        role="metric_owner",
    )
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["name"] == "新用户数"
    # rename_required 被清除并记录 resolved_at（幂等闭环）
    mark = kwargs["arbitration_mark"]
    assert mark["rename_required"] is False
    assert mark["resolved_at"]


async def test_update_metric_rename_keeps_mark_when_name_unchanged():
    """未改名（name 不变）时不误清 rename_required 标记——仅真正改名才触发清除。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="DRAFT",
        row_version=1,
        version=1,
        name="原名称",
        arbitration_mark={
            "status": "coexist",
            "conflict_id": "CF-RENAME",
            "rename_required": True,
            "rename_opposite_code": "gmv_total",
        },
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(name="原名称", change_reason="仅同步其他字段"),
        actor_id=1,
        role="metric_owner",
    )
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert "arbitration_mark" not in kwargs  # 未改名 → 不触碰仲裁标记


# ---- 状态机非法跃迁（submit/review/reject/promote/rollback/deprecate）----


async def test_submit_metric_invalid_transition():
    """非 DRAFT 状态提交（如 PUBLISHED）→ ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED", owner_id=1))
    with pytest.raises(ConflictError) as exc:
        await svc.submit_metric(
            "sales_gmv_daily",
            MetricSubmitRequest(change_reason="提交评审"),
            actor_id=1,
            role="metric_owner",
            user_domain="sales",
        )
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_reject_metric_invalid_transition():
    """非 REVIEW 状态驳回（如 EXPERIMENTAL）→ ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="EXPERIMENTAL"))
    with pytest.raises(ConflictError) as exc:
        await svc.reject_metric(
            "sales_gmv_daily",
            MetricRejectRequest(reason="口径不符"),
            actor_id=1,
            role="platform_admin",
        )
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_promote_metric_invalid_transition():
    """非 EXPERIMENTAL 状态 promote → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT"))
    with pytest.raises(ConflictError) as exc:
        await svc.promote_metric("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_rollback_metric_invalid_transition():
    """非 EXPERIMENTAL 状态 rollback → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT"))
    with pytest.raises(ConflictError) as exc:
        await svc.rollback_metric("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_rollback_metric_no_previous_published():
    """EXPERIMENTAL 回退但无上一 PUBLISHED 版本 → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="EXPERIMENTAL", version=2))
    repo.list_versions = AsyncMock(return_value=[MagicMock(status="EXPERIMENTAL", version=2)])
    with pytest.raises(ConflictError) as exc:
        await svc.rollback_metric("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert exc.value.error_code == "NO_PREVIOUS_PUBLISHED_VERSION"


async def test_recycle_expired_gray_success():
    """P1-7: 灰度超期回收——EXPERIMENTAL → DRAFT，清灰度白名单，发布事件。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="EXPERIMENTAL", gray_tenant_ids=[1, 2])
    )
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", gray_tenant_ids=None)
    )
    svc._publish_event = AsyncMock()

    result = await svc.recycle_expired_gray("sales_gmv_daily", actor_id=0)

    assert result.status == "DRAFT"
    kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert kwargs["status"] == "DRAFT"
    assert kwargs["gray_tenant_ids"] is None
    svc._publish_event.assert_awaited_once()
    assert svc._publish_event.await_args.args[0] == "metric.gray_recycled"


async def test_recycle_expired_gray_non_experimental_rejected():
    """非 EXPERIMENTAL 状态回收 → ConflictError（INVALID_TRANSITION）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    with pytest.raises(ConflictError) as exc:
        await svc.recycle_expired_gray("sales_gmv_daily", actor_id=0)
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_reject_metric_experimental_still_blocked():
    """P1-7: EXPERIMENTAL→DRAFT 虽入状态机，reject 通道仍仅限 REVIEW（回收走系统路径）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="EXPERIMENTAL"))
    with pytest.raises(ConflictError) as exc:
        await svc.reject_metric(
            "sales_gmv_daily",
            MetricRejectRequest(reason="口径不符"),
            actor_id=1,
            role="platform_admin",
        )
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_deprecate_metric_invalid_transition():
    """非 PUBLISHED 状态废弃（如 DRAFT）→ ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT"))
    with pytest.raises(ConflictError) as exc:
        await svc.deprecate_metric(
            "sales_gmv_daily",
            successor_code="sales_gmv_amount_daily",
            actor_id=1,
            role="metric_owner",
        )
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_deprecate_metric_successor_not_found():
    """替代指标不存在 → NotFoundError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.get_by_code.side_effect = [
        make_metric(status="PUBLISHED"),  # 原指标
        None,  # successor 查询
    ]
    with pytest.raises(NotFoundError):
        await svc.deprecate_metric(
            "sales_gmv_daily", successor_code="not_exist_code", actor_id=1, role="metric_owner"
        )


# ---- approve_metric 派生/复合指标依赖校验（921-938 / 944 / 975）----


async def test_approve_derived_metric_unpublished_dependency():
    """派生指标发布时依赖未发布 → DEPENDENCY_NOT_PUBLISHED。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="REVIEW",
        owner_id=1,
        pii_flag=False,
        type="derived",
        definition_json={"dependencies": ["sales_gmv_amount_daily"]},
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    fake_checker = MagicMock()
    fake_checker.check_dependencies_published = AsyncMock(return_value=["sales_gmv_amount_daily"])
    with (
        patch(
            "app.services.semantic.dependency_checker.DependencyChecker",
            return_value=fake_checker,
        ),
        pytest.raises(BusinessError) as exc,
    ):
        await svc.approve_metric(
            "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="platform_admin"
        )
    assert exc.value.error_code == "DEPENDENCY_NOT_PUBLISHED"


async def test_approve_derived_metric_cycle():
    """派生指标发布时检测到循环依赖 → CYCLIC_DEPENDENCY。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="REVIEW",
        owner_id=1,
        pii_flag=False,
        type="composite",
        definition_json={"dependencies": ["sales_gmv_amount_daily"]},
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    fake_checker = MagicMock()
    fake_checker.check_dependencies_published = AsyncMock(return_value=[])
    fake_checker.detect_cycle = AsyncMock(return_value=["a", "b", "a"])
    with (
        patch(
            "app.services.semantic.dependency_checker.DependencyChecker",
            return_value=fake_checker,
        ),
        pytest.raises(BusinessError) as exc,
    ):
        await svc.approve_metric(
            "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="platform_admin"
        )
    assert exc.value.error_code == "CYCLIC_DEPENDENCY"


async def test_approve_composite_metric_invalid_formula():
    """复合指标发布时公式引用非指标标识符（裸表字段）→ INVALID_COMPOSITE_FORMULA。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="REVIEW",
        owner_id=1,
        pii_flag=False,
        type="composite",
        definition_json={
            "dependencies": ["sales_gmv_amount_daily"],
            "expression": "amount / head_amount",
        },
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    fake_checker = MagicMock()
    fake_checker.check_dependencies_published = AsyncMock(return_value=[])
    fake_checker.detect_cycle = AsyncMock(return_value=None)
    fake_checker.validate_composite_formula = AsyncMock(
        return_value=["公式引用非指标标识符「amount」（复合公式仅允许派生/复合指标 code）"]
    )
    with (
        patch(
            "app.services.semantic.dependency_checker.DependencyChecker",
            return_value=fake_checker,
        ),
        pytest.raises(BusinessError) as exc,
    ):
        await svc.approve_metric(
            "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="platform_admin"
        )
    assert exc.value.error_code == "INVALID_COMPOSITE_FORMULA"
    assert "amount" in exc.value.message


async def test_approve_composite_metric_valid_formula_passes():
    """复合指标公式仅引用已存在派生指标 code → 正常发布（不触发公式校验拦截）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="REVIEW",
        owner_id=1,
        pii_flag=False,
        type="composite",
        definition_json={
            "dependencies": ["sales_gmv_amount_daily", "sales_order_cnt_daily"],
            "expression": "sales_gmv_amount_daily / sales_order_cnt_daily",
        },
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()
    fake_checker = MagicMock()
    fake_checker.check_dependencies_published = AsyncMock(return_value=[])
    fake_checker.detect_cycle = AsyncMock(return_value=None)
    fake_checker.validate_composite_formula = AsyncMock(return_value=[])
    with (
        patch(
            "app.services.semantic.dependency_checker.DependencyChecker",
            return_value=fake_checker,
        ),
    ):
        result = await svc.approve_metric(
            "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="platform_admin"
        )
    fake_checker.validate_composite_formula.assert_awaited_once()
    assert result is not None


async def test_approve_derived_metric_emits_dependencies():
    """派生指标正常发布 → 事件 payload 携带 dependencies（975）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="REVIEW",
        owner_id=1,
        pii_flag=False,
        type="derived",
        definition_json={"dependencies": ["sales_gmv_amount_daily"]},
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()
    fake_checker = MagicMock()
    fake_checker.check_dependencies_published = AsyncMock(return_value=[])
    fake_checker.detect_cycle = AsyncMock(return_value=None)
    with patch(
        "app.services.semantic.dependency_checker.DependencyChecker",
        return_value=fake_checker,
    ):
        result = await svc.approve_metric(
            "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="platform_admin"
        )
    assert result.status == "PUBLISHED"
    payload = svc._publish_event.call_args.args[1]
    assert payload["dependencies"] == ["sales_gmv_amount_daily"]


async def test_approve_metric_version_not_found():
    """approve 时目标版本（当前待审核版本）记录缺失 → NotFoundError。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1, pii_flag=False)
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(target_version=1),
            actor_id=1,
            role="platform_admin",
        )


# ---- emergency_publish 边界 ----


async def test_emergency_publish_invalid_status():
    """紧急发布非 DRAFT/REVIEW 状态（如 PUBLISHED）→ ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED", pii_flag=False))
    with pytest.raises(ConflictError) as exc:
        await svc.emergency_publish_metric(
            "sales_gmv_daily",
            MetricEmergencyPublishRequest(reason="生产系统故障需立即紧急发布处理"),
            actor_id=1,
            role="domain_admin",
        )
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_emergency_publish_version_not_found():
    """紧急发布时目标版本（当前版本）记录缺失 → NotFoundError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", pii_flag=False))
    repo.get_version = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.emergency_publish_metric(
            "sales_gmv_daily",
            MetricEmergencyPublishRequest(
                reason="生产系统故障需立即紧急发布处理", target_version=1
            ),
            actor_id=1,
            role="domain_admin",
        )


async def test_approve_metric_rejects_historical_target_version():
    """审批经 API 直调传历史版本号 → ConflictError INVALID_TARGET_VERSION（防版本历史篡改）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1, pii_flag=False, version=2)
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=None)
    with pytest.raises(ConflictError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(target_version=1),
            actor_id=1,
            role="platform_admin",
        )
    assert exc.value.error_code == "INVALID_TARGET_VERSION"


async def test_emergency_publish_rejects_historical_target_version():
    """紧急发布传历史版本号 → ConflictError INVALID_TARGET_VERSION（防口径/版本矛盾）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", pii_flag=False, version=2)
    )
    repo.get_version = AsyncMock(return_value=None)
    with pytest.raises(ConflictError) as exc:
        await svc.emergency_publish_metric(
            "sales_gmv_daily",
            MetricEmergencyPublishRequest(
                reason="生产系统故障需立即紧急发布处理", target_version=1
            ),
            actor_id=1,
            role="domain_admin",
        )
    assert exc.value.error_code == "INVALID_TARGET_VERSION"


# ---- confirm_version 边界 ----


async def test_confirm_version_no_pending():
    """版本无待确认记录 → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(return_value=[])
    with pytest.raises(ConflictError) as exc:
        await svc.confirm_version("sales_gmv_daily", version=1, consumer_id=9)
    assert exc.value.error_code == "NO_PENDING_CONFIRMATION"


async def test_confirm_version_no_mine():
    """当前用户无待确认记录 → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="PENDING")]
    )
    with pytest.raises(ConflictError) as exc:
        await svc.confirm_version("sales_gmv_daily", version=1, consumer_id=888)
    assert exc.value.error_code == "NO_PENDING_CONFIRMATION"


async def test_confirm_version_rejected_cannot_reconfirm():
    """已拒绝的消费方不可再次确认——拒绝决定不可静默撤销（防 CANCELLED 版本误转正）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="REJECTED")]
    )
    repo.update_confirmation_status = AsyncMock()
    with pytest.raises(ConflictError) as exc:
        await svc.confirm_version("sales_gmv_daily", version=1, consumer_id=9)
    assert exc.value.error_code == "NO_PENDING_CONFIRMATION"
    repo.update_confirmation_status.assert_not_called()


async def test_confirm_version_already_confirmed_idempotent():
    """已确认的确认记录幂等返回（不重复转正）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric()
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="CONFIRMED")]
    )
    repo.update_confirmation_status = AsyncMock()
    result = await svc.confirm_version("sales_gmv_daily", version=1, consumer_id=9)
    assert result is metric
    repo.update_confirmation_status.assert_not_called()


async def test_confirm_version_partial_returns_without_promote():
    """部分消费方确认（未全部）→ 返回 metric，不触发版本转正。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[
            MagicMock(id=1, consumer_id=9, status="PENDING"),
            MagicMock(id=2, consumer_id=3, status="PENDING"),
        ]
    )
    repo.update_confirmation_status = AsyncMock()
    result = await svc.confirm_version("sales_gmv_daily", version=1, consumer_id=9)
    assert result is not None
    repo.update_with_optimistic_lock.assert_not_called()


async def test_confirm_version_lock_conflict_rolls_back():
    """全部确认后转正遇乐观锁冲突 → 回滚确认状态为 PENDING 并重新抛出。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="PENDING")]
    )
    repo.update_confirmation_status = AsyncMock()
    repo.get_version = AsyncMock(
        return_value=MagicMock(definition_json={"expression": "SUM(x)"}, diff_json={})
    )
    repo.update_with_optimistic_lock = AsyncMock(side_effect=ConflictError("乐观锁冲突"))
    with pytest.raises(ConflictError):
        await svc.confirm_version("sales_gmv_daily", version=1, consumer_id=9)
    # 回滚确认状态为 PENDING（位置参数 mine.id, status）
    assert repo.update_confirmation_status.call_args.args[1] == "PENDING"


# ---- auto_accept_timeout / _promote_pending_version（并行进程新增强）----


async def test_auto_accept_timeout_promotes_when_all_accepted():
    """超时自动接受：全部 PENDING 置 TIMEOUT_ACCEPTED 后转正。"""
    svc, repo = _svc_with_repo()
    repo.get_by_id = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.get_pending_confirmations = AsyncMock(
        side_effect=[
            [MagicMock(id=1, status="PENDING")],  # 首次读取：有 PENDING
            [MagicMock(id=1, status="TIMEOUT_ACCEPTED")],  # 重读：全部已接受
        ]
    )
    repo.update_confirmation_status = AsyncMock()
    repo.get_version = AsyncMock(
        return_value=MagicMock(definition_json={"expression": "SUM(x)"}, diff_json={})
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric())
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()

    result = await svc.auto_accept_timeout(metric_id=1, version=1)
    assert result is not None
    assert repo.update_confirmation_status.call_args.args[1] == "TIMEOUT_ACCEPTED"


async def test_auto_accept_timeout_all_confirmed_promotes():
    """无 PENDING 可标记但全部已确认 → 直接转正（返回更新后指标）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_id = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.get_pending_confirmations = AsyncMock(return_value=[MagicMock(id=1, status="CONFIRMED")])
    repo.update_confirmation_status = AsyncMock()
    repo.get_version = AsyncMock(
        return_value=MagicMock(definition_json={"expression": "SUM(x)"}, diff_json={})
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric())
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()

    result = await svc.auto_accept_timeout(metric_id=1, version=1)
    assert result is not None
    # 无 PENDING → 不写 TIMEOUT_ACCEPTED
    assert repo.update_confirmation_status.await_count == 0


async def test_auto_accept_timeout_no_confirmations_returns_none():
    """版本无待确认记录 → 返回 None（不抛错）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_id = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.get_pending_confirmations = AsyncMock(return_value=[])
    result = await svc.auto_accept_timeout(metric_id=1, version=1)
    assert result is None


async def test_auto_accept_timeout_metric_not_found():
    """指标不存在 → NotFoundError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_id = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.auto_accept_timeout(metric_id=999, version=1)


async def test_promote_pending_version_applies_top_level_after():
    """_promote_pending_version：应用版本口径 + top-level 破坏性字段 after 值回写主表。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(row_version=1, version=1)
    repo.get_version = AsyncMock(
        return_value=MagicMock(
            definition_json={"expression": "SUM(refund_amount)"},
            diff_json={"granularity": {"before": "daily", "after": "hourly"}},
        )
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(version=2))
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()

    result = await svc._promote_pending_version(metric, version=2)
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["effective_version"] == 2
    assert kwargs["version"] == 2
    assert kwargs["definition_json"]["expression"] == "SUM(refund_amount)"
    assert kwargs["granularity"] == "hourly"  # top-level diff after 回写
    assert result.version == 2


async def test_promote_pending_version_notifies_all_consumers():
    """转正（确认/超时默认接受）后全量通知消费方新口径已生效（skip_actor=None）。

    修复前：auto_accept_timeout 超时默认接受后新口径悄然生效，消费方无通知
    ——与"创建 PENDING 通知"不对称（超时场景消费方未主动确认、最需要被告知）。
    """
    svc, repo = _svc_with_repo()
    metric = make_metric(
        row_version=1, version=1, owner_id=2, backup_owner_id=3
    )
    repo.get_version = AsyncMock(
        return_value=MagicMock(
            definition_json={"expression": "SUM(refund_amount)"},
            diff_json={},
        )
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(version=2))
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()

    await svc._promote_pending_version(metric, version=2)

    svc._notify_pending_consumers.assert_awaited_once_with(
        metric_code=metric.metric_code,
        version=2,
        consumer_ids=[2, 3],  # owner + backup，全量通知（含未主动确认者）
        skip_actor=None,
        event_type="metric.breaking_change_promoted",
        title="指标口径变更已生效",
        extra_payload={"effective_version": 2},
    )


async def test_promote_pending_version_writes_promote_audit():
    """转正（新口径正式生效）写审计——修复前转正动作零审计，
    超时自动转正（定时任务、无用户操作）14 天后口径悄然生效不可追溯。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(row_version=1, version=1, owner_id=2)
    repo.get_version = AsyncMock(
        return_value=MagicMock(
            definition_json={"expression": "SUM(refund_amount)"},
            diff_json={},
        )
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(version=2))
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()
    svc._write_audit = AsyncMock()

    # 超时触发场景：actor 为指标 owner，trigger=timeout
    await svc._promote_pending_version(
        metric, version=2, trigger="timeout", actor_id=metric.owner_id
    )
    _, kwargs = svc._write_audit.call_args
    assert kwargs["action"] == "metric_definition.promote_version"
    assert kwargs["entity_type"] == "metric_definition"
    assert kwargs["entity_id"] == metric.metric_code
    assert kwargs["actor_id"] == 2
    assert kwargs["detail"]["version"] == 2
    assert kwargs["detail"]["trigger"] == "timeout"


async def test_confirm_version_promote_passes_consumer_trigger():
    """消费方全部确认触发转正：_promote_pending_version 以 consumer_confirm +
    最后确认的消费方为 actor 调用（审计可追溯谁触发了生效）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(row_version=1, version=2)
    svc.get_metric = AsyncMock(return_value=metric)
    repo.get_pending_confirmations = AsyncMock(
        return_value=[
            MagicMock(id=1, consumer_id=2, status="CONFIRMED"),
            MagicMock(id=2, consumer_id=3, status="PENDING"),
        ]
    )
    repo.update_confirmation_status = AsyncMock()
    svc._promote_pending_version = AsyncMock(return_value=metric)

    await svc.confirm_version(metric.metric_code, 2, consumer_id=3)

    svc._promote_pending_version.assert_awaited_once_with(
        metric, 2, trigger="consumer_confirm", actor_id=3
    )


async def test_auto_accept_promote_passes_timeout_trigger():
    """超时自动转正：_promote_pending_version 以 timeout 触发 + owner 为 actor
    调用（审计可追溯超时自动生效，非用户操作）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        row_version=1, version=2, owner_id=2, backup_owner_id=3, status="PUBLISHED"
    )
    repo.get_by_id = AsyncMock(return_value=metric)
    repo.get_pending_confirmations = AsyncMock(
        return_value=[
            MagicMock(id=1, consumer_id=2, status="TIMEOUT_ACCEPTED"),
            MagicMock(id=2, consumer_id=3, status="TIMEOUT_ACCEPTED"),
        ]
    )
    repo.update_confirmation_status = AsyncMock()
    svc._promote_pending_version = AsyncMock(return_value=metric)

    await svc.auto_accept_timeout(metric.id, 2)

    svc._promote_pending_version.assert_awaited_once_with(
        metric, 2, trigger="timeout", actor_id=2
    )


async def test_promote_pending_version_not_found():
    """_promote_pending_version 版本不存在 → NotFoundError。"""
    svc, repo = _svc_with_repo()
    repo.get_version = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc._promote_pending_version(make_metric(), version=99)


async def test_reject_version_no_pending():
    """reject 无待确认记录 → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(return_value=[])
    with pytest.raises(ConflictError):
        await svc.reject_version("sales_gmv_daily", version=1, consumer_id=9, reason="不认可")


async def test_reject_version_no_mine():
    """reject 当前用户无记录 → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="PENDING")]
    )
    with pytest.raises(ConflictError):
        await svc.reject_version("sales_gmv_daily", version=1, consumer_id=888, reason="不认可")


async def test_extend_version_no_pending():
    """extend 无待确认记录 → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(return_value=[])
    with pytest.raises(ConflictError):
        await svc.extend_version("sales_gmv_daily", version=1, actor_id=1, role="metric_owner")


async def test_extend_version_limit_reached():
    """已延期满 1 次 → EXTEND_LIMIT_REACHED。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[
            MagicMock(id=1, consumer_id=9, status="PENDING", extension_count=1, deadline=None)
        ]
    )
    with pytest.raises(ConflictError) as exc:
        await svc.extend_version("sales_gmv_daily", version=1, actor_id=1, role="metric_owner")
    assert exc.value.error_code == "EXTEND_LIMIT_REACHED"


# ---- _is_breaking_change 依赖集合比较 / compare_metrics similar ----


async def test_is_breaking_change_dependencies_set_diff():
    """依赖项按集合比较：顺序不同不算破坏，集合不同算破坏。"""
    svc, _ = _svc_with_repo()
    # 顺序不同但集合相同 → 非破坏
    assert (
        svc._is_breaking_change({"dependencies": ["a", "b"]}, {"dependencies": ["b", "a"]}) is False
    )
    # 集合不同 → 破坏
    assert (
        svc._is_breaking_change({"dependencies": ["a", "b"]}, {"dependencies": ["a", "c"]}) is True
    )


async def test_compare_metrics_similar():
    """字段值存在字符串包含关系 → 标记 similar。"""
    svc, repo = _svc_with_repo()
    a = make_metric(granularity="daily")
    b = make_metric(granularity="daily_hourly")
    repo.get_by_code = AsyncMock(side_effect=[a, b])
    result = await svc.compare_metrics("sales_gmv_daily", "sales_gmv_daily")
    assert result["metrics"] == ["sales_gmv_daily", "sales_gmv_daily"]
    assert result["fields"]["granularity"]["difference_level"] == "similar"


# ---- get_consumption_guide 分支 ----


async def test_get_consumption_guide_uses_existing():
    """已有 consumption_guide 直接返回（presence 判定，缺省补来源元数据）。"""
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get_guide = AsyncMock(return_value=None)
    svc._cache.set_guide = AsyncMock()
    existing_guide = {"metric_code": "sales_gmv_daily", "recommended_usage": ["自定义"]}
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="PUBLISHED", consumption_guide=existing_guide)
    )
    guide = await svc.get_consumption_guide("sales_gmv_daily")
    # presence 判定：DB 值优先，内容保留 + 补 guide_source（存量无 source 视为 manual）
    assert guide["recommended_usage"] == ["自定义"]
    assert guide["guide_source"] == "manual"
    assert "guide_updated_at" not in guide


async def test_get_consumption_guide_pii_caution():
    """PII 指标默认 guide 追加合规 caution。"""
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get_guide = AsyncMock(return_value=None)
    svc._cache.set_guide = AsyncMock()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED", pii_flag=True))
    guide = await svc.get_consumption_guide("sales_gmv_daily")
    assert any("PII" in c for c in guide["cautions"])


async def test_get_consumption_guide_semi_additive_caution():
    """SEMI_ADDITIVE 指标默认 guide 追加不可加维度 caution。"""
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get_guide = AsyncMock(return_value=None)
    svc._cache.set_guide = AsyncMock()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="PUBLISHED",
            additivity="SEMI_ADDITIVE",
            non_additive_dimensions=["store_id"],
        )
    )
    guide = await svc.get_consumption_guide("sales_gmv_daily")
    assert any("store_id" in c for c in guide["cautions"])


async def test_update_consumption_guide_sets_manual_source():
    """消费指南人工维护：manual 标记 + 更新人/时间 + row_version+1 + 清指南缓存。"""
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.invalidate_guide = AsyncMock()
    existing = make_metric(status="PUBLISHED", row_version=3, version=2)
    repo.get_by_code = AsyncMock(return_value=existing)
    updated = make_metric(status="PUBLISHED", row_version=4, version=2)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)

    result = await svc.update_consumption_guide(
        "sales_gmv_daily",
        MetricConsumptionGuideUpdateRequest(
            recommended_usage=["按日分析 GMV"],
            cautions=["含退款前金额"],
            related_metrics=["sales_uv_daily"],
            row_version=3,
        ),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )

    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["consumption_guide"]["recommended_usage"] == ["按日分析 GMV"]
    assert kwargs["consumption_guide"]["cautions"] == ["含退款前金额"]
    assert kwargs["guide_source"] == "manual"
    assert kwargs["guide_updated_by"] == 1
    assert kwargs["guide_updated_at"] is not None
    # 指南维护不触发版本号递增
    assert "version" not in kwargs
    svc._cache.invalidate_guide.assert_awaited_once_with("sales_gmv_daily")
    assert result["guide_source"] == "manual"


async def test_update_consumption_guide_optimistic_lock_conflict():
    """row_version 不一致 → 409 OPTIMISTIC_LOCK_CONFLICT，不写库。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="PUBLISHED", row_version=3)
    )
    with pytest.raises(ConflictError) as ei:
        await svc.update_consumption_guide(
            "sales_gmv_daily",
            MetricConsumptionGuideUpdateRequest(
                recommended_usage=["x"], row_version=2
            ),
            actor_id=1,
            role="metric_owner",
        )
    assert ei.value.error_code == "OPTIMISTIC_LOCK_CONFLICT"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_update_consumption_guide_blocked_by_pdp():
    """PDP 拒绝 write → FORBIDDEN，不写库。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    svc._governance_svc.check_metric_permission = AsyncMock(
        return_value=Decision(allow=False, reason="no write", error_code="FORBIDDEN")
    )
    with pytest.raises(BusinessError) as ei:
        await svc.update_consumption_guide(
            "sales_gmv_daily",
            MetricConsumptionGuideUpdateRequest(recommended_usage=["x"]),
            actor_id=9,
            role="viewer",
        )
    assert ei.value.error_code == "FORBIDDEN"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_update_consumption_guide_not_owner_raises_auth():
    """metric_owner 操作他人指标 → AuthError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(owner_id=1))
    with pytest.raises(AuthError):
        await svc.update_consumption_guide(
            "sales_gmv_daily",
            MetricConsumptionGuideUpdateRequest(recommended_usage=["x"]),
            actor_id=99,
            role="analyst",
            user_domain="sales",
        )
    repo.update_with_optimistic_lock.assert_not_called()


async def test_get_consumption_guide_auto_related_metrics():
    """自动生成分支：血缘一跳推荐（排除自身/非 metric 节点/去重/限量）。"""
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get_guide = AsyncMock(return_value=None)
    svc._cache.set_guide = AsyncMock()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="PUBLISHED", metric_code="sales_gmv_daily")
    )

    class _FakeEdge:
        def __init__(self, source: str, target: str) -> None:
            self.source_node = source
            self.target_node = target

    edges = [
        _FakeEdge("metric:sales_gmv_daily", "metric:sales_uv_daily"),
        _FakeEdge("table:dws_sales", "metric:sales_gmv_daily"),
        _FakeEdge("metric:sales_gmv_daily", "metric:sales_gmv_daily"),  # 自环排除
        _FakeEdge("metric:sales_uv_daily", "metric:sales_gmv_daily"),  # 去重
        _FakeEdge("metric:sales_gmv_daily", "metric:sales_arpu_daily"),
        _FakeEdge("metric:sales_gmv_daily", "metric:sales_gmv_weekly"),
        _FakeEdge("metric:sales_gmv_daily", "metric:sales_gmv_monthly"),
        _FakeEdge("metric:sales_gmv_daily", "metric:sales_gmv_quarterly"),
        _FakeEdge("metric:sales_gmv_daily", "metric:sales_gmv_yearly"),
        _FakeEdge("metric:sales_gmv_daily", "metric:sales_gmv_extra"),  # 超限被截
    ]
    with patch(
        "app.services.lineage.repository.LineageRepository"
    ) as mock_repo_cls:
        mock_repo_cls.return_value.edges_for_node = AsyncMock(return_value=edges)
        guide = await svc.get_consumption_guide("sales_gmv_daily")

    related = guide["related_metrics"]
    assert related == [
        "sales_uv_daily",
        "sales_arpu_daily",
        "sales_gmv_weekly",
        "sales_gmv_monthly",
        "sales_gmv_quarterly",
        "sales_gmv_yearly",
    ]
    assert len(related) == 6  # 限量
    assert guide["guide_source"] == "auto"
    svc._cache.set_guide.assert_awaited_once()


async def test_get_consumption_guide_auto_non_additive_caution():
    """自动生成分支：不可加指标的事实性注意进 cautions（非模板推荐用法）。"""
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get_guide = AsyncMock(return_value=None)
    svc._cache.set_guide = AsyncMock()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="PUBLISHED", additivity="NON_ADDITIVE")
    )

    guide = await svc.get_consumption_guide("sales_gmv_daily")
    assert guide["recommended_usage"] == []
    assert "不可加指标：不可跨维度聚合" in guide["cautions"]
    assert guide["guide_source"] == "auto"


async def test_create_metric_with_consumption_guide_sets_manual():
    """创建时携带消费指南 → guide_source=manual 落库。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())
    payload = make_create_payload(
        consumption_guide={
            "recommended_usage": ["创建时指南"],
            "cautions": [],
            "related_metrics": [],
        }
    )
    await svc.create_metric(MetricCreateRequest(**payload), owner_id=1)
    created_metric = repo.create.call_args.args[0]
    assert created_metric.consumption_guide["recommended_usage"] == ["创建时指南"]
    assert created_metric.guide_source == "manual"


async def test_create_metric_without_guide_uses_auto():
    """创建时不带消费指南 → guide_source=auto 落库。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())
    await svc.create_metric(MetricCreateRequest(**make_create_payload()), owner_id=1)
    created_metric = repo.create.call_args.args[0]
    assert created_metric.consumption_guide is None
    assert created_metric.guide_source == "auto"


# ---- create_metric 自动补全 / PII 传播 / 编码耗尽 ----


async def test_create_metric_auto_fill_sets_default_field(monkeypatch):
    """自动推断命中且当前值=默认值（metric_tier=T3）时覆盖为建议值（275 行 setattr）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    async def _get_defaults(domain):
        return {"metric_tier": "T3"}

    monkeypatch.setattr(svc, "_get_domain_defaults", _get_defaults)
    monkeypatch.setattr(
        "app.services.semantic.auto_fill.auto_fill",
        lambda **kw: {"defaults": {"metric_tier": "T2"}, "measure": "gmv"},
    )
    payload = make_create_payload(
        metric_code=None,
        source_table="ods_order",
        measure_column="amount",
        period="day",
        metric_tier="T3",  # 默认值 → 将被覆盖为 T2
    )
    result = await svc.create_metric(MetricCreateRequest(**payload), owner_id=1)
    assert result.status == "DRAFT"


async def test_create_metric_propagates_pii(monkeypatch):
    """definition 含 source_fields 时触发 PII 血缘传播（best-effort，不阻断创建）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())
    propagated = AsyncMock()
    monkeypatch.setattr(
        "app.services.governance.service.GovernanceService",
        lambda db: MagicMock(propagate_pii_to_metric=propagated),
    )
    payload = make_create_payload(
        definition_json={
            "expression": "SUM(order_amount)",
            "source_fields": [{"name": "order_amount", "pii": True}],
        }
    )
    result = await svc.create_metric(MetricCreateRequest(**payload), owner_id=1)
    assert result is created
    propagated.assert_awaited_once()


async def test_generate_metric_code_exhausted_raises_conflict(monkeypatch):
    """自动编码耗尽（generate_unique_code 抛 RuntimeError）→ ConflictError。"""
    svc, repo = _svc_with_repo()

    def _boom(base, exists):
        raise RuntimeError("exhausted")

    monkeypatch.setattr("app.core.codegen.generate_unique_code", _boom)
    with pytest.raises(ConflictError) as exc:
        await svc._generate_metric_code(
            MetricCreateRequest(**make_create_payload(metric_code=None))
        )
    assert exc.value.ctx["code"] == "CODE_EXHAUSTED"


async def test_redis_available_true(monkeypatch):
    """Redis 已初始化时 _redis_available 返回 True（49 行）。"""
    import app.services.semantic.service as svc_mod

    monkeypatch.setattr(svc_mod, "get_redis", lambda: MagicMock())
    assert svc_mod._redis_available() is True


async def test_update_metric_published_def_plus_top_level_breaking_merges_diff():
    """口径 + 顶层破坏性字段同时变更（走 definition_json 分支）→ top_diff 合并进 diff_json。

    覆盖 update_metric 的 ``if request.definition_json is not None`` 分支：
    - top_level_breaking 检测（BREAKING_TOP_LEVEL_FIELDS 变化）
    - top_diff 构建（556）与 merged_diff.update(top_diff)（585）
    - PUBLISHED + breaking → PENDING_CONFIRMATION + PendingVersionManager.create_pending
    """
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        row_version=1,
        version=1,
        granularity="daily",
        backup_owner_id=2,
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    fake_pvm = MagicMock()
    fake_pvm.create_pending = AsyncMock()

    with patch(
        "app.services.semantic.pending_version_manager.PendingVersionManager",
        return_value=fake_pvm,
    ):
        result = await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                definition_json={"expression": "SUM(new_col)"},
                granularity="hourly",
                change_reason="口径+粒度双重变更",
            ),
            actor_id=1,
            role="metric_owner",
        )

    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.status == "PENDING_CONFIRMATION"
    assert version_arg.change_type == "BREAKING"
    # top-level 破坏性字段 diff 已合并（556/585 覆盖）
    assert version_arg.diff_json["granularity"]["before"] == "daily"
    assert version_arg.diff_json["granularity"]["after"] == "hourly"
    # 定义 diff 已合并（_compute_diff 产物）
    assert "expression" in version_arg.diff_json
    assert fake_pvm.create_pending.call_args.args[2] == [1, 2]
    assert result.row_version == 2


async def test_auto_accept_timeout_partial_rejected_returns_none():
    """超时自动接受后有 REJECTED 残留（非全部 CONFIRMED/ACCEPTED）→ 返回 None。

    覆盖 auto_accept_timeout 的 ``return None`` 分支（1598）：PENDING 被置
    TIMEOUT_ACCEPTED 后重读，仍存在 REJECTED 记录时不转正。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_id = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.get_pending_confirmations = AsyncMock(
        side_effect=[
            [
                MagicMock(id=1, status="PENDING"),
                MagicMock(id=2, status="REJECTED"),
            ],
            [
                MagicMock(id=1, status="TIMEOUT_ACCEPTED"),
                MagicMock(id=2, status="REJECTED"),
            ],
        ]
    )
    repo.update_confirmation_status = AsyncMock()

    result = await svc.auto_accept_timeout(metric_id=1, version=1)
    assert result is None
    # PENDING 记录被置 TIMEOUT_ACCEPTED
    assert repo.update_confirmation_status.call_args.args[1] == "TIMEOUT_ACCEPTED"
    # 未触发转正
    repo.update_with_optimistic_lock.assert_not_called()


async def test_auto_accept_timeout_skips_deprecated_metric():
    """PENDING 确认期指标被废弃（DEPRECATED）后超时任务不得转正其版本。

    修复前：auto_accept_timeout 不检查指标状态——废弃指标 14 天后仍被转正
    PENDING 版本（口径悄然变更 + 通知"新口径已生效"，语义矛盾：废弃指标
    不应再发生口径变更）；get_by_id 只过滤软删（deleted_at），DEPRECATED
    状态此前未被识别。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_id = AsyncMock(return_value=make_metric(status="DEPRECATED"))
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, status="PENDING")]
    )
    repo.update_confirmation_status = AsyncMock()

    result = await svc.auto_accept_timeout(metric_id=1, version=1)

    assert result is None
    # 未触发确认记录终结、未触发转正——废弃指标版本保持不变
    repo.update_confirmation_status.assert_not_called()
    repo.update_with_optimistic_lock.assert_not_called()


# ---------------------------------------------------------------------------
# update_metric_description（TD §12.1 指标业务描述，不触发版本/不参与口径变更）
# ---------------------------------------------------------------------------


async def test_update_metric_description_sets_manual_source():
    svc, repo = _svc_with_repo()
    existing = make_metric(status="PUBLISHED", row_version=3, version=2)
    repo.get_by_code = AsyncMock(return_value=existing)
    updated = make_metric(status="PUBLISHED", row_version=4, version=2)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)

    result = await svc.update_metric_description(
        "sales_gmv_daily",
        MetricDescriptionUpdateRequest(description="  每日成交总额（含退款前）  "),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )

    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["description"] == "每日成交总额（含退款前）"  # 去除首尾空白
    assert kwargs["description_source"] == "manual"
    assert kwargs["description_updated_by"] == 1
    assert kwargs["description_updated_at"] is not None
    # 描述更新不触发版本号递增
    assert "version" not in kwargs
    assert repo.create_version.call_count == 0
    assert result is updated


async def test_update_metric_description_clears_on_blank():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(description="旧描述"))
    updated = make_metric(description=None, description_source=None)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)

    await svc.update_metric_description(
        "sales_gmv_daily",
        MetricDescriptionUpdateRequest(description="   "),
        actor_id=1,
        role="metric_owner",
    )

    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["description"] is None
    assert kwargs["description_source"] is None
    assert kwargs["description_updated_by"] == 1


async def test_update_metric_description_blocked_by_pdp():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    svc._governance_svc.check_metric_permission = AsyncMock(
        return_value=Decision(allow=False, reason="no write", error_code="FORBIDDEN")
    )

    with pytest.raises(BusinessError) as ei:
        await svc.update_metric_description(
            "sales_gmv_daily",
            MetricDescriptionUpdateRequest(description="越权描述"),
            actor_id=9,
            role="viewer",
        )
    assert ei.value.error_code == "FORBIDDEN"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_update_metric_description_not_owner_raises_auth():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(owner_id=1))

    with pytest.raises(AuthError):
        await svc.update_metric_description(
            "sales_gmv_daily",
            MetricDescriptionUpdateRequest(description="他人指标"),
            actor_id=99,
            role="analyst",
            user_domain="sales",
        )
    repo.update_with_optimistic_lock.assert_not_called()


# bind_metric_term（P2-11：指标↔术语绑定写路径）
# ---------------------------------------------------------------------------


async def test_bind_metric_term_success():
    """P2-11: 绑定术语 → 写 metric.term_id，不触发版本。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(status="PUBLISHED", row_version=3, version=2, term_id=None)
    repo.get_by_code = AsyncMock(return_value=existing)
    # 术语存在性校验通过：Term.id 查询返回 1
    svc._db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: 1)
    )
    updated = make_metric(status="PUBLISHED", row_version=4, version=2, term_id=55)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)

    result = await svc.bind_metric_term(
        "sales_gmv_daily", 55, actor_id=1, role="metric_owner", user_domain="sales"
    )

    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["term_id"] == 55
    assert "version" not in kwargs  # 绑定不触发版本
    assert result is updated


async def test_bind_metric_term_unbind():
    """P2-11: term_id=None 解绑，不校验术语存在性。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED", term_id=55))
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", term_id=None)
    )
    # 解绑不应发起术语查询
    svc._db.execute = AsyncMock(side_effect=AssertionError("不应查询术语"))

    await svc.bind_metric_term(
        "sales_gmv_daily", None, actor_id=1, role="metric_owner", user_domain="sales"
    )

    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["term_id"] is None


async def test_bind_metric_term_term_not_found():
    """P2-11: 术语不存在 → NotFoundError，不更新指标。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    svc._db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: None)
    )

    with pytest.raises(NotFoundError) as exc:
        await svc.bind_metric_term(
            "sales_gmv_daily", 999, actor_id=1, role="metric_owner", user_domain="sales"
        )
    assert exc.value.error_code == "NOT_FOUND"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_bind_metric_term_not_owner_raises_auth():
    """P2-11: 非 Owner 操作他人指标 → AuthError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(owner_id=1))

    with pytest.raises(AuthError):
        await svc.bind_metric_term(
            "sales_gmv_daily", 55, actor_id=99, role="analyst", user_domain="sales"
        )
    repo.update_with_optimistic_lock.assert_not_called()


# infer_metric_description（TD §12.1 LLM 推断指标业务描述，source=llm）
# ---------------------------------------------------------------------------


def _llm_client(content: str) -> MagicMock:
    """构造 enabled=True 的假 LLM 客户端（chat 返回指定 content）。"""
    client = MagicMock()
    client.enabled = True
    client.chat = AsyncMock(return_value={"content": content})
    client.close = AsyncMock()
    return client


async def test_infer_metric_description_llm_success_sets_llm_source():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED", row_version=3))
    updated = make_metric(description="每日成交总额（含退款前）", description_source="llm")
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    fake = _llm_client(
        '{"description": "每日成交总额（含退款前），按渠道汇总", "confidence": 0.92}'
    )
    with patch.object(svc, "_build_llm_client", AsyncMock(return_value=fake)):
        result = await svc.infer_metric_description(
            "sales_gmv_daily", actor_id=1, role="metric_owner", user_domain="sales"
        )

    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["description"] == "每日成交总额（含退款前），按渠道汇总"
    assert kwargs["description_source"] == "llm"
    assert kwargs["description_updated_by"] == 1
    # 推断不触发版本号递增
    assert "version" not in kwargs
    assert result is updated


async def test_infer_metric_description_llm_unavailable_raises_business():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    fake = MagicMock()
    fake.enabled = False
    with (
        patch.object(svc, "_build_llm_client", AsyncMock(return_value=fake)),
        pytest.raises(BusinessError) as ei,
    ):
        await svc.infer_metric_description("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert ei.value.error_code == "LLM_INFER_UNAVAILABLE"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_infer_metric_description_parse_fail_raises_business():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    # chat 返回非 JSON → 解析失败 → 视为 LLM 不可用
    fake = _llm_client("抱歉，我无法生成描述。")
    with (
        patch.object(svc, "_build_llm_client", AsyncMock(return_value=fake)),
        pytest.raises(BusinessError) as ei,
    ):
        await svc.infer_metric_description("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert ei.value.error_code == "LLM_INFER_UNAVAILABLE"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_infer_metric_description_blocked_by_pdp():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    svc._governance_svc.check_metric_permission = AsyncMock(
        return_value=Decision(allow=False, reason="no write", error_code="FORBIDDEN")
    )

    with pytest.raises(BusinessError) as ei:
        await svc.infer_metric_description(
            "sales_gmv_daily", actor_id=1, role="metric_owner", user_domain="sales"
        )
    assert ei.value.error_code == "FORBIDDEN"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_infer_metric_description_not_owner_raises_auth():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(owner_id=1))

    with pytest.raises(AuthError):
        await svc.infer_metric_description(
            "sales_gmv_daily", actor_id=99, role="analyst", user_domain="sales"
        )
    repo.update_with_optimistic_lock.assert_not_called()


async def test_infer_metric_description_skips_existing_llm_without_force():
    """已有 LLM 描述且未 force → 短路返回，不重复调 LLM（去重/省时核心防线）。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        description="每日成交总额（含退款前）",
        description_source="llm",
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    with patch.object(
        svc, "_llm_infer_metric_description", AsyncMock(return_value=None)
    ) as mock_infer:
        result = await svc.infer_metric_description(
            "sales_gmv_daily", actor_id=1, role="metric_owner", user_domain="sales"
        )

    assert result is existing
    mock_infer.assert_not_called()
    repo.update_with_optimistic_lock.assert_not_called()


async def test_infer_metric_description_force_regenerates_existing_llm():
    """force=True → 忽略已有 LLM 描述，重新调 LLM 生成并覆盖。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="PUBLISHED",
            description="旧描述",
            description_source="llm",
            row_version=5,
        )
    )
    updated = make_metric(description="新描述", description_source="llm")
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    fake = _llm_client('{"description": "新描述", "confidence": 0.9}')
    with patch.object(svc, "_build_llm_client", AsyncMock(return_value=fake)):
        result = await svc.infer_metric_description(
            "sales_gmv_daily", actor_id=1, role="metric_owner", user_domain="sales", force=True
        )

    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["description"] == "新描述"
    assert kwargs["description_source"] == "llm"
    assert result is updated


# ---- 废弃指标重新发起评审（DEPRECATED → REVIEW 闭环）----


async def test_deprecated_metric_can_resubmit_for_review():
    """废弃指标可重新提交评审（DEPRECATED → REVIEW），并清除废弃标记。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="DEPRECATED",
            owner_id=1,
            successor_code="sales_gmv_v2",
            deprecated_at="2026-08-01T00:00:00+00:00",
        )
    )
    updated = make_metric(status="REVIEW")
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)

    result = await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="废弃后重新评审"),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )

    assert result.status == "REVIEW"
    kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert kwargs["status"] == "REVIEW"
    assert kwargs["successor_code"] is None  # 重评审清除废弃标记
    assert kwargs["deprecated_at"] is None
    assert kwargs["sunset_until"] is None


async def test_published_metric_resubmit_still_blocked():
    """PUBLISHED 状态提交评审仍被拒（状态机约束不放松）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED", owner_id=1))
    with pytest.raises(ConflictError) as exc:
        await svc.submit_metric(
            "sales_gmv_daily",
            MetricSubmitRequest(change_reason="不应允许"),
            actor_id=1,
            role="metric_owner",
            user_domain="sales",
        )
    assert exc.value.error_code == "INVALID_TRANSITION"


# ---- 发起评审通知指定评审人/团队 ----


async def test_submit_notifies_specific_reviewer_when_assigned():
    """指定评审用户（reviewer_type=user）时，仅通知该评审人（非整个域）。

    提交成功另补「已提交评审」回执给提交人本人（生产就绪审查 P2：to_reviewers/
    指定评审人分支均排除提交人，此前提交人无回执）——第二次通知调用为回执。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", owner_id=1, domain="sales")
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="REVIEW"))

    await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(
            change_reason="提交审核，指定评审人", reviewer_type="user", reviewer_id=99
        ),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )

    calls = svc._notify_metric_stakeholders.call_args_list
    # 首次通知：指定评审人（metric.submitted），payload 携带 reviewer_id/type
    assert calls[0].args[0] == "metric.submitted"
    assert calls[0].kwargs["payload"]["reviewer_id"] == 99
    assert calls[0].kwargs["payload"]["reviewer_type"] == "user"
    # 回执：metric.submitted_ack 通知提交人（actor_id）
    assert calls[1].args[0] == "metric.submitted_ack"
    assert calls[1].kwargs["submitter_id"] == 1


async def test_deprecated_resubmit_publishes_resubmitted_event():
    """废弃指标重评审时发布 metric.resubmitted 事件（区别于首次提交）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DEPRECATED", owner_id=1))
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="REVIEW"))
    svc._publish_event = AsyncMock()

    await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="重新评审"),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )

    published = svc._publish_event.call_args.args[0]
    assert published == "metric.resubmitted"


# ---- 血缘接入：_register_metric_lineage_full（Task A）----


async def test_register_metric_lineage_full_derived_registers_dependency_edges():
    """derived 指标：注册表级血缘 + 每条 metric:{dep}→metric:{code} 依赖边（DERIVED_FROM/L3）。"""
    svc, _repo = _svc_with_repo()
    metric = make_metric(
        type="derived",
        definition_json={
            "expression": "SUM(a)/SUM(b)",
            "dependencies": ["fct_order", "dim_user"],
            "source_tables": ["dwd_order"],
        },
    )
    with (
        patch("app.services.lineage.service.LineageService") as mock_ls,
        patch("app.services.lineage.repository.LineageRepository") as mock_lr,
    ):
        lineage_svc = mock_ls.return_value
        lineage_svc.register_metric_from_definition = AsyncMock(return_value=[])
        lineage_svc.sync_metric_dimension_edges = AsyncMock(return_value=(0, 0))
        repo_inst = mock_lr.return_value
        repo_inst.upsert_edge = AsyncMock()

        await svc._register_metric_lineage_full(metric)

        # 表级血缘始终注册（commit=False 交由外层指标事务统一提交）
        lineage_svc.register_metric_from_definition.assert_awaited_once_with(metric, commit=False)
        # 两个依赖各一条 DERIVED_FROM / L3 边（composite/derived 统一 DERIVED_FROM）
        assert repo_inst.upsert_edge.await_count == 2
        calls = {c.kwargs["source_node"]: c.kwargs for c in repo_inst.upsert_edge.call_args_list}
        assert calls["metric:fct_order"]["target_node"] == "metric:sales_gmv_daily"
        assert calls["metric:fct_order"]["edge_type"] == "DERIVED_FROM"
        assert calls["metric:fct_order"]["granularity"] == "L3"
        assert calls["metric:dim_user"]["target_node"] == "metric:sales_gmv_daily"


async def test_register_metric_lineage_full_registers_base_atomic_edge():
    """derived 指标含 base_atomic：注册「原子→派生」的 BASED_ON 基础边（区别于依赖边）。

    OneData：派生 = 基础原子 + 业务限定 + 时间周期。base_atomic 与 dependencies 分离，
    各自生成不同边类型——BASED_ON 标识派生的原子基底，DERIVED_FROM 标识普通上游引用。
    """
    svc, _repo = _svc_with_repo()
    metric = make_metric(
        type="derived",
        definition_json={
            "expression": "SUM(x)",
            "base_atomic": "active_doctor_daily",
            "source_tables": ["dwd_doctor"],
        },
    )
    with (
        patch("app.services.lineage.service.LineageService") as mock_ls,
        patch("app.services.lineage.repository.LineageRepository") as mock_lr,
    ):
        lineage_svc = mock_ls.return_value
        lineage_svc.register_metric_from_definition = AsyncMock(return_value=[])
        lineage_svc.sync_metric_dimension_edges = AsyncMock(return_value=(0, 0))
        repo_inst = mock_lr.return_value
        repo_inst.upsert_edge = AsyncMock()

        await svc._register_metric_lineage_full(metric)

        # 无 dependencies → 仅 1 条边：BASED_ON 基础边
        assert repo_inst.upsert_edge.await_count == 1
        call = repo_inst.upsert_edge.call_args.kwargs
        assert call["source_node"] == "metric:active_doctor_daily"
        assert call["target_node"] == "metric:sales_gmv_daily"
        assert call["edge_type"] == "BASED_ON"
        assert call["granularity"] == "L3"
        assert call["change_reason"] == "metric_base_atomic"


async def test_register_metric_lineage_full_base_atomic_plus_dependencies():
    """derived 同时含 base_atomic + dependencies：BASED_ON 与 DERIVED_FROM 边并存。"""
    svc, _repo = _svc_with_repo()
    metric = make_metric(
        type="derived",
        definition_json={
            "expression": "SUM(a)/SUM(b)",
            "base_atomic": "active_doctor_daily",
            "dependencies": ["last_month_doctor_daily"],
            "source_tables": ["dwd_doctor"],
        },
    )
    with (
        patch("app.services.lineage.service.LineageService") as mock_ls,
        patch("app.services.lineage.repository.LineageRepository") as mock_lr,
    ):
        lineage_svc = mock_ls.return_value
        lineage_svc.register_metric_from_definition = AsyncMock(return_value=[])
        lineage_svc.sync_metric_dimension_edges = AsyncMock(return_value=(0, 0))
        repo_inst = mock_lr.return_value
        repo_inst.upsert_edge = AsyncMock()

        await svc._register_metric_lineage_full(metric)

        assert repo_inst.upsert_edge.await_count == 2
        by_src = {
            c.kwargs["source_node"]: c.kwargs["edge_type"]
            for c in repo_inst.upsert_edge.call_args_list
        }
        assert by_src["metric:active_doctor_daily"] == "BASED_ON"
        assert by_src["metric:last_month_doctor_daily"] == "DERIVED_FROM"


async def test_register_metric_lineage_full_atomic_skips_dependency_edges():
    """atomic 指标即使 definition 含 dependencies 也跳过指标间依赖边（仅注册表级血缘）。"""
    svc, _repo = _svc_with_repo()
    metric = make_metric(
        type="atomic",
        definition_json={
            "expression": "SUM(x)",
            "dependencies": ["fct_order"],
            "source_tables": ["dwd_x"],
        },
    )
    with (
        patch("app.services.lineage.service.LineageService") as mock_ls,
        patch("app.services.lineage.repository.LineageRepository") as mock_lr,
    ):
        lineage_svc = mock_ls.return_value
        lineage_svc.register_metric_from_definition = AsyncMock(return_value=[])
        lineage_svc.sync_metric_dimension_edges = AsyncMock(return_value=(0, 0))
        repo_inst = mock_lr.return_value
        repo_inst.upsert_edge = AsyncMock()

        await svc._register_metric_lineage_full(metric)

        lineage_svc.register_metric_from_definition.assert_awaited_once()
        repo_inst.upsert_edge.assert_not_awaited()  # atomic 无指标间依赖边


async def test_register_metric_lineage_full_registers_dimension_edges():
    """atomic 指标含 dimensions：维度边差异同步由 register_metric_from_definition 内完成。"""
    svc, _repo = _svc_with_repo()
    metric = make_metric(
        type="atomic",
        definition_json={
            "expression": "SUM(gmv)",
            "dimensions": ["dim_store", "dim_channel"],
            "source_tables": ["dwd_order"],
        },
    )
    with patch("app.services.lineage.service.LineageService") as mock_ls:
        lineage_svc = mock_ls.return_value
        lineage_svc.register_metric_from_definition = AsyncMock(return_value=[])
        lineage_svc.sync_metric_dimension_edges = AsyncMock(return_value=(0, 0))

        await svc._register_metric_lineage_full(metric)

        # 表/维度/字段血缘统一由 register_metric_from_definition 差异同步
        lineage_svc.register_metric_from_definition.assert_awaited_once_with(metric, commit=False)
        # atomic 无依赖边，不调用依赖注册（register_metric_from_definition 在依赖边前执行）
        lineage_svc.sync_metric_dimension_edges.assert_not_awaited()


async def test_register_metric_lineage_full_clears_dimension_edges_when_absent():
    """atomic 无维度：仍调用 register_metric_from_definition（其内部差异同步清残留）。"""
    svc, _repo = _svc_with_repo()
    metric = make_metric(
        type="atomic",
        definition_json={"expression": "SUM(gmv)", "source_tables": ["dwd_order"]},
    )
    with patch("app.services.lineage.service.LineageService") as mock_ls:
        lineage_svc = mock_ls.return_value
        lineage_svc.register_metric_from_definition = AsyncMock(return_value=[])
        lineage_svc.sync_metric_dimension_edges = AsyncMock(return_value=(0, 0))

        await svc._register_metric_lineage_full(metric)

        lineage_svc.register_metric_from_definition.assert_awaited_once_with(metric, commit=False)
        lineage_svc.sync_metric_dimension_edges.assert_not_awaited()


async def test_register_metric_lineage_full_failure_is_swallowed():
    """血缘注册抛错不向上传播（best-effort，绝不阻断指标创建/发布/更新主流程）。"""
    svc, _repo = _svc_with_repo()
    metric = make_metric(type="composite", definition_json={"dependencies": ["fct_order"]})
    with patch("app.services.lineage.service.LineageService") as mock_ls:
        mock_ls.return_value.register_metric_from_definition = AsyncMock(
            side_effect=RuntimeError("lineage store down")
        )
        # 不应抛异常
        await svc._register_metric_lineage_full(metric)


# ---- 血缘变更影响通知：notify_lineage_impacted_owners（Task C）----


async def test_notify_lineage_impacted_owners_notifies_owners():
    """下游存在受影响指标 → 按 owner 定向通知，event_type=lineage.change_impacted / title=血缘变更影响。"""# noqa: E501
    svc, repo = _svc_with_repo()
    edges = [
        MagicMock(target_node="metric:gmv_derived"),
        MagicMock(target_node="metric:gmv_composite"),
        MagicMock(target_node="table:db.orders"),  # 非指标节点，应忽略
    ]

    async def _lookup(code: str) -> Metric:
        return make_metric(metric_code=code, owner_id=7 if code == "gmv_derived" else 9)

    with (
        patch("app.db.mysql.async_session_factory") as mock_factory,
        patch("app.services.lineage.service.LineageService") as mock_ls,
        patch("app.services.notify.service.NotifyService") as mock_ns,
    ):
        mock_factory.return_value = AsyncMock()  # 独立会话异步上下文管理器
        lineage_svc = mock_ls.return_value
        lineage_svc.query_impact = AsyncMock(return_value=edges)
        repo.get_by_code = _lookup  # 按 code 返回对应 owner 的指标
        notify_inst = mock_ns.return_value
        notify_inst.notify_user = AsyncMock(return_value=None)

        count = await svc.notify_lineage_impacted_owners("table:db.orders")

    assert count == 2
    assert notify_inst.notify_user.await_count == 2
    event_types = {c.kwargs["event_type"] for c in notify_inst.notify_user.call_args_list}
    assert event_types == {"lineage.change_impacted"}
    titles = {c.kwargs["title"] for c in notify_inst.notify_user.call_args_list}
    assert titles == {"血缘变更影响"}


async def test_notify_lineage_impacted_owners_query_failure_returns_zero():
    """影响分析查询抛错 → 返回 0，且不发任何通知（best-effort，不阻断上游变更）。"""
    svc, _repo = _svc_with_repo()
    with (
        patch("app.services.lineage.service.LineageService") as mock_ls,
        patch("app.services.notify.service.NotifyService") as mock_ns,
    ):
        mock_ls.return_value.query_impact = AsyncMock(side_effect=RuntimeError("graph down"))
        notify_inst = mock_ns.return_value
        notify_inst.notify_user = AsyncMock(return_value=None)

        count = await svc.notify_lineage_impacted_owners("table:db.orders")

    assert count == 0
    notify_inst.notify_user.assert_not_awaited()


# ============================================================
# DATA_SOURCE_DROPPED 状态闭环（TD §12.3 / PRD R5-01）
#   mark_source_dropped     : 数据源 DROP → 下游 PUBLISHED 指标置 DSD
#   recover_source_dropped  : DSD → PUBLISHED（源恢复/误报）
#   confirm_deprecate_dropped : DSD → DEPRECATED（确认退役，须填替代指标）
# ============================================================


async def test_mark_source_dropped_marks_published_metrics():
    """数据源 DROP → 血缘下游 PUBLISHED 指标批量置 DATA_SOURCE_DROPPED。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="gmv", status="PUBLISHED", row_version=1)
    m2 = make_metric(metric_code="aov", status="PUBLISHED", row_version=1)
    # 1) 数据源 → 该源下 2 张表（DBCatalog 查询）
    result = MagicMock()
    result.scalars.return_value.all.return_value = ["dwd_orders", "dwd_pay"]
    svc._db.execute = AsyncMock(return_value=result)
    # 2) 血缘下游：每张表返回对应 metric 节点
    with patch("app.services.lineage.service.LineageService") as mock_ls:
        mock_ls.return_value.query_impact = AsyncMock(
            side_effect=[
                [MagicMock(target_node="metric:gmv")],
                [MagicMock(target_node="metric:aov")],
            ]
        )
        repo.get_by_code = AsyncMock(side_effect=[m1, m2])
        repo.update_with_optimistic_lock = AsyncMock(return_value=m1)
        svc._publish_event = AsyncMock(return_value=None)
        svc._cache.invalidate = AsyncMock(return_value=None)

        count = await svc.mark_source_dropped(["mysql_orders"], actor_id=1, role="platform_admin")

    assert count == 2
    assert repo.update_with_optimistic_lock.await_count == 2
    # 目标状态是 DATA_SOURCE_DROPPED
    calls = repo.update_with_optimistic_lock.call_args_list
    for c in calls:
        assert c.kwargs["status"] == "DATA_SOURCE_DROPPED"


async def test_mark_source_dropped_rejects_non_admin():
    """越权防护：非管理角色调 mark_source_dropped → AuthError，不得批量变更他人指标状态。"""
    from app.core.exceptions import AuthError

    svc, repo = _svc_with_repo()
    # 不 mock 任何血缘/DB 查询：角色校验须在副作用发生前拦截
    with pytest.raises(AuthError) as exc:
        await svc.mark_source_dropped(["mysql_orders"], actor_id=1, role="metric_owner")
    assert exc.value.error_code == "FORBIDDEN"
    # 未触碰任何指标状态更新
    repo.update_with_optimistic_lock.assert_not_called()


async def test_recover_source_dropped_returns_to_published():
    """DSD → PUBLISHED（源恢复/确认误报），状态机合法。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(metric_code="gmv", status="DATA_SOURCE_DROPPED", row_version=1)
    updated = make_metric(metric_code="gmv", status="PUBLISHED", row_version=2)
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    svc._cache.invalidate = AsyncMock(return_value=None)
    svc._publish_event = AsyncMock(return_value=None)

    result = await svc.recover_source_dropped("gmv", actor_id=1, role="platform_admin")

    assert result.status == "PUBLISHED"
    assert repo.update_with_optimistic_lock.await_count == 1
    assert repo.update_with_optimistic_lock.call_args.kwargs["status"] == "PUBLISHED"


async def test_recover_source_dropped_rejects_non_dsd():
    """非 DSD 状态调 recover → 409 非法跃迁。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(metric_code="gmv", status="PUBLISHED", row_version=1)
    repo.get_by_code = AsyncMock(return_value=metric)

    with pytest.raises(ConflictError):
        await svc.recover_source_dropped("gmv", actor_id=1, role="platform_admin")


async def test_confirm_deprecate_dropped_invalid_successor_raises():
    """DSD → DEPRECATED 填了不存在的替代指标 → 404。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(metric_code="gmv", status="DATA_SOURCE_DROPPED", row_version=1)
    repo.get_by_code = AsyncMock(side_effect=[metric, None])

    with pytest.raises(NotFoundError):
        await svc.confirm_deprecate_dropped(
            "gmv", successor_code="no_such_metric", actor_id=1, role="platform_admin"
        )


async def test_confirm_deprecate_dropped_allows_no_successor():
    """DSD → DEPRECATED 无替代指标也可退役（可选替代）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(metric_code="gmv", status="DATA_SOURCE_DROPPED", row_version=1)
    dep = make_metric(metric_code="gmv", status="DEPRECATED", row_version=2)
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.update_with_optimistic_lock = AsyncMock(return_value=dep)
    svc._publish_event = AsyncMock(return_value=None)
    svc._cleanup_metric_lineage = AsyncMock(return_value=None)
    svc._cache.invalidate = AsyncMock(return_value=None)

    result = await svc.confirm_deprecate_dropped(
        "gmv", successor_code=None, actor_id=1, role="platform_admin"
    )

    assert result.status == "DEPRECATED"
    assert repo.update_with_optimistic_lock.call_args.kwargs["successor_code"] is None


async def test_confirm_deprecate_dropped_success():
    """DSD → DEPRECATED（确认退役），带替代指标 + 事件。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(metric_code="gmv", status="DATA_SOURCE_DROPPED", row_version=1)
    successor = make_metric(metric_code="gmv_v2", status="PUBLISHED", row_version=1)
    dep = make_metric(metric_code="gmv", status="DEPRECATED", row_version=2)
    repo.get_by_code = AsyncMock(side_effect=[metric, successor])
    repo.update_with_optimistic_lock = AsyncMock(return_value=dep)
    svc._publish_event = AsyncMock(return_value=None)
    svc._cleanup_metric_lineage = AsyncMock(return_value=None)
    svc._cache.invalidate = AsyncMock(return_value=None)

    result = await svc.confirm_deprecate_dropped(
        "gmv", successor_code="gmv_v2", actor_id=1, role="platform_admin"
    )

    assert result.status == "DEPRECATED"
    svc._publish_event.assert_awaited_once()
    assert svc._publish_event.call_args.args[0] == "metric.deprecated"


async def test_update_metric_row_version_conflict_raises_409():
    """跨请求乐观锁：前端回传 row_version 与当前不一致 → ConflictError（防静默覆盖）。"""
    from app.core.exceptions import ConflictError

    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=5, version=1)
    repo.get_by_code = AsyncMock(return_value=existing)

    with pytest.raises(ConflictError) as exc:
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                name="新名称",
                change_reason="调整元数据",
                row_version=3,  # 客户端持有的旧版本号
            ),
            actor_id=1,
            role="metric_owner",
        )
    assert exc.value.error_code == "OPTIMISTIC_LOCK_CONFLICT"
    assert exc.value.ctx["current_row_version"] == 5
    # 冲突时不应触达落库
    repo.update_with_optimistic_lock.assert_not_called()


async def test_update_metric_row_version_match_passes():
    """跨请求乐观锁：row_version 一致时正常更新。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=5, version=1)
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=6, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            name="新用户数",
            change_reason="调整元数据",
            row_version=5,  # 与当前一致
        ),
        actor_id=1,
        role="metric_owner",
    )
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["name"] == "新用户数"


async def test_update_metric_description_row_version_conflict_raises_409():
    """描述编辑跨请求乐观锁：row_version 不一致 → ConflictError（防静默覆盖）。"""
    from app.core.exceptions import ConflictError

    svc, repo = _svc_with_repo()
    existing = make_metric(status="PUBLISHED", row_version=4, version=2)
    repo.get_by_code = AsyncMock(return_value=existing)

    with pytest.raises(ConflictError) as exc:
        await svc.update_metric_description(
            "sales_gmv_daily",
            MetricDescriptionUpdateRequest(description="新描述", row_version=2),
            actor_id=1,
            role="metric_owner",
        )
    assert exc.value.error_code == "OPTIMISTIC_LOCK_CONFLICT"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_update_metric_review_edit_resets_to_draft():
    """REVIEW 状态编辑即撤回重提（FR-005 闭环）：重置 DRAFT 并清空评审指派。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="REVIEW", row_version=1, version=1, reviewer_id=3, reviewer_type="user"
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="DRAFT"))

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(name="销售 GMV 改", change_reason="修正名称"),
        actor_id=1,
        role="metric_owner",
    )

    call_args = repo.update_with_optimistic_lock.call_args
    updates = call_args.kwargs
    assert updates["status"] == "DRAFT"
    assert updates["reviewer_id"] is None
    assert updates["reviewer_type"] is None
    assert updates["reviewer_domain"] is None


async def test_sql_batch_register_doris_ctas_candidates_end_to_end():
    """Doris CTAS 解析候选 → batch_register_from_sql 端到端创建 DRAFT。

    回归链路：默认方言不支持 DUPLICATE KEY/DISTRIBUTED BY/PROPERTIES 曾致解析
    失败 → 0 候选（无法批量注册）；现在 parse-sql-batch 剥离物理属性后产出候选，
    候选 definition_json（expression + source_fields）被 batch-register-from-sql
    消费并逐条 savepoint 创建——「解析 → 注册」整条链路对 Doris 全通。
    """
    from app.services.semantic.schemas import MetricSqlBatchRegisterRequest
    from app.services.semantic.sql_split import infer_sql_batch

    doris_ctas = """
DROP TABLE IF EXISTS wedw_dws.doctor_func_index_df;
CREATE TABLE IF NOT EXISTS wedw_dws.doctor_func_index_df
DUPLICATE KEY(create_date, doctor_code, hosp_code)
DISTRIBUTED BY HASH(create_date, doctor_code, hosp_code) BUCKETS 5
PROPERTIES ("replication_allocation" = "tag.location.default: 1")
AS SELECT
  coalesce(a.event_date, b.create_date) AS create_date,
  coalesce(a.quality_control_qc_report_cnt, 0) AS quality_control_qc_report_cnt,
  coalesce(b.order_cnt, 0) AS yyf_order_cnt
FROM (
  SELECT t1.user_id, to_date(t1.event_time) AS event_date,
    sum(case when get_json_string(t1.biz_data,'$.skillId')='quality-control-qc-report'
        then 1 else 0 end) AS quality_control_qc_report_cnt
  FROM ods_track_event t1 GROUP BY t1.user_id, to_date(t1.event_time)
) a
FULL JOIN (
  SELECT doctor_code, create_date, count(distinct prescription_no) AS order_cnt
  FROM doctor_yyf_his_order_detail_df GROUP BY doctor_code, create_date
) b ON a.user_id = b.doctor_code AND a.event_date = b.create_date
"""
    parsed = await infer_sql_batch(
        MagicMock(), sql=doris_ctas, split_mode="statement", domain_code="wedw"
    )
    bases = [c for c in parsed["candidates"] if c["type"] == "derived"]
    assert len(bases) >= 2, "Doris CTAS 应解析出 ≥2 个派生候选"

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    request = MetricSqlBatchRegisterRequest(
        domain="wedw",
        candidates=[
            SqlBatchCreateCandidate(
                key=c["key"],
                metric_code=c["metric_code"],
                name=c["name"],
                type="derived",
                source_table=c["source_table"],
                measure_column=c["measure_column"],
                aggregation=c["aggregation"],
                period=c["period"],
                definition_json=c["definition_json"],
            )
            for c in bases
        ],
    )
    result = await svc.batch_register_from_sql(request, actor_id=1)
    assert len(result["candidates"]) == len(bases)
    assert all(c["status"] == "DRAFT" for c in result["candidates"])
    # 候选携带 Doris 口径（expression + source_fields）创建成功
    assert repo.create.call_count == len(bases)


async def test_sql_batch_register_derived_phase2_with_mount():
    """批量注册含派生候选：Phase1 基础候选（方案 A：SQL 一律派生）→ Phase2 派生
    （依赖预检 + type=derived + mount）。

    派生候选（用户把基础候选在线改为派生：依赖指标 + 计算表达式）走 Phase2 依赖预检，
    依赖在 Phase1 创建的基础候选命中 dep_ok → savepoint 创建 type=derived 指标并透传
    挂载实体（OneData 挂载层，源表/列/粒度/周期/域自动落 metric_mount）。
    """
    from app.services.semantic.schemas import (
        MetricMountInput,
        MetricSqlBatchRegisterRequest,
        SqlBatchCreateCandidate,
    )

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    base = SqlBatchCreateCandidate(
        key="0:active",
        metric_code="outpatient_doctor_active_month",
        name="医生活跃数",
        type="derived",
        source_table="wedw_dws.doctor_active_month_di",
        measure_column="doctor_code",
        aggregation="COUNT_DISTINCT",
        period="month",
        unit="PERSON",
        definition_json={
            "expression": "COUNT(DISTINCT doctor_code)",
            "source_fields": [
                {"table": "wedw_dws.doctor_active_month_di", "column": "doctor_code"}
            ],
        },
    )
    derived = SqlBatchCreateCandidate(
        key="0:retention",
        metric_code="outpatient_doctor_retention_month",
        name="医生留存率",
        type="derived",
        source_table="wedw_dws.doctor_active_month_di",
        measure_column=None,
        aggregation=None,
        period="month",
        unit=None,
        definition_json={
            "expression": "outpatient_doctor_active_month / outpatient_doctor_last_active_month",
            "dependencies": ["outpatient_doctor_active_month"],
        },
        dependencies=["outpatient_doctor_active_month"],
        mount=MetricMountInput(
            source_table="wedw_dws.doctor_active_month_di",
            source_column="doctor_code",
            granularity="month",
            default_period="month",
            domain="outpatient",
        ),
    )
    request = MetricSqlBatchRegisterRequest(
        domain="outpatient",
        candidates=[base, derived],
    )
    # 派生候选带 mount → create_metric 落 metric_mount；mock 环境下其 save 需 AsyncMock
    with patch(
        "app.services.metric_mount.repository.MetricMountRepository.save",
        new=AsyncMock(return_value=MagicMock()),
    ):
        result = await svc.batch_register_from_sql(request, actor_id=1)
    # Phase1 基础 + Phase2 派生均创建成功（派生依赖命中 dep_ok，无缺依赖跳过）
    assert [c["status"] for c in result["candidates"]] == ["DRAFT", "DRAFT"]
    assert repo.create.call_count == 2
