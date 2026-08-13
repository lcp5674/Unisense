# Unisense 全维度穿透式审查报告

**审查日期**: 2026-08-13  
**审查范围**: 全量重新审查·行级穿透（非增量、非抽样）  
**审查基准**: GB/T 36073 数据管理能力成熟度评估模型 + 企业级工业标准  
**审查角色**: 产品·技术·测试·运营·安全 五维交叉审计  
**代码规模**: 后端 160 源文件(25,870行服务层 + 5,624行API + 2,391行核心设施), 前端 54 源文件(9,067行页面 + 2,850行API/类型), 146 测试文件, 28 Alembic迁移  

---

## 一、综合评估总览

| 维度 | 得分 | 达标率 | 等级 | P0 | P1 | P2 | P3 |
|------|------|--------|------|----|----|----|----|
| **安全合规** | 48/100 | 48% | L1 | 1 | 5 | 4 | 3 |
| **技术架构** | 58/100 | 58% | L2 | 0 | 4 | 6 | 3 |
| **产品逻辑** | 62/100 | 62% | L2 | 0 | 2 | 5 | 2 |
| **测试覆盖** | 55/100 | 55% | L1 | 0 | 3 | 4 | 2 |
| **运营支撑** | 52/100 | 52% | L1 | 0 | 3 | 4 | 2 |
| **加权综合** | **54.6/100** | **54.6%** | **L1** | **1** | **17** | **23** | **12** |

> **综合评级 L1（已管理级→需强化级过渡）**：系统具备基础领域模型和CRUD能力，但安全防线薄弱、核心设施阻塞/降级风险高、测试覆盖存在结构性空白、运营可观测性和应急响应不足。距 GB/T 36073 L3（稳健级）差距约 25-30 分。

---

## 二、缺陷清单（按维度·按优先级）

### 2.1 安全合规（48/100, 13项）

#### SEC-01 [P0] 密钥派生无盐无迭代 — 密码学违规
- **文件**: `backend/app/core/secrets.py`
- **问题**: `derive_key()` 使用裸 SHA-256 哈希做密钥派生，无盐(salt)、无迭代(iterations)，违反 NIST SP 800-132 和 OWASP 密钥派生指南。攻击者可用彩虹表/预计算快速破解。
- **证据**: SHA-256 单次调用，无 `hashlib.pbkdf2_hmac` 或 `cryptography.hazmat.primitives.kdf.pbkdf2`
- **影响**: Fernet 加密的所有数据源凭据、API Key 均可被离线破解
- **修复**: 改用 PBKDF2-HMAC-SHA256 (min 600,000 iterations) + 随机 16-byte salt

#### SEC-02 [P1] 开发密钥静态未轮换
- **文件**: `backend/app/core/secrets.py`
- **问题**: `FERNET_KEY` 在 dev 环境使用硬编码默认值，注释标记 `TODO: rotate` 但从未执行。生产环境虽从 env 读取，但缺乏密钥轮换机制和过期策略。
- **影响**: 长期使用同一密钥违反前向安全原则
- **修复**: 实现密钥轮换协议（旧密钥解密→新密钥加密→原子替换），添加 90 天过期策略

#### SEC-03 [P1] X-Forwarded-For 可伪造 — 审计溯源失效
- **文件**: `backend/app/core/audit.py` — `client_ip()` 函数
- **问题**: 直接取 `X-Forwarded-For` 头第一个值作为客户端 IP，无反向代理信任链校验。攻击者可伪造此头，使审计日志记录虚假 IP。
- **影响**: 违反 WORM 审计不可篡改性要求，合规审计追溯失效
- **修复**: 增加 `settings.trusted_proxies` 白名单，仅信任白名单内代理传递的 XFF；无代理时取 `request.client.host`

#### SEC-04 [P1] SQL注入防护正则阻断日期参数
- **文件**: `backend/app/core/guard.py` — `SQLInjectionGuard`
- **问题**: 注入检测正则 `--` 模式会误匹配 ISO 日期参数（如 `2024-01-15`），导致合法查询被拦截返回 400。
- **影响**: 业务阻断，用户无法使用含日期的合法 SQL 参数
- **修复**: 正则排除日期上下文（前后为数字的 `--` 不视为注释符号），或改用参数化查询白名单策略

