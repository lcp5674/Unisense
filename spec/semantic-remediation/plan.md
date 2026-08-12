# Implementation Plan: 语义建模模块工业级整改

**Input**: Feature specification from `spec/semantic-remediation/spec.md`

## Summary

将语义建模模块从L2级(57.0/100)提升至工业级标准(≥90/100)。核心改造：实现完整6态状态机(DRAFT/REVIEW/PUBLISHED/EXPERIMENTAL/DEPRECATED/DATA_SOURCE_DROPPED)+8种合法跃迁(submit/approve/reject/promote/rollback/deprecate/confirm_version/reject_version)；PENDING_VERSION破坏性变更缓冲机制(14天消费方确认期+超时默认接受+延期+Schema Drift暂停)；依赖指标递归校验+DAG环检测；冲突预检+命名规范；事件驱动双写(Neo4j/ES/notify)；灰度发布+回滚；紧急发布快通道；健康度五维评分；指标对比+批量注册；缓存韧性修复+数据安全加固+架构分层重构。全面对齐TD §12.3，消除38项缺陷，TD合规率从28.6%提升至100%。

## Technical Context

**Language/Version**: Python 3.11
**Primary Dependencies**: FastAPI, SQLAlchemy 2.0(async), Pydantic v2, Redis 7(async), structlog
**Storage**: MySQL 8(metric/metric_version/pending_version_confirmation/metric_health_score/batch_registration), Redis 7(缓存+分布式锁+EventBus), Neo4j 5(血缘图), ES 8.15(全文索引)
**Testing**: pytest(asyncio), 单元/集成/混沌测试
**Target Platform**: Linux server (Docker/K8s)
**Project Type**: web-service (REST API + 事件驱动)
**Performance Goals**: 指标CRUD p95<200ms, dashboard聚合<500ms, 缓存命中率>80%
**Constraints**: 版本缓存失效延迟<1s, PENDING_VERSION超时精度秒级, 熔断5次失败后30s半开
**Scale/Scope**: 万级指标, 千级并发读写

## Project Structure

### Documentation (this feature)

```text
spec/semantic-remediation/
├── spec.md              # 需求规格
├── plan.md              # 本文件
└── tasks.md             # 任务分解(Phase 3)
```

### Source Code (repository root)

```text
backend/app/
├── models/
│   ├── metric.py                    # [MOD] Metric模型+MetricState枚举+合法跃迁矩阵
│   ├── metric_version.py            # [NEW] MetricVersion独立文件(从metric.py拆出)+PendingVersionConfirmation模型
│   ├── metric_health.py             # [NEW] MetricHealthScore+HealthDimension枚举
│   ├── metric_template.py           # [MOD] Schema校验对齐
│   └── enums.py                     # [MOD] 新增MetricStateEnum+VersionStatusEnum
├── services/semantic/
│   ├── service.py                   # [MOD] 完整状态机+PENDING_VERSION+依赖校验+事件发布+灰度+紧急发布+健康度+对比+批量注册
│   ├── repository.py                # [MOD] dashboard聚合+LIKE转义+IntegrityError捕获+assert消除+version原子
│   ├── schemas.py                   # [MOD] metric_code严格校验+保留词+新增SubmitRequest/ApproveRequest等
│   ├── cache.py                     # [MOD] 键含版本号+record_success+pipeline预热
│   ├── state_machine.py             # [NEW] 状态机跃迁矩阵+校验逻辑
│   ├── dependency_checker.py        # [NEW] 依赖递归校验+DAG环检测
│   ├── health_scorer.py             # [NEW] 五维健康度评分引擎
│   ├── pending_version_manager.py   # [NEW] PENDING_VERSION生命周期管理(超时/延期/确认/拒绝/Drift暂停)
│   └── conflict_precheck.py         # [NEW] 冲突预检(异步调conflict服务)+命名规范校验
├── api/
│   ├── metrics.py                   # [MOD] 补齐submit/approve/reject/promote/rollback/confirm_version/reject_version/compare/batch-register端点
│   └── semantic.py                  # [MOD] dashboard迁移到repository+模板Schema校验+消费指南迁移到service
├── tasks/
│   └── semantic_tasks.py            # [NEW] Arq定时任务: PENDING_VERSION超时检查+健康度每日刷新+紧急发布补审提醒+灰度超期提醒
├── db/
│   └── mysql.py                     # (已有，不修改)
└── core/
    ├── eventbus.py                  # (已有，复用)
    └── base_service.py              # (已有，复用)

backend/tests/
├── unit/
│   ├── test_state_machine.py        # [NEW] 状态机跃迁测试
│   ├── test_dependency_checker.py   # [NEW] 依赖校验+环检测测试
│   ├── test_health_scorer.py        # [NEW] 健康度评分测试
│   ├── test_pending_version.py      # [NEW] PENDING_VERSION生命周期测试
│   ├── test_conflict_precheck.py    # [NEW] 冲突预检+命名校验测试
│   └── test_semantic_cache.py       # [NEW] 缓存韧性测试(熔断复位+版本键+pipeline)
├── integration/
│   └── test_semantic_integration.py # [NEW] 端到端集成测试
└── chaos/
    └── test_semantic_chaos.py       # [NEW] 并发发布/版本竞争/Redis宕机测试
```

