# Implementation Plan: Unisense 全维度审查整改（2026-08-13 版）

**Input**: Feature specification from `spec/full-audit-remediation/spec.md`

## Summary

修复 2026-08-13 全维度穿透式审查发现的 53 项缺陷（1×P0 + 17×P1 + 23×P2 + 12×P3），将综合得分从 54.6 提升至 ≥75（GB/T 36073 L3-稳健级）。整改分三阶段：Phase 1 紧急修复（P0+P1=18项），Phase 2 核心加固（P2=23项），Phase 3 质量提升（P3=12项）。技术路线：安全加固（PBKDF2密钥派生+JWT黑名单+XFF信任链+注入守卫修复）、性能修复（bcrypt异步化+Prometheus归一化+事件总线退避）、运营增强（健康检查降级+优雅关闭+配置热更新+降级注册中心）、测试补全（前端核心页面+API集成+安全回归+混沌测试）。

## Technical Context

**Language/Version**: Python 3.11 (FastAPI) + TypeScript (React 18 + Vite)  
**Primary Dependencies**: FastAPI, SQLAlchemy 2.0(async), Redis 7(aioredis), Neo4j 5, Elasticsearch 8.15, passlib[bcrypt], cryptography[Fernet], structlog, Prometheus, Ant Design 5  
**State Management**: N/A (非HarmonyOS项目)  
**Storage**: MySQL 8, Neo4j 5, Elasticsearch 8.15, Redis 7, Apache Doris(HTTP API)  
**Testing**: pytest(unit/integration/security/chaos/observability), vitest + @testing-library/react(前端), Playwright(E2E)  
**Target Platform**: Linux server (Docker Compose), Chrome/Firefox(前端)  
**Project Type**: Web service (monorepo: backend/ + frontend/)  
**Performance Goals**: API P99 < 500ms, 10并发登录 < 500ms, 查询 < 2s  
**Constraints**: 等保2.0合规, GB/T 36073 L3, 无零停机部署要求  
**Scale/Scope**: 后端 3292 Python文件, 前端 54 TS/TSX文件, 146测试文件, 28 Alembic迁移

## Project Structure

### Documentation (this feature)

```text
spec/full-audit-remediation/
├── spec.md              # 需求规格
├── plan.md              # 本文件
└── tasks.md             # 任务清单
```

### Source Code (repository root)

```text
backend/
├── app/
│   ├── core/            # 核心设施（整改重点区域）
│   │   ├── secrets.py   # SEC-01/02: 密钥派生+轮换
│   │   ├── guard.py     # SEC-04/05: SQL注入守卫
│   │   ├── security.py  # SEC-06/TECH-01: JWT+bcrypt
│   │   ├── audit.py     # SEC-03: 审计IP信任链
│   │   ├── config.py    # SEC-07/OPS-03: CORS+热配置
│   │   ├── middleware.py # SEC-11/12: PII脱敏+请求大小限制
│   │   ├── metrics.py   # TECH-02: Prometheus path归一化
│   │   ├── resilience.py # TECH-03: 异步探活
│   │   ├── eventbus.py  # TECH-04: 指数退避+死信队列
│   │   └── logging.py   # SEC-10: PII脱敏processor
│   ├── db/
│   │   ├── mysql.py     # TECH-07/SEC-07: 连接池+密码mask
│   │   └── redis.py     # SEC-08: TLS支持
│   ├── services/
│   │   ├── semantic/    # PROD-02: 指标编码锁定
│   │   ├── conflict/    # PROD-04: 仲裁超时
│   │   ├── quality/     # PROD-05: 规则校验
│   │   ├── glossary/    # PROD-06: 阈值可配置
│   │   ├── governance/  # PROD-07: 被遗忘权token
│   │   ├── consume/     # TECH-06/PROD-08: 限流+降级UI
│   │   ├── collector/   # SEC-09/TECH-09: beeline竞态+ClickHouse参数化
│   │   ├── llm/         # TECH-10: 重试+熔断
│   │   ├── lineage/     # TECH-11: 日志补全
│   │   └── dimension/   # PROD-02补充
│   ├── api/             # TECH-12/13: 事务+日期校验
│   │   ├── tracking.py  # TECH-13: 日期422
│   │   └── *.py         # TECH-12: UoW渐进迁移
│   └── main.py          # OPS-01/02: 优雅关闭+健康检查
├── tests/               # TEST-01~09: 测试补全
│   ├── unit/
│   ├── integration/
│   ├── security/
│   ├── chaos/
│   └── perf/
└── alembic/             # OPS-04: 迁移幂等性

frontend/
├── src/
│   ├── api.ts           # SEC-12/PROD-08/09: Token+降级提示+207解析
│   ├── pages/           # PROD-03/31: 域树限制+降级UI
│   └── __tests__/       # TEST-01: 前端测试补全
└── e2e/                 # TEST-09: E2E测试
```