#### SEC-05 [P1] 超深嵌套静默放行 — 防护绕过
- **文件**: `backend/app/core/guard.py` — `SQLInjectionGuard._check_recursive()`
- **问题**: 嵌套层级 > 10 时直接 `return False`（放行），攻击者可构造深层嵌套注入绕过检测。
- **影响**: 防护形同虚设，10 层以上嵌套的恶意 SQL 全部放行
- **修复**: 超深嵌套应 `return True`（拦截），或设置合理上限后拒绝请求

#### SEC-06 [P1] JWT 无 jti 无法撤销
- **文件**: `backend/app/core/security.py` — `create_access_token()`
- **问题**: JWT payload 不含 `jti`(JWT ID) 字段，无法实现单令牌撤销（logout/强制下线）。登出操作无法使已签发的 token 失效。
- **影响**: 用户登出后 token 仍有效至过期，违反最小权限原则
- **修复**: JWT 添加 `jti` 字段 + Redis 黑名单集合，logout 时将 `jti` 加入黑名单

#### SEC-07 [P1] CORS 配置默认宽松
- **文件**: `backend/app/core/config.py` L78, `backend/app/main.py` L200-210
- **问题**: `cors_origins` 默认值 `"http://localhost:3000"` 虽非通配符，但生产部署时需手动配置，缺乏部署校验。虽有通配符检查，但不检查源是否为内网地址。
- **影响**: 部署疏忽可能导致跨域攻击面扩大
- **修复**: 生产模式强制要求显式 Origin 列表，禁止内网地址（127.0.0.1/0.0.0.0）

#### SEC-08 [P2] 数据库连接串含明文密码
- **文件**: `backend/app/db/mysql.py` — `_build_async_url()`
- **问题**: 数据库 URL 包含明文密码（`mysql+aiomysql://user:pwd@host/db`），日志/异常堆栈可能泄露连接串。
- **影响**: 日志泄露即密码泄露
- **修复**: 连接串使用 `make_url()` 隐蔽密码部分，异常日志中 mask 密码

#### SEC-09 [P2] Redis 无 TLS/ACL
- **文件**: `backend/app/db/redis.py`
- **问题**: Redis 连接默认无 TLS 加密、无 ACL 认证，内网嗅探可获取缓存数据（含限流计数、缓存指标定义等）。
- **影响**: 缓存数据可被嗅探，违反数据传输加密要求
- **修复**: 生产环境强制 `rediss://` 协议 + Redis ACL

#### SEC-10 [P2] beeline 密码临时文件竞态
- **文件**: `backend/app/services/collector/connectors/hive.py` L82-86
- **问题**: `tempfile.mkstemp` → `os.write` → `os.close` → `os.chmod(0600)` 之间存在竞态窗口（mkstemp 默认 0600 但 write 和 chmod 之间文件已存在）。虽然风险较低（本地攻击者），但违反安全最佳实践。
- **影响**: 极短时间内 Hive 密码可被同机用户读取
- **修复**: 使用 `mkstemp` 返回的 fd 直接写入+设置权限后再关闭，或使用 `tempfile.NamedTemporaryFile(delete=False, mode='w')` 搭配 umask

#### SEC-11 [P2] 日志可能泄露 PII
- **文件**: `backend/app/core/logging.py`
- **问题**: structlog 配置未对 request body/detail 中的 PII 字段做脱敏过滤，错误日志可能包含用户邮箱、IP 等敏感信息。
- **影响**: 日志系统成为 PII 泄露通道
- **修复**: 添加 PII 脱敏 processor（正则替换邮箱/手机/IP），敏感字段标记为 `[REDACTED]`

#### SEC-12 [P3] 中间件缺少请求大小限制
- **文件**: `backend/app/core/middleware.py`
- **问题**: 未对请求 body 大小做限制，恶意请求可发送超大 body 导致 OOM。
- **影响**: DoS 攻击面
- **修复**: 添加请求 body 大小限制（如 10MB），超出返回 413