**Structure Decision**: 遵循现有项目架构(service/repository/schemas分层+api路由)，在semantic服务内新增5个专项模块(state_machine/dependency_checker/health_scorer/pending_version_manager/conflict_precheck)，将MetricVersion拆为独立模型文件，新增3个模型(metric_version独立/pending_version_confirmation/metric_health_score)。

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|--------------------------------------|
| 状态机独立模块 | TD §12.3要求6态+8跃迁+合法校验，逻辑复杂度高 | 内联在service中会导致service.py超800行且无法独立测试 |
| PENDING_VERSION独立管理器 | 生命周期含超时/延期/确认/拒绝/Drift暂停5+状态，需定时任务 | 简单布尔标记无法表达完整生命周期 |
| 健康度独立评分引擎 | 五维加权+分级+缺失维度处理+每日刷新 | 内联逻辑会使service膨胀且无法独立测试各维度 |

## Research & Decisions

### Decision 1: 状态机实现方式
- **Decision**: 独立state_machine.py模块，定义MetricStateMachine类含合法跃迁矩阵+校验方法
- **Rationale**: TD要求6态+8跃迁，跃迁矩阵是核心业务规则，需独立可测试。跃迁校验在任何状态变更前执行，若校验失败直接409。
- **Alternatives considered**: (1)Pydantic枚举+validator——无法表达跃迁方向性；(2)数据库约束——状态校验是业务逻辑不应在存储层

### Decision 2: PENDING_VERSION存储模型
- **Decision**: 新增pending_version_confirmation表+MetricVersion.status增加PENDING_CONFIRMATION/CANCELLED/EXPERIMENTAL状态
- **Rationale**: 需记录每个消费方的确认/拒绝/超时状态，PENDING_VERSION的版本状态需与普通DRAFT/PUBLISHED区分。confirmation表记录消费方确认记录。
- **Alternatives considered**: (1)在metric表加pending_version字段——无法记录消费方确认记录；(2)纯Redis存储——查询不便+无持久化

### Decision 3: 依赖校验环检测算法
- **Decision**: DFS三色标记法(white/gray/black)检测有向图环
- **Rationale**: TD要求DERIVED_FROM有向图环检测，DFS三色标记法O(V+E)复杂度，可同时输出环路径。依赖图存储在definition_json.dependencies字段中，运行时从DB加载构建邻接表。
- **Alternatives considered**: (1)拓扑排序——无法输出环路径；(2)Neo4j Cypher环查询——依赖外部服务+延迟高

