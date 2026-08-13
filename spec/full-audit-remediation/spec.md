# Feature Specification: Unisense 全维度审查整改（2026-08-13 版）

**Created**: 2026-08-13  
**Status**: Draft  
**Input**: 基于 `spec/full-audit-report-2026-08-13.md` 审查报告的全部53项问题，覆盖P0-P3四个优先级，前后端同步整改，对标 GB/T 36073 L3 稳健级

## Overview

Unisense 指标语义中台经全维度穿透式审查（2026-08-13），识别出53项需整改问题，综合加权得分54.6/100（L1-需强化级），距工业级标准（L3-稳健级≥75分）差20.4分。本次整改目标：修复全部53项问题，将综合得分提升至≥75分，达到 GB/T 36073 L3-稳健级标准。整改范围覆盖安全合规13项、技术架构13项、产品逻辑9项、测试覆盖9项、运营支撑9项，前后端同步整改。

## User Scenarios & Testing

### User Story 1 - 安全工程师修复密钥派生致命缺陷 (Priority: P0)

安全工程师需要确保密钥派生使用标准算法（PBKDF2），所有加密凭据无法被离线破解。

**Why this priority**: SEC-01 是唯一P0项，SHA-256无盐无迭代密钥派生违反 NIST SP 800-132，所有加密数据源凭据和 API Key 可被彩虹表/预计算快速破解，属等保2.0一票否决项。

**Independent Test**: 可通过密码学审计脚本验证：1) derive_key() 使用 PBKDF2-HMAC-SHA256 且迭代次数≥600,000；2) 已有加密数据成功迁移解密→新密钥加密；3) 新旧密钥可共存过渡期。

**Acceptance Scenarios**:

1. **Given** secrets.py derive_key() 函数, **When** 调用密钥派生, **Then** 使用 PBKDF2-HMAC-SHA256 + 随机16-byte salt + ≥600,000 迭代
2. **Given** 旧密钥加密的数据源凭据, **When** 系统启动, **Then** 自动用旧密钥解密→新密钥加密→原子替换，过渡期支持旧密钥解密
3. **Given** 密钥轮换操作, **When** 执行轮换, **Then** 旧密钥保留于解密列表直至所有数据迁移完成

---

### User Story 2 - 安全工程师加固认证与审计链 (Priority: P1)

安全工程师需要确保 JWT 可撤销、审计 IP 不可伪造、SQL 注入防护不误杀合法请求。

**Why this priority**: SEC-02~06 是5个P1安全项，影响认证完整性和审计不可篡改性。

**Independent Test**: 1) JWT payload 含 jti 字段；2) logout 后 jti 加入 Redis 黑名单，同一 token 再次请求返回 401；3) X-Forwarded-For 仅在可信代理链内取值；4) 日期参数 `2024-01-15` 不被注入防护拦截；5) 超10层嵌套请求被拦截而非放行。

**Acceptance Scenarios**:

1. **Given** 用户登录获取 JWT, **When** 检查 token payload, **Then** 包含 jti (UUID4) 字段
2. **Given** 用户执行 logout, **When** 同一 JWT 再次请求, **Then** 返回 401 (jti 在黑名单中)
3. **Given** 请求经过可信反向代理, **When** 读取客户端 IP, **Then** 取 trusted_proxies 白名单内代理传递的 XFF 值
4. **Given** 请求含日期参数 `2024-01-15`, **When** 经过 SQL 注入守卫, **Then** 不被拦截
5. **Given** 请求含 >10 层嵌套 JSON, **When** 经过 SQL 注入守卫, **Then** 返回 422 而非放行
6. **Given** 生产环境 JWT_SECRET < 32 字符, **When** 应用启动, **Then** 拒绝启动

---

### User Story 3 - 后端工程师消除事件循环阻塞 (Priority: P1)

后端工程师需要确保所有同步阻塞调用（bcrypt哈希、TCP探活）不阻塞事件循环。

**Why this priority**: TECH-01/03 是性能P1项，并发登录/注册时 bcrypt 阻塞可导致全站响应延迟。