**Structure Decision**: 遵循现有项目架构，不引入新目录结构。整改在现有文件中原位修改，新增文件仅限：`backend/app/core/key_rotation.py`（密钥轮换）、`backend/app/core/degradation.py`（降级注册中心扩展）、`backend/app/core/dlq.py`（死信队列）、`frontend/src/utils/apiErrorHandlers.ts`（前端错误处理增强）、`frontend/e2e/`（E2E测试目录）。

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| 密钥轮换子系统（新模块） | NIST SP 800-132 要求密钥轮换，需支持新旧密钥共存过渡期 | 简单替换方案无法保证过渡期数据可用性 |
| Unit of Work 渐进迁移 | 100+处 db.commit() 散布，一次性迁移风险高 | 一次性重构影响面太大，回归测试成本过高 |
| 降级注册中心（扩展模块） | 多组件降级状态需统一汇聚 | 各服务独立降级导致运维不可见 |

## Research & Decisions

### R&D-01: 密钥派生方案选择
- **Decision**: 使用 `hashlib.pbkdf2_hmac('sha256', password, salt, 600_000)` 替代裸 SHA-256
- **Rationale**: Python 标准库原生支持，无需引入 cryptography 库的 KDF，性能可接受（~300ms 派生一次），满足 NIST SP 800-132 要求
- **Alternatives considered**: Argon2id（需额外依赖，性能更优但过度）、cryptography.hazmat.primitives.kdf.pbkdf2（功能等价但引入更多依赖）

### R&D-02: JWT 黑名单存储
- **Decision**: Redis SET 存储 jti，TTL = JWT 过期时间；Redis 不可用时降级为进程内 set + TTL 检查
- **Rationale**: Redis 是已有依赖，SET+TTL 原子操作性能优异；进程内降级保证单实例可用
- **Alternatives considered**: 数据库表（查询性能差）、纯内存无降级（进程重启后黑名单丢失完全不可接受）

### R&D-03: bcrypt 异步化方案
- **Decision**: 使用 `asyncio.to_thread(passlib.hash.bcrypt.using(rounds=12).hash, password)` 包装
- **Rationale**: Python 3.11 原生 asyncio.to_thread，不引入新依赖，线程池复用
- **Alternatives considered**: argon2-cffi（异步但需新依赖+所有存量密码需迁移）

### R&D-04: 事件总线退避策略
- **Decision**: 指数退避 1s→2s→4s→8s→16s→30s(max)，3次重试后写入内存死信队列，定时重放
- **Rationale**: 简单可靠，内存死信队列足够（事件量不大），避免引入消息队列
- **Alternatives considered**: Redis List 死信队列（增加复杂度，事件量不大无必要）

### R&D-05: Unit of Work 迁移节奏
- **Decision**: 渐进式迁移，Phase 1 仅迁移涉及 P0/P1 的 API 端点（metrics/collector/governance），其余后续迭代
- **Rationale**: 100+处一次性迁移回归风险过高，P0/P1 相关端点优先
- **Alternatives considered**: 一次性全量迁移（风险过高）、不迁移（不解决根本问题）

### R&D-06: 前端 Token 存储方案
- **Decision**: 缩短 JWT 有效期至 15 分钟 + refresh token 机制，仍用 localStorage（不迁移到 httpOnly cookie）
- **Rationale**: 迁移到 httpOnly cookie 需改动登录 API 返回方式（JSON→Set-Cookie），影响前端+后端+网关三层，改动范围过大；缩短有效期+refresh token 可显著降低 XSS 窃取的窗口期
- **Alternatives considered**: 完全迁移到 httpOnly cookie（改动范围过大，风险高）