### Decision 4: 健康度评分数据来源
- **Decision**: 五维数据分别从(1)Metric一等字段完整度(2)consume.queried事件经observability聚合(3)quality_event经quality服务聚合(4)audit_log审核时效(5)lineage覆盖率聚合
- **Rationale**: TD §12.3明确规定五维数据源，需跨服务聚合。每日Arq定时任务批量计算，关键事件实时增量重算。
- **Alternatives considered**: (1)实时计算——跨5个服务聚合延迟不可接受；(2)仅用本地数据——无法覆盖活跃度/质量/血缘三维度

### Decision 5: 缓存键版本化策略
- **Decision**: 缓存键格式`metric:def:{code}:v{version}`，版本变更时旧键自然过期(TTL=600s)，无需主动invalidate
- **Rationale**: 版本号是单调递增整数，每次口径变更递增→新键自动写入→旧键10分钟后过期。消除invalidate失败导致的一致性问题。
- **Alternatives considered**: (1)维持现有键+invalidate——invalidate失败时缓存不一致；(2)版本号+时间戳双重键——过度复杂

### Decision 6: PENDING_VERSION超时实现
- **Decision**: Arq cron定时任务每分钟检查pending_version_confirmation表，超时(14天+延期)未确认→自动接受
- **Rationale**: 需精确到秒级超时检查，Arq cron可靠调度，检查逻辑简单(查created_at+extension_days)。超时接受后触发CURRENT切换+事件发布。
- **Alternatives considered**: (1)Celery延迟任务——项目未引入Celery；(2)Redis过期键通知——不可靠且无业务语义

### Decision 7: 事件发布与双写一致性
- **Decision**: 事件发布在数据库事务commit后执行(best-effort)，失败入Arq重试队列。读侧对Neo4j/ES缺失标stale。
- **Rationale**: 事件发布与DB事务无法做成强一致(跨系统)，采用最终一致性+重试队列+stale标记。这是TD §5.1的明确设计。
- **Alternatives considered**: (1)事务内同步双写——任一失败则全部回滚，不可接受；(2)Saga编排——过度复杂

### Decision 8: dashboard聚合SQL优化
- **Decision**: 单次查询使用CASE WHEN+GROUP BY条件聚合，替代5次独立查询
- **Rationale**: dashboard需统计total/by_status/by_tier/by_domain/pii_count，单次条件聚合查询可减少4次网络往返。加deleted_at IS NULL过滤。
- **Alternatives considered**: (1)物化视图——MySQL不支持异步刷新；(2)Redis缓存——实时性不足

## Data Model

### 新增模型: MetricStateMachine (纯逻辑，无表)

```
合法跃迁矩阵:
DRAFT     → REVIEW          (submit)
REVIEW    → PUBLISHED       (approve, PII门禁+依赖校验通过)
REVIEW    → EXPERIMENTAL    (approve, 灰度模式)
REVIEW    → DRAFT           (reject)
PUBLISHED → DEPRECATED      (deprecate, 校验successor)
PUBLISHED → PENDING_VERSION (PUT破坏性变更)
EXPERIMENTAL → PUBLISHED    (promote)
EXPERIMENTAL → PUBLISHED    (rollback, 回退上一版本)
DATA_SOURCE_DROPPED → PUBLISHED (源恢复)
DATA_SOURCE_DROPPED → DEPRECATED (确认废弃)

非法跃迁示例(返回409):
DRAFT → PUBLISHED (须先submit→approve)
DRAFT → DEPRECATED (仅PUBLISHED可废弃)
PUBLISHED → DRAFT (无反向跃迁)
```

### 新增模型: PendingVersionConfirmation (新表)

```sql
CREATE TABLE pending_version_confirmation (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    metric_id BIGINT NOT NULL,
    version INT NOT NULL,
    consumer_id BIGINT NOT NULL,       -- 消费方用户ID
    status ENUM('PENDING','CONFIRMED','REJECTED','TIMEOUT_ACCEPTED') NOT NULL DEFAULT 'PENDING',
    reason TEXT,                         -- 拒绝原因
    extension_count INT NOT NULL DEFAULT 0,  -- 延期次数(最多1次)
    deadline DATETIME NOT NULL,          -- 确认截止时间(created_at+14天+延期)
    confirmed_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE KEY uk_pending_confirm (metric_id, version, consumer_id),
    INDEX idx_pending_deadline (status, deadline)
);
```