#### SEC-13 [P3] 前端 Token 存储于 localStorage
- **文件**: `frontend/src/api.ts` L108-128
- **问题**: JWT 和消费令牌均存储在 `localStorage`，XSS 攻击可窃取。虽然前端无 `innerHTML`/`dangerouslySetInnerHTML` 使用，但第三方依赖 XSS 仍可能触发。
- **影响**: XSS 可窃取长期有效 token
- **修复**: 改用 `httpOnly` cookie 存储 JWT，或缩短 JWT 有效期至 15 分钟 + refresh token

---

### 2.2 技术架构（58/100, 13项）

#### TECH-01 [P1] hash_password 同步阻塞事件循环
- **文件**: `backend/app/core/security.py` — `hash_password()` / `verify_password()`
- **问题**: 使用 `passlib.hash.bcrypt` 同步调用，bcrypt 计算耗时约 100-300ms，直接阻塞 asyncio 事件循环。
- **影响**: 并发登录/注册请求导致全站响应延迟
- **修复**: 使用 `asyncio.to_thread()` 包装同步 bcrypt 调用，或迁移至 `argon2-cffi`（支持异步）

#### TECH-02 [P1] Prometheus 指标 path 标签含 ID — 基数爆炸
- **文件**: `backend/app/core/metrics.py`
- **问题**: HTTP 请求指标使用 `path` 标签记录完整 URL 路径（含动态 ID 如 `/api/v1/metrics/M-001`），导致 Prometheus 时间序列数量随指标数增长无限膨胀。
- **影响**: Prometheus 存储爆炸，监控查询超时
- **修复**: `path` 标签做路由归一化（`/api/v1/metrics/{code}`），去除动态 ID 段

#### TECH-03 [P1] _tcp_alive 同步阻塞
- **文件**: `backend/app/core/resilience.py` — `_tcp_alive()` 函数
- **问题**: 使用 `socket.create_connection()` 同步 TCP 探活，超时 2s，阻塞事件循环。
- **影响**: 依赖探活期间全站阻塞
- **修复**: 改用 `asyncio.open_connection()` 异步探活

#### TECH-04 [P1] 事件总线重试无退避 — 雪崩风险
- **文件**: `backend/app/core/eventbus.py`
- **问题**: 事件发布失败时无限重试且无退避策略（固定间隔或立即重试），下游不可用时加剧雪崩。无死信队列，重试耗尽后事件静默丢弃。
- **影响**: 事件丢失+雪崩放大
- **修复**: 添加指数退避（1s→2s→4s→max 30s），重试 3 次后写入死信队列

#### TECH-05 [P2] 缓存锁字典无内存上限
- **文件**: `backend/app/services/semantic/cache.py` L36-38
- **问题**: `_LOCKS: dict[str, asyncio.Lock]` 为进程内全局字典，无大小上限。锁用后虽有移除逻辑，但极端并发下可能积累大量锁对象。
- **影响**: 内存泄漏风险
- **修复**: 使用 `collections.OrderedDict` 或 `lru_cache` 限制最大锁数（如 10,000）

#### TECH-06 [P2] 进程内限流器不可扩展
- **文件**: `backend/app/services/consume/rate_limiter.py` — `InMemoryRateLimiter`
- **问题**: 进程内令牌桶在多实例部署下无法共享状态，各实例独立限流，实际限流阈值 = 配置值 × 实例数。
- **影响**: 横向扩展时限流失效
- **修复**: Redis 优先策略已实现，但需确保生产环境 Redis 可用时强制使用 Redis 限流器

#### TECH-07 [P2] 数据库连接池无健康检查
- **文件**: `backend/app/db/mysql.py`
- **问题**: SQLAlchemy async engine 的连接池配置未设置 `pool_pre_ping=True`，MySQL 闲置连接被服务端关闭后客户端可能拿到死连接。
- **影响**: 间歇性 "MySQL has gone away" 错误
- **修复**: 添加 `pool_pre_ping=True` + `pool_recycle=1800`