### R&D-07: E2E 测试框架
- **Decision**: 使用 Playwright + Docker Compose 启动完整环境
- **Rationale**: Playwright 原生支持多浏览器、网络拦截、自动等待；Docker Compose 已有完整开发环境
- **Alternatives considered**: Cypress（不如 Playwright 灵活）、不引入E2E（无法验证端到端流程）

### R&D-08: 配置热更新方案
- **Decision**: 关键配置项（限流阈值/OLAP超时/LLM端点）从 Redis Hash 读取，定时刷新（30s间隔）
- **Rationale**: Redis 已有依赖，Hash 结构适合配置存储，30s 刷新间隔平衡实时性和性能
- **Alternatives considered**: ETCD/Consul（引入新依赖，过度设计）、环境变量重载（需重启）

## Data Model

### KeyRotation（密钥轮换记录）
- id: Integer PK
- key_id: String(64) UNIQUE — 密钥标识(SHA-256 of key material)
- purpose: String(32) — 用途(FERNET/JWT)
- status: Enum(ACTIVE/DEPRECATED/REVOKED)
- created_at: DateTime
- deprecated_at: DateTime nullable
- rotated_by: Integer FK→users — 执行轮换的操作人

### JwtBlacklistEntry（JWT黑名单）
- jti: String(36) PK — UUID4
- expired_at: DateTime — JWT 原始过期时间
- created_at: DateTime — 加入黑名单时间
- reason: String(32) — 原因(LOGOUT/FORCE_RESET/SECURITY)

### DegradationEntry（降级注册中心条目）
- component: String(64) — 组件名(redis/neo4j/es/olap/llm)
- status: Enum(HEALTHY/DEGRADED/DOWN)
- reason: Text nullable — 降级原因
- since: DateTime — 降级开始时间
- last_check: DateTime — 最近检查时间

### FeatureFlag（特性开关）
- name: String(128) PK — 开关名称
- enabled: Boolean — 全局启用状态
- target_domains: JSON nullable — 目标域列表
- target_users: JSON nullable — 目标用户ID列表
- description: Text nullable
- created_at: DateTime
- updated_at: DateTime

### DeadLetterEvent（事件总线死信）
- id: Integer PK AUTO
- event_type: String(128) — 原始事件类型
- payload: JSON — 原始事件载荷
- failure_reason: Text — 失败原因
- retry_count: Integer — 已重试次数
- created_at: DateTime
- last_retry_at: DateTime nullable
- status: Enum(PENDING/RETRIED/EXHAUSTED)

## Contracts & Interfaces

### 密钥轮换 API
- `POST /api/v1/admin/key-rotation/rotate` — 执行密钥轮换
  - Request: `{ purpose: "FERNET" }`
  - Response: `{ old_key_id: "...", new_key_id: "...", migrated_count: N }`
  - Auth: admin only

### JWT 黑名单内部接口
- `add_to_blacklist(jti: str, exp: datetime, reason: str)` → void
- `is_blacklisted(jti: str) → bool`
- Redis key: `jwt:bl:{jti}`, TTL = exp - now

### 降级注册中心接口
- `register_degradation(component: str, reason: str)` → void
- `clear_degradation(component: str)` → void
- `get_all_degradations() → list[DegradationEntry]`
- `/health` 端点扩展：包含 degradations 字段

### 配置热更新接口
- `get_config(key: str, default: Any) → Any` — 从 Redis Hash 读取，fallback 到 Settings
- Redis key: `unisense:config:{key}`
- 刷新间隔: 30s

### 特性开关接口
- `is_feature_enabled(name: str, domain: str | None, user_id: int | None) → bool`
- `GET /api/v1/admin/feature-flags` — 列出所有开关
- `PUT /api/v1/admin/feature-flags/{name}` — 更新开关

### 事件总线死信队列接口
- `send_to_dlq(event_type: str, payload: dict, reason: str)` → void
- `retry_dlq(limit: int = 10)` → int — 重试死信，返回成功数
- `GET /api/v1/admin/dead-letter-events` — 管理接口

### 冲突仲裁超时接口
- 内部定时任务: 每10分钟扫描 OPEN 状态 >72h 的冲突，自动执行 escalate
- 不暴露新 API，复用现有 escalate 端点逻辑

### 前端错误处理增强
- `handleDegradedEngine(error: UnisenseApiError)` — 展示降级提示
- `parseMultiStatus(response: Response)` → `{ succeeded: string[], failed: {id: string, error: string}[] }`
- 207 响应解析工具函数