### 新增模型: MetricHealthScore (新表)

```sql
CREATE TABLE metric_health_score (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    metric_id BIGINT NOT NULL UNIQUE,
    score INT NOT NULL DEFAULT 0,        -- 0-100
    level ENUM('EXCELLENT','GOOD','WARNING','CRITICAL') NOT NULL DEFAULT 'CRITICAL',
    completeness_score INT NOT NULL DEFAULT 0,   -- 口径完整度 0-100
    activity_score INT NOT NULL DEFAULT 0,       -- 活跃度 0-100
    quality_score INT NOT NULL DEFAULT 0,        -- 质量 0-100
    owner_response_score INT NOT NULL DEFAULT 0, -- Owner响应 0-100
    lineage_coverage_score INT NOT NULL DEFAULT 0, -- 血缘覆盖 0-100
    missing_dimensions JSON,             -- 数据不足的维度列表
    calculated_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    INDEX idx_health_level (level),
    INDEX idx_health_score (score)
);
```

### 修改模型: MetricVersion

```
status枚举扩展: DRAFT / PENDING_CONFIRMATION / PUBLISHED / EXPERIMENTAL / ARCHIVED / CANCELLED
新增字段:
- pending_deadline DATETIME          -- PENDING_VERSION确认截止时间
- extension_count INT DEFAULT 0      -- 延期次数
- effective_at DATETIME              -- 实际生效时间(confirm后记录)
```

### 修改模型: Metric

```
status枚举新增: EXPERIMENTAL, DATA_SOURCE_DROPPED
新增字段:
- emergency_publish BOOLEAN DEFAULT FALSE   -- 紧急发布标记
- emergency_reason TEXT                     -- 紧急发布原因
- emergency_reviewed_at DATETIME            -- 补审时间
- gray_tenant_ids JSON                      -- 灰度白名单租户ID列表
- pending_conflict BOOLEAN DEFAULT FALSE    -- 冲突预检标记
- pending_conflict_detail JSON              -- 冲突详情
```

### 修改模型: MetricTemplate

```
无需结构变更，但API层需新增MetricTemplateCreateRequest/MetricTemplateUpdateRequest Schema校验
```

## Contracts & Interfaces

### 新增API端点 (对齐TD §3.2)

```
POST   /metric-definitions/{code}/submit              # DRAFT→REVIEW (FR-003)
POST   /metric-definitions/{code}/approve              # REVIEW→PUBLISHED/EXPERIMENTAL (FR-004)
POST   /metric-definitions/{code}/reject               # REVIEW→DRAFT (FR-005)
POST   /metric-definitions/{code}/promote              # EXPERIMENTAL→PUBLISHED (FR-020)
POST   /metric-definitions/{code}/rollback             # EXPERIMENTAL→上一PUBLISHED (FR-020)
POST   /metric-definitions/{code}/emergency-publish    # 紧急发布 (FR-022)
POST   /metric-definitions/{code}/confirm-version      # 消费方确认版本 (FR-007)
POST   /metric-definitions/{code}/reject-version       # 消费方拒绝版本 (FR-007)
POST   /metric-definitions/{code}/extend-version       # 延期确认+7天 (FR-008)
POST   /metric-definitions/compare                     # 指标对比 (FR-029)
POST   /metric-definitions/batch-register              # 批量注册 (FR-030)
GET    /metric-definitions/{code}/health               # 健康度评分 (FR-025)
```

### 新增请求Schema