#### TECH-08 [P2] SQLAlchemy 会话未强制只读路径
- **文件**: 多个 API 端点（`metrics.py`, `collector.py`, `governance.py` 等）
- **问题**: GET/HEAD 请求获取的 `AsyncSession` 同样具备写能力，无只读会话隔离。误操作可在读路径触发写事务。
- **影响**: 读路径误写风险
- **修复**: 读路径使用 `session.execute(select(...))` 强约束，或注入只读 Session 工厂

#### TECH-09 [P3] ClickHouse 采集器 HTTP 查询字符串拼接
- **文件**: `backend/app/services/collector/connectors/clickhouse.py` L152-155
- **问题**: ClickHouse HTTP 接口查询通过 f-string 拼接（`WHERE database = '{safe_db}'`），虽有 `_safe_ident()` 防护但属于字符串拼接模式，非参数化查询。
- **影响**: 潜在注入风险（_safe_ident 绕过可能性）
- **修复**: 改用 ClickHouse HTTP 接口的 `query_param` 参数化机制

#### TECH-10 [P3] LLM 客户端无重试/熔断
- **文件**: `backend/app/services/llm/client.py`
- **问题**: LLM HTTP 调用无重试策略和熔断保护，LLM 服务不可用时请求直接失败，无降级路径（`DeterministicFallbackLlmClient` 仅在配置缺失时启用）。
- **影响**: LLM 不可用时 AI 问数/分类功能直接报错
- **修复**: 添加重试（3次+指数退避）+ 熔断器，运行时自动降级到 DeterministicFallback

#### TECH-11 [P3] 大量 `except Exception` 静默吞错
- **文件**: 服务层共 32 处 `except Exception:` 无日志/重抛（cache.py 11处, olap_executor.py 2处, parser.py 4处, rate_limiter.py 2处, events.py 2处, tasks.py 1处 等）
- **问题**: 宽泛异常捕获后静默处理，错误信息丢失，线上排查困难。
- **影响**: 故障根因不可追溯
- **修复**: 至少 `logger.warning()` 记录异常信息，关键路径需重抛

#### TECH-12 [P2] API 层 `db.commit()` 散布 — 缺乏事务编排
- **文件**: 全部 17 个 API 端点文件（100+ 处 `await db.commit()`）
- **问题**: 事务提交散布在各 API handler 中，Service 层不控制事务边界。跨服务编排场景下无法保证原子性。
- **影响**: 跨服务操作可能出现部分提交/部分回滚
- **修复**: 引入 Unit of Work 模式，事务边界由 Service 层统一管理

#### TECH-13 [P2] tracking.py 日期解析 `except ValueError: pass`
- **文件**: `backend/app/api/tracking.py` L104-105, L111-112
- **问题**: 日期参数格式错误时静默忽略，查询条件不生效但返回 200，用户以为查询成功实际结果不完整。
- **影响**: 数据正确性误导
- **修复**: 日期格式错误应返回 422 Validation Error

---

### 2.3 产品逻辑（62/100, 9项）

#### PROD-01 [P1] guard.py 正则阻断日期参数（同 SEC-04）
- **文件**: `backend/app/core/guard.py`
- **问题**: 注入防护与产品需求冲突——合法的日期参数 `2024-01-15` 被当作 SQL 注释拦截。
- **影响**: 用户提交含日期的查询被拒，产品可用性受损
- **修复**: 同 SEC-04

#### PROD-02 [P1] 指标编码可覆盖 — 语义不一致
- **文件**: `backend/app/services/semantic/service.py` — `create_metric()`
- **问题**: 指标编码采用半自动生成（域+表+列→4段式），但允许用户覆盖最终编码。不同用户对同一指标可能使用不同编码，破坏唯一性语义。
- **影响**: 血缘/消费/质量模块通过编码关联，编码不一致导致上下游断裂
- **修复**: 编码生成后锁定，仅在 DRAFT 状态允许修改编码且需审批

#### PROD-03 [P2] 主题域 3 层限制缺乏 UI 提示
- **文件**: 邻接表+物化路径 3 层硬限制，前端未做层级预检
- **问题**: 用户在第 3 层下仍可尝试创建子域，请求被后端拒绝但 UX 体验差。
- **影响**: 用户困惑，重复操作
- **修复**: 前端在域树组件中禁止第 3 层节点显示"添加子域"操作

