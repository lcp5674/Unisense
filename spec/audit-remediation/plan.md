# Implementation Plan: Unisense 全维度审查整改

**Input**: Feature specification from `spec/audit-remediation/spec.md`

## Summary

修复审查报告全部26项问题（P0×4 + P1×9 + P2×8 + P3×5），将GB/T 36073综合加权得分从65.4提升至≥75（L3-稳健级）。技术路线：后端修复安全缺陷+填充空壳实现+基础设施加固；前端补全缺失功能+引入组件库+建立测试体系；统一架构抽象（EventBus/BaseService）降低技术债。

## Technical Context

**Language/Version**: Python 3.11 (后端), TypeScript 5.6 (前端)
**Primary Dependencies**: FastAPI 0.115, SQLAlchemy 2.0, aiomysql, aioredis, httpx (后端); React 18, Vite 5 (前端)
**State Management**: 后端无全局状态管理（服务无状态）；前端当前为组件内useState，需引入轻量状态管理
**Storage**: MySQL 8.0, Neo4j 5, ES 8.15, Redis 7, Apache Doris (新增OLAP), S3/MinIO (新增归档)
**Testing**: pytest (后端107个测试文件); Vitest + Playwright (前端待建设)
**Target Platform**: Linux server (后端Docker), 现代浏览器 (前端)
**Project Type**: Web service + SPA
**Performance Goals**: consume P95≤300ms, NL2SQL P95≤2s, 全局限流误差≤5%
**Constraints**: 等保2.0合规, PIPL合规, 无破坏性迁移
**Scale/Scope**: 14个后端服务, 7个前端页面(需扩展至15+)

## Project Structure

### Documentation (this feature)

```text
spec/audit-remediation/
├── spec.md              # Feature specification
├── plan.md              # This file
└── tasks.md             # Task breakdown (Phase 3)
```

### Source Code (repository root)

```text
backend/app/
├── main.py                          # [修改] 添加lifespan Redis管理+CORS严格配置+校验钩子
├── core/
│   ├── config.py                    # [修改] 添加生产校验器(jwt_secret≥32/Fernet独立/olap_url必填)
│   ├── guard.py                     # [修改] 递归扫描嵌套JSON body
│   ├── secrets.py                   # [修改] 移除JWT密钥派生降级路径
│   ├── resilience.py                # [修改] 熔断器集成到服务调用链
│   ├── audit.py                     # [修改] 支持独立事务写入
│   ├── eventbus.py                  # [新增] 统一事件总线抽象
│   └── base_service.py              # [新增] 服务基类Protocol
├── db/
│   ├── mysql.py                     # [修改] 移除yield后自动commit
│   └── redis.py                     # [修改] 纳入lifespan管理
├── services/
│   ├── ai/service.py                # [修改] 参数化SQL+execute委托consume
│   ├── consume/
│   │   ├── service.py               # [修改] Redis限流器+Doris执行引擎
│   │   └── olap_executor.py         # [新增] Doris HTTP API执行器
│   ├── notify/service.py            # [修改] SMTP邮件通道+钉钉通道增强
│   ├── collector/
│   │   ├── service.py               # [修改] 强制Arq队列生产环境
│   │   └── queue.py                 # [修改] Arq队列为默认实现
│   ├── quality/service.py           # [修改] datetime.utcnow→now(UTC)
│   ├── assetmap/service.py          # [修改] 新增图谱/热力/责任人端点
│   ├── recommend/service.py         # [修改] 协同过滤推荐算法
│   ├── governance/service.py        # [修改] PII血缘传播
│   ├── semantic/service.py          # [修改] 模板包/消费指南API
│   └── observability/service.py     # [修改] NPS采集+反馈闭环
├── api/
│   ├── assetmap.py                  # [修改] 新增图谱/热力/责任人路由
│   ├── semantic.py                  # [修改] 新增模板包/驾驶舱路由
│   └── observability.py             # [修改] 新增NPS/反馈采纳路由
├── models/
│   ├── tracking.py                  # [新增] 埋点事件模型
│   ├── template.py                  # [新增] 模板包模型
│   └── audit_archive.py             # [新增] 审计归档元数据模型
└── tasks/
    └── audit_archive.py             # [新增] 定时归档Celery/Arq任务

frontend/
├── package.json                     # [修改] 添加antd/router/zustand/vitest/playwright
├── vite.config.ts                   # [修改] 添加test配置
├── src/
│   ├── App.tsx                      # [修改] 替换为react-router路由
│   ├── main.tsx                     # [微调] Provider包装
│   ├── api.ts                       # [修改] 扩展API方法
│   ├── types.ts                     # [修改] 扩展类型定义
│   ├── store/
│   │   └── index.ts                 # [新增] Zustand全局状态
│   ├── components/
│   │   ├── Layout.tsx               # [新增] 统一布局(侧边栏+顶栏+面包屑)
│   │   ├── QuickBIEmbed.tsx         # [新增] QuickBI iframe嵌入组件
│   │   └── TrackingProvider.tsx     # [新增] 埋点Provider
│   ├── pages/
│   │   ├── Dashboard.tsx            # [新增] 治理驾驶舱
│   │   ├── ConsumptionGuide.tsx     # [新增] 消费指南Tab
│   │   └── (现有7页面重构)
│   └── hooks/
│       └── useTracking.ts           # [新增] 埋点Hook
├── e2e/
│   ├── smoke.mjs                    # [保留]
│   └── *.spec.ts                    # [新增] Playwright E2E测试
└── __tests__/
    └── *.test.tsx                   # [新增] Vitest组件测试

docker-compose.yml                   # [修改] 添加Doris服务+ES安全配置
```