**Independent Test**: 1) hash_password 执行期间事件循环不被阻塞（可并发处理其他请求）；2) _tcp_alive 使用 asyncio.open_connection 异步探活。

**Acceptance Scenarios**:

1. **Given** 10 个并发登录请求, **When** 执行 hash_password/verify_password, **Then** 总耗时接近单次 bcrypt 耗时而非10倍串行
2. **Given** 依赖探活调用 _tcp_alive, **When** 目标不可达超时, **Then** 不阻塞事件循环其他协程

---

### User Story 4 - 运维工程师实现基础设施可观测 (Priority: P1)

运维工程师需要健康检查反映真实降级状态，优雅关闭释放所有连接，关键配置可热更新。

**Why this priority**: OPS-01/02/03 是运营P1项，当前健康检查假绿、连接泄漏、配置变更需重启。

**Independent Test**: 1) Redis 不可用时 /health 返回 degraded；2) 优雅关闭时 MySQL/Neo4j/ES 连接池全部 dispose；3) 限流阈值可通过 Redis 热更新无需重启。

**Acceptance Scenarios**:

1. **Given** Redis 连接断开, **When** 请求 /health, **Then** 返回 degraded 状态码 503 且 body 包含降级详情
2. **Given** 应用收到 SIGTERM, **When** 执行优雅关闭, **Then** MySQL engine.dispose() + Neo4j driver.close() + ES client.close() + Redis close 全部执行
3. **Given** 运维修改 Redis 中限流阈值配置, **When** 服务下次读取, **Then** 使用新阈值无需重启

---

### User Story 5 - 安全工程师修复安全细节 (Priority: P2)

安全工程师需要修复数据库连接串密码泄露、Redis 无 TLS、beeline 竞态、日志 PII、请求大小限制等安全问题。

**Why this priority**: SEC-07~12 是P2安全项，影响纵深防御体系完整性。

**Independent Test**: 1) 数据库连接串异常日志中密码被 mask；2) 日志中邮箱/手机/IP 被脱敏为 [REDACTED]；3) 请求 body > 10MB 返回 413。

**Acceptance Scenarios**:

1. **Given** 数据库连接异常, **When** 记录错误日志, **Then** 密码部分被 mask 为 ***
2. **Given** structlog 处理含 PII 的日志, **When** 输出日志, **Then** 邮箱/手机号/IP 被替换为 [REDACTED]
3. **Given** 请求 body > 10MB, **When** 到达中间件, **Then** 返回 413 Payload Too Large
4. **Given** CORS 配置含内网地址, **When** 生产模式启动, **Then** 拒绝启动

---

### User Story 6 - 后端工程师修复技术架构缺陷 (Priority: P2)

后端工程师需要修复事件总线退避、缓存锁上限、连接池健康检查、事务编排、ClickHouse参数化等问题。

**Why this priority**: TECH-04~12 是P2技术项，影响系统稳定性和可维护性。

**Independent Test**: 1) 事件发布失败后重试间隔指数增长；2) 缓存锁字典大小有上限；3) MySQL 连接池 pre_ping 开启；4) ClickHouse 查询使用参数化而非字符串拼接。

**Acceptance Scenarios**:

1. **Given** 事件发布失败, **When** 重试, **Then** 间隔 1s→2s→4s→...→max 30s，3次后写入死信队列
2. **Given** 高并发缓存 miss, **When** 同时请求同一 key, **Then** _LOCKS 字典大小不超过 10,000
3. **Given** MySQL 闲置连接被服务端关闭, **When** 客户端复用, **Then** pool_pre_ping 检测并重建
4. **Given** tracking API 收到格式错误的日期参数, **When** 解析失败, **Then** 返回 422 而非静默忽略

---

### User Story 7 - 产品经理修正产品逻辑缺陷 (Priority: P2)

产品经理需要确保指标编码锁定、冲突仲裁超时、质量规则校验、术语冲突阈值可配置、批量废弃部分失败可见。