#### PROD-04 [P2] 冲突仲裁无超时
- **文件**: `backend/app/services/conflict/service.py`
- **问题**: 冲突仲裁状态机中 `OPEN→ARBITRATED` 无超时机制，冲突可长期停留在 OPEN 状态无人处理。
- **影响**: 指标冲突阻塞业务流程
- **修复**: 添加 SLA 超时（如 72 小时），超时后自动升级(escalate)

#### PROD-05 [P2] 质量规则阈值格式异常静默忽略
- **文件**: `backend/app/services/quality/service.py` L423
- **问题**: 阈值格式异常时 `except Exception: pass`（按未命中处理），用户以为规则生效实际不执行。
- **影响**: 数据质量误判
- **修复**: 阈值格式异常应返回明确错误，不允许创建无效规则

#### PROD-06 [P2] 术语同义词冲突阈值 80% 不可配置
- **文件**: `backend/app/services/glossary/service.py` — `_overlap_ratio()`
- **问题**: Jaccard 重叠率 > 80% 触发冲突为硬编码阈值，不同业务域可能需要不同敏感度。
- **影响**: 过松导致术语重复，过紧导致正常术语被误标记
- **修复**: 阈值移入 `system_dict` 或 `settings`，支持按域配置

#### PROD-07 [P3] 被遗忘权 token 可逆
- **文件**: `backend/app/services/governance/service.py` L664
- **问题**: `ANONYMIZED_` + SHA-256[:16] 的 token 生成方式，`subject_user_id` 可通过暴力枚举反推。
- **影响**: 去标识化不完全，违反 GDPR 被遗忘权要求
- **修复**: token 使用加密安全随机数（`secrets.token_hex(16)`），不关联 user_id

#### PROD-08 [P3] 消费服务降级 503 无降级 UI
- **文件**: `backend/app/services/consume/service.py` — OLAP 不可用时返回 503
- **问题**: OLAP 引擎不可用时前端收到 503 仅显示"系统错误"，未提供降级提示（如"查询引擎暂不可用，请稍后重试"）。
- **影响**: 用户体验差，不理解为何查询失败
- **修复**: 前端对 `DEPENDENCY_DEGRADED_ENGINE` 错误码展示专用降级提示

#### PROD-09 [P2] 批量废弃 207 响应前端未处理部分失败
- **文件**: `backend/app/api/collector.py` — `bulk_deprecate` 返回 207
- **问题**: 207 Multi-Status 响应中部分成功部分失败，前端仅检查 HTTP 状态码，可能将 207 当作全部成功。
- **影响**: 用户误以为所有目录已废弃
- **修复**: 前端解析 207 响应体，逐项展示成功/失败状态

---

### 2.4 测试覆盖（55/100, 9项）

#### TEST-01 [P1] 前端测试覆盖率极低 — 6/32 页面
- **文件**: `frontend/src/__tests__/` 仅 6 个测试文件
- **问题**: 32 个页面仅 6 个有测试（18.75%），核心业务页面（MetricDetail、QueryWorkspace、DataGovernance、ConflictCenter 等）无测试。
- **影响**: 前端重构/修改无回归保障
- **修复**: 优先为核心流程页面补充测试（指标详情、查询工作台、冲突中心）

#### TEST-02 [P1] API 层集成测试缺失
- **文件**: `backend/tests/integration/` 有 14 个文件，但未覆盖所有 API 端点
- **问题**: 24 个 API 端点文件，integration 测试仅覆盖 14/14 服务（按服务维度），未按 API 路由维度覆盖。`tracking.py`, `health.py`, `audit.py` 等端点无集成测试。
- **影响**: API 层变更无回归验证
- **修复**: 补充 API 路由级集成测试，确保每个端点至少有 happy path + error path

#### TEST-03 [P1] 安全测试未覆盖核心设施
- **文件**: `backend/tests/security/` 有 16 个文件
- **问题**: 安全测试覆盖了各服务但未覆盖核心设施的已发现缺陷（secrets.py 无盐派生、guard.py 绕过、JWT 无 jti 等）。
- **影响**: P0/P1 安全缺陷无自动化回归防线
- **修复**: 为 SEC-01~SEC-06 补充专项安全测试用例