**Structure Decision**: 本项目为已有Python/React项目，遵循现有架构和目录约定，不引入MVVM或其他架构迁移。后端保持FastAPI三层结构（API→Service→Repository），前端在现有React结构上扩展组件/状态/路由，不重构为Next.js或其他框架。

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| 引入Apache Doris | FR-005要求真实OLAP执行，现有consume仅空壳 | 继续空壳不可接受（P0级缺陷） |
| 前端引入Ant Design | 7个页面手工CSS不可维护，需组件库统一交互 | 自建组件库耗时过长，Ant Design成熟可靠 |
| 前端引入react-router | 现有state路由无URL映射，无法深链接/书签 | state路由无法满足QuickBI嵌入回调URL需求 |

## Research & Decisions

### Decision 1: OLAP执行引擎实现路径
- **Decision**: 通过Doris HTTP API(8030端口)提交SQL查询，解析JSON结果集返回
- **Rationale**: Doris HTTP API无需额外JDBC驱动，Python httpx原生支持，连接池管理简单
- **Alternatives considered**: 
  - JDBC via JayDeBeApi: 需Java运行时+JDBC驱动，部署复杂
  - MySQL协议直连Doris: Doris兼容MySQL协议但需额外端口(9030)，增加攻击面
  - PyDoris: 官方Python SDK但依赖重，社区活跃度低

### Decision 2: Redis限流器算法选择
- **Decision**: Redis滑动窗口限流（sorted set + timestamp score）
- **Rationale**: 滑动窗口精度高，支持全局配额+接入方级配额双维度，Redis不可用时降级为本地令牌桶
- **Alternatives considered**:
  - 固定窗口: 窗口边界突发问题
  - 令牌桶(Redis): 需要Lua脚本保证原子性，实现复杂度高
  - 漏桶: 不支持突发流量

### Decision 3: 前端组件库选择
- **Decision**: Ant Design 5.x (antd)
- **Rationale**: React生态最成熟的企业级组件库，中文文档完善，内置ProTable/ProForm适合数据治理场景
- **Alternatives considered**:
  - Arco Design: 字节跳动出品，组件略少
  - MUI: 国际化优先，中文场景不如antd
  - 自建: 开发周期不可接受

### Decision 4: 前端路由选择
- **Decision**: react-router-dom v6
- **Rationale**: React生态标准路由库，支持URL映射/深链接/代码分割，QuickBI回调需要URL路由
- **Alternatives considered**:
  - TanStack Router: 类型安全但学习曲线陡
  - 继续state路由: 无法满足QuickBI嵌入和E2E测试需求

### Decision 5: 前端状态管理选择
- **Decision**: Zustand
- **Rationale**: 轻量(<1KB)，无Provider包裹限制，TypeScript友好，适合中小规模全局状态
- **Alternatives considered**:
  - Redux Toolkit: 过重，当前规模不需要
  - Jotai: 原子化状态，不适合全局共享场景
  - Context API: 性能问题（全量重渲染）

### Decision 6: 埋点存储选型
- **Decision**: Elasticsearch（与现有搜索共用集群）
- **Rationale**: 已有ES 8.15集群，零新增运维成本，ES聚合查询天然适合行为分析，支持Kibana可视化
- **Alternatives considered**:
  - ClickHouse: 高吞吐但需新增集群，运维成本高
  - MySQL: 聚合查询性能差，影响业务库
  - Redis: 不适合持久化历史数据