**Why this priority**: PROD-02~09 是P2/P3产品项，影响业务正确性和用户体验。

**Independent Test**: 1) 指标编码创建后不可修改；2) 冲突 OPEN 超72小时自动升级；3) 无效质量规则创建被拒绝；4) 前端对207响应逐项展示。

**Acceptance Scenarios**:

1. **Given** 指标已创建(DRAFT状态), **When** 尝试修改编码, **Then** 需审批流程且仅在DRAFT允许
2. **Given** 冲突 OPEN 状态超过72小时, **When** 定时任务检查, **Then** 自动升级(escalate)并通知域管理员
3. **Given** 质量规则阈值格式异常, **When** 创建规则, **Then** 返回 422 错误而非静默创建
4. **Given** 批量废弃返回 207, **When** 前端解析响应, **Then** 逐项展示成功/失败状态

---

### User Story 8 - 测试工程师补全测试覆盖 (Priority: P1)

测试工程师需要将前端测试从6/32页提升至核心页面全覆盖，API集成测试补全，安全缺陷添加回归测试。

**Why this priority**: TEST-01/02/03 是P1测试项，当前前端仅18.75%有测试，安全P0/P1无回归防线。

**Independent Test**: 1) 前端核心页面(指标详情/查询/冲突/治理)有测试；2) 全部API端点有集成测试；3) SEC-01~06有安全回归测试。

**Acceptance Scenarios**:

1. **Given** 前端核心页面, **When** 运行测试, **Then** MetricDetail/QueryWorkspace/ConflictCenter/DataGovernance 至少有 happy path 测试
2. **Given** 全部24个API端点文件, **When** 运行集成测试, **Then** 每个端点至少有 happy path + error path
3. **Given** SEC-01~06安全缺陷, **When** 运行安全测试, **Then** 密钥派生/JWT撤销/XFF伪造/日期误拦截/嵌套绕过有回归用例

---

### User Story 9 - 运维工程师增强运营能力 (Priority: P2)

运维工程师需要实现迁移自动化验证、降级统一面板、灰度发布特性开关、审计容量预警等运营能力。

**Why this priority**: OPS-04~09 是P2/P3运营项，影响生产运维效率和安全性。

**Independent Test**: 1) CI中迁移 upgrade→downgrade→upgrade 幂等性测试通过；2) /health/degraded 展示全局降级状态；3) 管理员初始密码可通过环境变量配置。

**Acceptance Scenarios**:

1. **Given** Alembic迁移脚本, **When** CI执行, **Then** upgrade→downgrade→upgrade 幂等性测试通过
2. **Given** 多个组件降级, **When** 请求 /health/degraded, **Then** 返回统一降级面板包含所有降级组件及原因
3. **Given** 容器首次启动, **When** 设置 ADMIN_INITIAL_PASSWORD 环境变量, **Then** 管理员使用该密码而非默认 admin123

---

### Edge Cases

- 密钥迁移过程中旧密钥数据能否正确解密？如果旧密钥已丢失怎么办？
- JWT 黑名单在 Redis 不可用时如何处理？（降级策略：内存黑名单 or 拒绝所有待撤销 token）
- bcrypt 异步包装在 Windows 上的 asyncio.to_thread 行为是否一致？
- 前端 Token 从 localStorage 迁移到 httpOnly cookie 后，消费令牌（API客户端JWT）如何传递？
- 批量废弃 207 响应体格式是否与前端解析逻辑兼容？

## Requirements

### Functional Requirements

**安全合规 (SEC)**