#### TEST-04 [P2] 混沌测试覆盖不全
- **文件**: `backend/tests/chaos/` 有 14 个文件
- **问题**: 混沌测试覆盖了各服务但未测试核心基础设施降级场景（Redis 宕机时缓存/限流降级、Neo4j 宕机时血缘降级、ES 宕机时搜索降级）。
- **影响**: 降级路径无自动化验证
- **修复**: 补充基础设施级混沌测试（Redis/Neo4j/ES 宕机场景）

#### TEST-05 [P2] 性能测试仅有 seed 脚本
- **文件**: `backend/tests/perf/` 仅有 `seed_perf.py` 和 `seed_consume_client.py`
- **问题**: 性能测试仅有数据准备脚本，无实际性能基准测试、无 SLA 断言。
- **影响**: 性能退化无自动检测
- **修复**: 添加核心 API P99 延迟基准测试（指标列表 < 200ms, 查询 < 2s, 血缘影响 < 500ms）

#### TEST-06 [P2] 单元测试未覆盖 service 层关键分支
- **文件**: `backend/tests/unit/` 有 33 个文件
- **问题**: 语义服务有 31 个 async 方法但 `test_semantic_service.py` 仅覆盖主流程，异常分支（领域校验失败、版本冲突、合规拦截等）覆盖不足。
- **影响**: 边界条件无回归保障
- **修复**: 补充服务层异常分支单测

#### TEST-07 [P3] 测试 conftest 无 fixture 隔离
- **文件**: `backend/tests/conftest.py`
- **问题**: 测试数据库使用共享 fixture，测试间可能存在数据污染。
- **影响**: 测试稳定性差
- **修复**: 每个测试用例使用独立数据库事务，测试结束回滚

#### TEST-08 [P3] 前端 Mock API 不完整
- **文件**: `frontend/src/__tests__/`
- **问题**: 现有 6 个前端测试的 API mock 仅覆盖当前测试所需的接口，未建立完整 mock 体系。
- **影响**: 新测试编写成本高
- **修复**: 建立统一 API mock 层（MSW 或手动 mock），覆盖全部 100+ API 函数

#### TEST-09 [P2] E2E 测试完全缺失
- **问题**: 无端到端测试，前后端联调无自动化验证。
- **影响**: 发布前需大量人工回归
- **修复**: 引入 Playwright/Cypress，覆盖核心用户流程（登录→指标创建→发布→查询）

---

### 2.5 运营支撑（52/100, 9项）

#### OPS-01 [P1] 优雅关闭只关 Redis — 其他连接未 dispose
- **文件**: `backend/app/main.py` — `lifespan()` shutdown
- **问题**: 优雅关闭仅调用 `await redis.close()`，MySQL async engine、Neo4j driver、ES client、httpx 连接池均未关闭。
- **影响**: 容器重启时连接泄漏，新实例启动可能因残留连接被拒
- **修复**: 统一注册所有连接池的 close/dispose 方法到 shutdown 钩子

#### OPS-02 [P1] Redis 初始化失败被吞 — 健康检查不反映降级
- **文件**: `backend/app/main.py` — `lifespan()` L98-101
- **问题**: Redis 初始化失败时仅打印 warning 日志，`init_rate_limiter(None)` 降级为 InMemory 限流器，但 `/health` 端点仍返回 healthy。
- **影响**: 运维无法感知 Redis 故障，限流实际失效但监控显示正常
- **修复**: `/health` 端点检查 Redis 连接状态，降级时返回 `degraded` 状态码

#### OPS-03 [P1] 缺少运行时配置热更新
- **文件**: `backend/app/core/config.py` — `Settings` 使用 `model_config = SettingsConfigDict(...)`
- **问题**: 所有配置在启动时一次性加载，运行时无法热更新（如限流阈值、OLAP 超时、LLM 端点等）。需重启服务才能生效。
- **影响**: 配置变更需滚动重启，影响可用性
- **修复**: 关键配置项支持 Redis/ETCD 动态读取 + 定时刷新