### Decision 7: 审计归档实现路径
- **Decision**: Arq定时任务 + S3/MinIO存储
- **Rationale**: 项目已使用Arq（采集队列），复用同一任务框架；MinIO兼容S3 API，docker-compose已有MinIO
- **Alternatives considered**:
  - Celery: 更重，需额外消息队列(RabbitMQ/Redis)
  - 简单cron脚本: 不在应用生命周期内，难以监控

### Decision 8: QuickBI SSO集成方案
- **Decision**: 嵌入报表Ticket方案（QuickBI GenerateTicket API → iframe URL拼ticket参数）
- **Rationale**: QuickBI标准嵌入方案，无需OIDC/OAuth2复杂流程，后端调用QuickBI OpenAPI获取ticket，前端iframe携带ticket访问
- **Alternatives considered**:
  - OAuth2 OIDC: QuickBI嵌入报表不直接支持OIDC流程
  - 自研Token: 安全性不足，需QuickBI侧适配

## Data Model

### 新增实体

#### TrackingEvent (埋点事件)
| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | VARCHAR(36) | PK | UUID |
| event_type | VARCHAR(32) | NOT NULL | search/query/approve/browse/nps |
| actor_id | VARCHAR(36) | NOT NULL, FK→user.id | 操作人 |
| target_id | VARCHAR(36) | NULL | 目标对象ID(指标/术语等) |
| target_type | VARCHAR(32) | NULL | metric/term/glossary |
| context_json | JSON | NULL | 事件上下文(搜索关键词/查询参数等) |
| created_at | DATETIME | NOT NULL | 事件时间 |

#### MetricTemplate (模板包)
| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | VARCHAR(36) | PK | UUID |
| name | VARCHAR(200) | NOT NULL, UNIQUE | 模板名称 |
| domain | VARCHAR(100) | NOT NULL | 所属域 |
| category | VARCHAR(100) | NULL | 分类 |
| definition_template | JSON | NOT NULL | 指标定义模板(口径/维度/粒度占位) |
| description | TEXT | NULL | 模板说明 |
| created_by | VARCHAR(36) | NOT NULL, FK→user.id | 创建人 |
| created_at | DATETIME | NOT NULL | 创建时间 |
| updated_at | DATETIME | NOT NULL | 更新时间 |

#### AuditArchiveLog (审计归档日志)
| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | VARCHAR(36) | PK | UUID |
| archive_date | DATE | NOT NULL | 归档日期 |
| rows_archived | INT | NOT NULL | 归档行数 |
| s3_key | VARCHAR(500) | NOT NULL | S3对象键 |
| s3_size_bytes | BIGINT | NOT NULL | 对象大小 |
| status | VARCHAR(20) | NOT NULL | pending/completed/failed |
| created_at | DATETIME | NOT NULL | 任务创建时间 |
| completed_at | DATETIME | NULL | 完成时间 |

### 修改实体

#### AuditLog (审计日志)
- 新增字段: `archived BOOLEAN DEFAULT FALSE` — 标记是否已归档

#### Metric (指标)
- 新增字段: `template_id VARCHAR(36) NULL FK→metric_template.id` — 关联模板

#### Notification (通知)
- 新增字段: `channel VARCHAR(20) DEFAULT 'console'` — 通知渠道(console/webhook/email/dingtalk)

### PII传播规则
- 血缘边(LineageEdge)新增属性: `pii_inherited BOOLEAN DEFAULT FALSE`
- 下游指标自动继承上游PII标记: 若任一上游source_column含pii=True，则下游metric.definition_json.pii自动设为True

## Contracts & Interfaces

### 新增API端点

#### OLAP执行引擎
```
POST /api/v1/consume/query
  Request:  { metric_code, dimensions?, filters?, limit?, offset? }
  Response: { sql, rows, total, elapsed_ms, from_cache }
  Error:    503 DEPENDENCY_DEGRADED_ENGINE (Doris不可用时)
```

#### 图谱数据端点
```
GET /api/v1/assetmap/graph
  Query:    domain?, depth?, pii_only?
  Response: { nodes: [{id, type, label, pii, domain, owner}], edges: [{source, target, type}] }

GET /api/v1/assetmap/heatmap
  Query:    dimension? (sensitivity|domain|owner)
  Response: { buckets: [{key, count, pii_count}] }

GET /api/v1/assetmap/owner-view
  Query:    owner_id?
  Response: { owners: [{owner_id, owner_name, metric_count, pii_count, health_score}] }
```