```
MetricSubmitRequest:
    change_reason: str (min_length=4)

MetricApproveRequest:
    mode: Literal["standard", "experimental"]
    gray_tenant_ids: list[int] | None  (仅experimental模式)
    target_version: int | None

MetricRejectRequest:
    reason: str (min_length=4)

MetricEmergencyPublishRequest:
    reason: str (min_length=10)
    target_version: int | None

VersionConfirmRequest:
    version: int

VersionRejectRequest:
    version: int
    reason: str (min_length=4)

VersionExtendRequest:
    version: int

MetricCompareRequest:
    metric_codes: list[str] (min_length=2, max_length=2)

MetricBatchRegisterRequest:
    source_table: str
    measure_columns: list[str]
    dimension_mapping: dict[str, str] | None
    llm_prefill: bool = True
    domain: str

MetricTemplateCreateRequest:
    code: str (max_length=64, pattern)
    name: str (max_length=128)
    domain: str (max_length=64)
    description: str | None
    defaults_json: dict
    required_fields: list[str] | None
    type: Literal["atomic","derived","composite"] | None
    ...
```

### 新增事件类型 (EventBus)

```
metric.created              -- 指标创建(DRAFT) → conflict异步预检
metric.submitted            -- 提交审核 → notify(domain_admin待审)
metric.approved             -- 审核通过 → lineage(Neo4j)+search(ES)+notify
metric.rejected             -- 驳回 → notify(Owner)
metric.deprecated           -- 废弃 → lineage+notify(下游消费方)
metric.pending_version      -- PENDING_VERSION生成 → notify(下游消费方)
metric.version_confirmed    -- 消费方确认版本 → notify(Owner)
metric.version_rejected     -- 消费方拒绝版本 → notify(Owner)
metric.promoted             -- 灰度全量 → lineage+search
metric.rolled_back          -- 灰度回滚 → notify+audit
metric.emergency_published  -- 紧急发布 → audit(EMERGENCY_PUBLISH标记)
metric.health_critical      -- 健康度<55 → notify.todo(整改待办)
```

### Arq定时任务

```
check_pending_version_timeouts  -- 每分钟检查PENDING_VERSION超时 → 默认接受
refresh_health_scores           -- 每日凌晨批量重算健康度
check_emergency_review_overdue  -- 每小时检查紧急发布24h补审
check_experimental_expiry       -- 每日检查灰度30天未决策
```

### 内部接口契约

```
# state_machine.py
class MetricStateMachine:
    TRANSITIONS: dict[MetricState, dict[MetricState, str]]  # {from: {to: action_name}}
    @classmethod
    def validate_transition(cls, from_state: MetricState, to_state: MetricState) -> str | None
    # 返回None=合法，返回字符串=拒绝原因

# dependency_checker.py
class DependencyChecker:
    async def check_dependencies_published(self, definition_json: dict) -> list[str]
    # 返回未发布/已废弃的依赖指标code列表
    async def detect_cycle(self, metric_code: str, definition_json: dict) -> list[str] | None
    # 返回环路径或None

# health_scorer.py
class HealthScorer:
    async def calculate(self, metric_id: int) -> MetricHealthScore
    async def batch_calculate(self, metric_ids: list[int]) -> list[MetricHealthScore]
    # 五维加权计算

# pending_version_manager.py
class PendingVersionManager:
    async def create_pending(self, metric: Metric, new_version: MetricVersion, consumer_ids: list[int]) -> None
    async def confirm(self, metric_id: int, version: int, consumer_id: int) -> PendingAction
    async def reject(self, metric_id: int, version: int, consumer_id: int, reason: str) -> PendingAction
    async def extend(self, metric_id: int, version: int) -> None
    async def check_timeouts(self) -> list[int]  # 返回超时的metric_id列表
    async def pause_on_drift(self, metric_id: int, version: int, drift_detail: dict) -> None

# conflict_precheck.py
class ConflictPrechecker:
    RESERVED_WORDS: frozenset[str]  # {"test","temp","dummy","demo","tmp","sample"}
    CODE_PATTERN: str  # ^[a-z][a-z0-9]*(_[a-z][a-z0-9]*){3}$
    async def precheck(self, metric_code: str, definition_json: dict) -> dict | None
    # 返回冲突详情或None
    def validate_code_format(self, code: str) -> tuple[bool, str | None]
    # 返回(合法, 错误信息)
```