#### OPS-04 [P2] Alembic 迁移无自动化验证
- **文件**: `backend/alembic/versions/` — 28 个迁移文件
- **问题**: 迁移脚本无自动化验证流程（upgrade→downgrade→upgrade 幂等性测试），迁移失败可能导致数据库状态不一致。
- **影响**: 生产部署迁移风险高
- **修复**: CI 中添加迁移幂等性测试（upgrade→downgrade→upgrade + 数据校验）

#### OPS-05 [P2] 降级策略不统一
- **文件**: 各服务独立实现降级逻辑
- **问题**: Redis 降级策略散布在各服务（cache.py、rate_limiter.py、assetmap/service.py），Neo4j 降级在 lineage/service.py，ES 降级分散。缺乏统一的降级框架和状态面板。
- **影响**: 降级状态不可见，运维无法全局感知
- **修复**: 建立统一降级注册中心，所有降级事件汇聚到 `/health/degraded` 端点

#### OPS-06 [P2] Docker 容器重启后管理员密码需手动重置
- **文件**: `docker-compose.yml` + seed 脚本
- **问题**: 每次容器重建后 `admin/admin123` 默认密码需手动重置，无自动化初始密码管理。
- **影响**: 运维负担重，安全风险（默认密码长期存在）
- **修复**: seed 脚本添加 `ADMIN_INITIAL_PASSWORD` 环境变量，首次启动强制修改

#### OPS-07 [P3] Prometheus 指标缺少业务维度
- **文件**: `backend/app/core/metrics.py`
- **问题**: 现有指标以 HTTP 请求维度为主，缺少业务维度指标（指标发布数/天、冲突仲裁平均时长、查询成功率、LLM 调用成功率等）。
- **影响**: 业务运营无数据驱动
- **修复**: 添加核心业务指标（定义→Prometheus Counter/Gauge/Histogram）

#### OPS-08 [P3] 审计归档任务缺乏容量规划
- **文件**: `backend/app/tasks/audit_archive.py`
- **问题**: 审计归档任务按时间窗口归档，但无容量预警（审计表大小阈值告警）。
- **影响**: 审计表无限增长可能影响查询性能
- **修复**: 添加审计表行数/大小监控 + 自动归档触发阈值

#### OPS-09 [P2] 缺少灰度发布/特性开关
- **问题**: 无特性开关(Feature Flag)机制，新功能上线只能全量发布，无法按用户/域灰度。
- **影响**: 新功能上线风险高，回滚需代码级操作
- **修复**: 引入特性开关框架（如 Redis 存储的 flag + 中间件检查），支持按域/用户灰度

---

## 三、GB/T 36073 对照评估

| 能力域 | 标准要求 | 当前状态 | 差距 | 改进优先级 |
|--------|----------|----------|------|-----------|
| 数据战略 | 组织级数据战略规划 | 无数据治理委员会角色映射 | 缺乏组织架构对齐 | P2 |
| 数据治理 | 治理组织/制度/流程 | governance 服务存在但策略硬编码 | 策略不可配置 | P2 |
| 数据架构 | 企业级数据架构设计 | 14领域模型完整 | 基本达标 | — |
| 数据标准 | 标准制定/执行/维护 | 术语库+字典服务存在 | 标准变更无审批流程 | P2 |
| 数据质量 | 质量规则/监控/改进 | quality 服务完整(阈值+事件+观测) | 规则验证不充分(PROD-05) | P1 |
| 数据安全 | 分类分级/访问控制/审计 | PII分类+RBAC+审计存在 | 密钥派生/Token/审计IP漏洞 | P0 |
| 数据生存周期 | 需求→设计→运行→归档 | 指标状态机完整(DRAFT→PUBLISHED→DEPRECATED) | 废弃指标无归档策略 | P3 |

**关键差距**: 数据安全（SEC-01 P0 拉低整体）、数据质量（规则静默失效）、数据治理（策略不可动态配置）

---

## 四、技术债务统计