#### 模板包端点
```
POST   /api/v1/semantic/templates       — 创建模板
GET    /api/v1/semantic/templates        — 列表(分页)
GET    /api/v1/semantic/templates/{id}   — 详情
DELETE /api/v1/semantic/templates/{id}   — 删除
POST   /api/v1/semantic/templates/{id}/instantiate — 从模板创建指标
```

#### 驾驶舱聚合端点
```
GET /api/v1/semantic/dashboard
  Response: {
    total_metrics, published_count, draft_count, deprecated_count,
    conflict_count, review_pending_count, avg_review_hours,
    pii_metric_count, quality_anomaly_count, top_domains: [{domain, count}]
  }
```

#### NPS与反馈闭环
```
POST /api/v1/observability/nps
  Request:  { score: int(0-10), target_id, target_type, comment? }
  Response: { nps_id }

PATCH /api/v1/observability/feedback/{id}/status
  Request:  { status: "acknowledged"|"adopted"|"dismissed", response_note? }
  Response: { feedback_id, status }
```

#### 埋点事件
```
POST /api/v1/tracking/event
  Request:  { event_type, target_id?, target_type?, context? }
  Response: { event_id }

GET /api/v1/tracking/stats
  Query:    event_type?, start_date?, end_date?, group_by?
  Response: { stats: [{group_key, event_count, unique_actors}] }
```

#### 消费指南
```
GET /api/v1/semantic/metrics/{code}/consumption-guide
  Response: { metric_code, definition, calculation_logic, dimensions, usage_examples, related_metrics, faq }
```

### 修改API端点

#### NL2SQL execute增强
```
POST /api/v1/ai/nl2sql
  Request:  { nl_query, metric_scope?, execute?: boolean }
  Response: { 
    sql, anchored_words, safety_status,
    execute_result?: { rows, total, elapsed_ms },  // execute=true时
    execute_error?: string                          // 执行失败时
  }
```

#### 审计归档
```
POST /api/v1/audit/archive
  Request:  { before_date, dry_run? }
  Response: { archive_id, rows_to_archive }

GET /api/v1/audit/archive/{id}
  Response: { status, rows_archived, s3_key, completed_at? }
```

### 内部接口

#### EventBus协议
```
EventBus.publish(event_type: str, payload: dict, actor_id: str) → None
EventBus.subscribe(event_type: str, handler: Callable) → None
EventBus.unsubscribe(event_type: str, handler: Callable) → None
```
- 后端实现: Redis Pub/Sub + 本地订阅者注册表
- 替代所有 _safe_publish 散落实现

#### BaseService协议
```
BaseService.__init__(db: AsyncSession, eventbus: EventBus, settings: Settings)
BaseService._write_audit(action, target_type, target_id, detail, pii_flag) → None
BaseService._publish_event(event_type, payload) → None
```

#### OLAPExecutor接口
```
OLAPExecutor.execute(sql: str, params: dict, timeout: float) → OLAPResult
OLAPResult: { rows: list[dict], total: int, elapsed_ms: float, from_cache: bool }
```
- Doris实现: HTTP POST to `http://{doris_host}:8030/api/{database}/{table}`
- 熔断保护: CircuitBreaker包裹，5次失败后打开

#### RedisRateLimiter接口
```
RedisRateLimiter.allow(key: str, qps_limit: int) → bool
RedisRateLimiter.allow_daily(key: str, daily_quota: int) → bool
```
- 滑动窗口实现: Redis ZADD+ZREMRANGEBYSCORE+ZCARD原子操作
- 降级: Redis不可用时回退到InMemoryRateLimiter+告警日志

### 前端组件接口

#### QuickBIEmbed
```
Props: { reportId: string, dashboardId?: string, params?: Record<string, string> }
State: { ticket: string, loading: boolean, error: string|null }
Lifecycle: mount→fetchTicket→buildIframeUrl→render
```

#### TrackingProvider
```
Context: { track: (eventType, targetId?, targetType?, context?) => void }
Hooks: useTracking() → { track }
```

#### Dashboard (驾驶舱)
```
Props: {}
State: { summary: DashboardSummary, loading: boolean }
Data source: GET /api/v1/semantic/dashboard
Sections: 全局健康度卡片 | 冲突趋势图 | 审核时效图 | 域分布Top5 | PII指标统计
```

#### ConsumptionGuide
```
Props: { metricCode: string }
State: { guide: ConsumptionGuide, loading: boolean }
Data source: GET /api/v1/semantic/metrics/{code}/consumption-guide
Sections: 口径定义 | 计算逻辑 | 维度说明 | 使用示例 | 关联指标 | FAQ
```