- **FR-001**: 密钥派生必须使用 PBKDF2-HMAC-SHA256，salt ≥ 16字节随机，iterations ≥ 600,000
- **FR-002**: 支持密钥轮换协议：旧密钥→解密→新密钥加密→原子替换，90天过期策略
- **FR-003**: 审计客户端 IP 必须验证反向代理信任链，仅信任 settings.trusted_proxies 白名单内代理
- **FR-004**: SQL注入防护必须排除日期上下文（前后为数字的 `--` 不视为注释），超深嵌套(>10层)必须拦截而非放行
- **FR-005**: JWT 必须包含 jti(UUID4) 字段，logout 时将 jti 加入 Redis 黑名单集合
- **FR-006**: 生产环境 CORS 必须禁止通配符和内网地址(127.0.0.1/0.0.0.0)
- **FR-007**: 数据库连接串在异常日志中必须 mask 密码部分
- **FR-008**: 生产环境 Redis 连接必须使用 TLS (rediss://)
- **FR-009**: beeline 密码临时文件必须使用 fd 直接写入+chmod 后再关闭，消除竞态窗口
- **FR-010**: structlog 必须配置 PII 脱敏 processor，邮箱/手机/IP 替换为 [REDACTED]
- **FR-011**: 中间件必须限制请求 body 大小（≤10MB），超出返回 413
- **FR-012**: 前端 JWT 应从 localStorage 迁移至 httpOnly cookie 或缩短有效期至15分钟+refresh token

**技术架构 (TECH)**

- **FR-013**: hash_password/verify_password 必须使用 asyncio.to_thread() 包装同步 bcrypt 调用
- **FR-014**: Prometheus HTTP 请求指标 path 标签必须做路由归一化，去除动态 ID 段
- **FR-015**: _tcp_alive 必须改用 asyncio.open_connection() 异步探活
- **FR-016**: 事件总线重试必须使用指数退避(1s→2s→4s→max 30s)，3次后写入死信队列
- **FR-017**: 缓存锁字典必须有大小上限(10,000)，使用 LRU 策略淘汰
- **FR-018**: MySQL 连接池必须设置 pool_pre_ping=True + pool_recycle=1800
- **FR-019**: tracking API 日期参数格式错误必须返回 422 而非静默忽略
- **FR-020**: ClickHouse 连接器查询必须使用参数化查询替代字符串拼接
- **FR-021**: LLM 客户端必须添加重试(3次+指数退避)+熔断器，运行时自动降级
- **FR-022**: 大量 except Exception 静默吞错处必须至少添加 logger.warning()
- **FR-023**: 引入 Unit of Work 模式统一事务边界管理（渐进式，API层commit逐步迁移至Service层）

**产品逻辑 (PROD)**

- **FR-024**: 指标编码创建后仅在 DRAFT 状态允许修改且需审批
- **FR-025**: 冲突仲裁 OPEN 状态超过72小时必须自动升级(escalate)
- **FR-026**: 质量规则阈值格式异常必须拒绝创建（返回422）
- **FR-027**: 术语同义词冲突阈值(80%)必须可配置（移入 system_dict 或 settings）
- **FR-028**: 被遗忘权 token 必须使用加密安全随机数(secrets.token_hex)，不关联 user_id
- **FR-029**: 前端对 DEPENDENCY_DEGRADED_ENGINE 错误码必须展示专用降级提示
- **FR-030**: 前端对 207 Multi-Status 响应必须逐项解析展示成功/失败状态
- **FR-031**: 前端主题域树组件第3层节点禁止显示"添加子域"操作

**测试覆盖 (TEST)**

- **FR-032**: 前端核心页面(MetricDetail/QueryWorkspace/ConflictCenter/DataGovernance)必须有 happy path 测试
- **FR-033**: 全部 24 个 API 端点文件必须有 happy path + error path 集成测试
- **FR-034**: SEC-01~06 必须有安全回归测试用例
- **FR-035**: 核心基础设施(Redis/Neo4j/ES宕机)必须有混沌测试
- **FR-036**: 核心 API 必须有 P99 延迟基准测试
- **FR-037**: 语义服务异常分支必须有单元测试覆盖
- **FR-038**: 测试数据库必须使用独立事务隔离，测试结束回滚
- **FR-039**: 前端必须有统一 API mock 层覆盖全部 API 函数
- **FR-040**: 必须有端到端测试覆盖核心用户流程（登录→创建指标→发布→查询）

**运营支撑 (OPS)**

- **FR-041**: 优雅关闭必须注册所有连接池(MySQL/Neo4j/ES/Redis)的 close/dispose
- **FR-042**: /health 端点必须检查 Redis/Neo4j/ES 连接状态，降级时返回 degraded (503)
- **FR-043**: 关键配置项(限流阈值/OLAP超时/LLM端点)必须支持运行时热更新
- **FR-044**: CI 必须执行 Alembic 迁移幂等性测试(upgrade→downgrade→upgrade)
- **FR-045**: 必须建立统一降级注册中心，所有降级事件汇聚到 /health/degraded
- **FR-046**: seed 脚本必须支持 ADMIN_INITIAL_PASSWORD 环境变量
- **FR-047**: 必须添加核心业务维度 Prometheus 指标（指标发布数/天、冲突仲裁时长、查询成功率、LLM调用成功率）
- **FR-048**: 审计归档任务必须有容量预警（审计表行数/大小阈值告警）
- **FR-049**: 必须引入特性开关框架支持按域/用户灰度发布

### Key Entities

- **KeyRotation**: 密钥轮换记录（旧密钥ID→新密钥ID、状态、创建时间、完成时间）
- **JwtBlacklist**: JWT 黑名单条目（jti、过期时间、加入时间）
- **TrustedProxy**: 可信反向代理白名单（IP/CIDR、备注）
- **DegradationRegistry**: 降级注册中心条目（组件名、状态、原因、时间戳）
- **FeatureFlag**: 特性开关（名称、启用状态、目标域/用户列表、创建时间）
- **DeadLetterEvent**: 事件总线死信（原始事件、失败原因、重试次数、时间戳）

## Success Criteria

### Measurable Outcomes

- **SC-001**: 综合审查得分从 54.6 提升至 ≥75（GB/T 36073 L3-稳健级）
- **SC-002**: P0 缺陷清零，P1 缺陷修复率 100%
- **SC-003**: 安全合规维度得分从 48 提升至 ≥70
- **SC-004**: 测试覆盖维度得分从 55 提升至 ≥70
- **SC-005**: 前端核心页面测试覆盖从 18.75% 提升至 ≥60%
- **SC-006**: 10 并发登录请求平均响应时间 < 500ms（bcrypt 异步化后）
- **SC-007**: /health 端点在 Redis 不可用时正确返回 degraded 状态
- **SC-008**: 日期参数查询不再被 SQL 注入守卫误拦截
- **SC-009**: 所有 53 项审查缺陷均有修复或明确的 defer 记录

## Assumptions

- 密钥迁移支持新旧密钥共存过渡期，旧密钥不会丢失
- JWT 黑名单在 Redis 不可用时降级为内存黑名单（进程重启后失效，可接受）
- bcrypt 异步化使用 asyncio.to_thread()，不引入新依赖
- Unit of Work 事务编排采用渐进式迁移，不一次性重构全部 API
- E2E 测试框架选择 Playwright（需安装浏览器，CI 环境需支持）
- 特性开关使用 Redis 存储 + 中间件检查，不引入第三方 Feature Flag 服务
- 前端 Token 存储改为 httpOnly cookie 需要 API 层配合设置 Set-Cookie 头
- 密钥轮换 90 天策略为默认值，可通过配置调整

## Open Questions

- [NEEDS CLARIFICATION: 前端 Token 迁移策略] — 完全迁移到 httpOnly cookie 需要改动登录API返回方式（Set-Cookie），还是仅缩短 JWT 有效期至15分钟+refresh token？两种方案改动范围差异大。
- [NEEDS CLARIFICATION: E2E测试范围] — Playwright E2E 测试是否纳入本次整改？还是作为后续独立迭代？E2E 需要完整运行环境（Docker compose 全启动），CI 成本较高。
- [NEEDS CLARIFICATION: Unit of Work 迁移节奏] — 100+ 处 db.commit() 全部迁移至 Service 层是否一次性完成？还是分批渐进（优先 P0/P1 涉及的 API 端点）？