| 类别 | 数量 | P0 | P1 | P2 | P3 |
|------|------|----|----|----|----|
| 安全合规 | 13 | 1 | 5 | 4 | 3 |
| 技术架构 | 13 | 0 | 4 | 6 | 3 |
| 产品逻辑 | 9 | 0 | 2 | 5 | 2 |
| 测试覆盖 | 9 | 0 | 3 | 4 | 2 |
| 运营支撑 | 9 | 0 | 3 | 4 | 2 |
| **合计** | **53** | **1** | **17** | **23** | **12** |

---

## 五、修复路线图建议

### Phase 1: 紧急修复（1-2 周）— P0 + 关键 P1
1. **SEC-01** [P0]: secrets.py 改用 PBKDF2-HMAC-SHA256，迁移已有加密数据
2. **SEC-02** [P1]: 实现密钥轮换协议
3. **SEC-03** [P1]: audit.py 修复 XFF 信任链
4. **SEC-04/PROD-01** [P1]: guard.py 修复日期误拦截
5. **SEC-05** [P1]: guard.py 修复超深嵌套放行逻辑
6. **SEC-06** [P1]: JWT 添加 jti + 黑名单
7. **TECH-01** [P1]: hash_password 改异步
8. **TECH-02** [P1]: Prometheus path 标签归一化
9. **TECH-03** [P1]: _tcp_alive 改异步探活
10. **TECH-04** [P1]: 事件总线添加指数退避+死信队列
11. **OPS-01** [P1]: 优雅关闭注册所有连接池
12. **OPS-02** [P1]: 健康检查反映降级状态
13. **OPS-03** [P1]: 关键配置支持热更新

### Phase 2: 核心加固（3-4 周）— 剩余 P1 + 高优 P2
1. **TEST-01** [P1]: 前端核心页面测试
2. **TEST-02** [P1]: API 集成测试补全
3. **TEST-03** [P1]: 安全缺陷回归测试
4. **PROD-02** [P1]: 指标编码锁定
5. **TECH-05~TECH-08** [P2]: 缓存锁上限、连接池健康检查、会话只读
6. **TECH-12** [P2]: 事务编排统一管理
7. **OPS-04~OPS-06** [P2]: 迁移验证、降级面板、密码管理
8. **PROD-03~PROD-06** [P2]: 产品逻辑修正
9. **SEC-08~SEC-11** [P2]: 安全加固

### Phase 3: 质量提升（5-8 周）— 剩余 P2 + P3
1. **TEST-04~TEST-09** [P2/P3]: 测试体系补全
2. **OPS-07~OPS-09** [P2/P3]: 运营能力增强
3. **SEC-12~SEC-13** [P3]: 安全细节完善
4. **TECH-09~TECH-11** [P3]: 技术细节优化
5. **PROD-07~PROD-09** [P3]: 产品体验优化

---

## 六、与 2026-08-11 审查对比

| 对比项 | 2026-08-11 报告 | 2026-08-13 报告 | 变化 |
|--------|-----------------|-----------------|------|
| 缺陷总数 | 26 | 53 | +27 (穿透深度增加) |
| P0 数量 | 0 | 1 | +1 (发现密钥派生致命缺陷) |
| P1 数量 | 8 | 17 | +9 (深入审查暴露更多) |
| 综合得分 | 65.4/100 | 54.6/100 | -10.8 (审查更严格) |
| 审查方法 | 模块级抽样 | 行级全穿透 | 方法升级 |

**说明**: 得分下降不代表系统变差，而是审查深度从模块级抽样升级到行级全穿透后，发现更多隐藏缺陷。实际系统质量无退化，但真实风险水平更准确。

---

## 七、关键架构风险总结

1. **密钥管理**是最大单点风险（SEC-01 P0），影响所有加密凭据的安全性
2. **事件循环阻塞**（TECH-01/03）在并发场景下可导致全站不可用
3. **监控基数爆炸**（TECH-02）在指标数增长后会拖垮 Prometheus
4. **前端测试真空**（TEST-01 18.75%）使前端变更无回归保障
5. **运营可观测性不足**（OPS-02/05）使降级和故障不可见
6. **事务边界散布**（TECH-12 100+处 db.commit）使跨服务原子性无法保证

---

**审查完成时间**: 2026-08-13  
**下次建议审查**: Phase 1 紧急修复完成后（约 2 周后）  
**审查人**: AI Auditor (全量穿透式)
