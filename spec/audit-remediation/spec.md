# Feature Specification: Unisense 全维度审查整改

**Created**: 2026-08-11  
**Status**: Draft  
**Input**: 基于 `spec/full-audit-report.md` 审查报告的全部26项问题，覆盖P0-P3四个优先级，前后端同步整改

## Overview

Unisense 指标语义中台经全维度穿透式审查，识别出26项需整改问题，综合加权得分65.4/100（L2-受管理级），距工业级标准（L3-稳健级≥75分）差9.6分。本次整改目标：修复全部26项问题，将综合得分提升至≥75分，达到GB/T 36073 L3-稳健级标准。整改范围覆盖后端安全缺陷、空壳实现填充、基础设施加固、前端功能补全与测试体系建设。

## User Scenarios & Testing

### User Story 1 - 安全工程师修复SQL注入与密钥缺陷 (Priority: P0)

安全工程师需要确保所有API端点不受SQL注入攻击，JWT/Fernet密钥在生产环境有强度校验，AI服务生成的SQL不包含注入向量。

**Why this priority**: SQL注入和弱密钥是等保2.0一票否决项，直接阻断生产上线。

**Independent Test**: 可通过渗透测试脚本验证：1) 嵌套JSON body注入被守卫拦截；2) AI服务关键词分支生成参数化SQL；3) jwt_secret<32字符时生产模式拒绝启动。

**Acceptance Scenarios**:

1. **Given** 攻击者发送嵌套JSON body `{"data": {"name": "'; DROP TABLE metrics; --"}}`, **When** 请求到达任意POST端点, **Then** guard.py递归扫描拦截并返回422
2. **Given** AI服务收到NL2SQL请求命中关键词匹配分支, **When** 生成SQL, **Then** SQL使用参数化占位符而非f-string拼接
3. **Given** 生产环境JWT_SECRET设置为短于32字符的值, **When** 应用启动, **Then** 配置校验拒绝启动并输出明确错误
4. **Given** 生产环境未配置UNISENSE_FERNET_KEY, **When** 应用启动, **Then** 配置校验拒绝降级到JWT密钥派生模式

---

### User Story 2 - 数据分析师获得真实查询结果 (Priority: P0)

数据分析师通过Semantic API或NL2SQL提交查询时，期望获得真实的OLAP查询结果而非硬编码空数据。

**Why this priority**: 执行引擎空壳是产品核心功能缺失，消费方完全无法使用。

**Independent Test**: 可通过POST /api/v1/consume/query发送真实指标查询，验证返回非空rows且SQL已在Doris执行。

**Acceptance Scenarios**:

1. **Given** 已注册指标关联Doris表, **When** 数据分析师提交consume查询请求, **Then** 系统生成SQL下推至Doris执行并返回真实结果集
2. **Given** 已注册指标, **When** NL2SQL请求设置execute=true, **Then** 系统将生成的SQL委托至consume执行引擎并返回执行结果
3. **Given** Doris不可用(连接超时), **When** 查询请求到达, **Then** 返回503+明确降级提示，熔断器记录失败

---

### User Story 3 - 平台运维水平扩展不限流失效 (Priority: P0)

平台运维人员将服务扩展为多实例部署时，API限流策略仍应按全局配额生效，而非因进程内实现导致限流翻倍。

**Why this priority**: 生产环境必须支持水平扩展，进程内限流是架构级阻断项。

**Independent Test**: 启动2个服务实例，以2倍单实例限额的QPS发送请求，验证全局限流生效（部分请求被429拒绝）。

**Acceptance Scenarios**:

1. **Given** 限流配额为100 QPS, **When** 2个服务实例各发送60 QPS(共120 QPS), **Then** 全局统计约20个请求被429拒绝
2. **Given** Redis不可用, **When** 限流器尝试获取配额, **Then** 降级为本地限流并记录告警日志

---

### User Story 4 - 前端用户获得完整产品体验 (Priority: P0)

前端用户（数据分析师、指标Owner）期望看到QuickBI嵌入报表、治理驾驶舱、消费指南等完整功能，而非半成品界面。

**Why this priority**: 前端是用户直接接触层，缺失功能等同于产品不可用。

**Independent Test**: 可通过E2E测试验证：1) QuickBI iframe加载且SSO穿透；2) 驾驶舱展示全局健康度；3) 消费指南Tab可点击。

**Acceptance Scenarios**:

1. **Given** 数据分析师登录平台, **When** 进入指标详情页, **Then** QuickBI报表iframe正常加载并展示数据可视化
2. **Given** 治理委员会成员登录, **When** 进入驾驶舱页面, **Then** 展示全局健康度、冲突趋势、审核时效等聚合指标
3. **Given** 新用户进入指标详情, **When** 点击消费指南Tab, **Then** 展示指标口径、计算逻辑、使用示例

---

### User Story 5 - 通知真实触达责任人 (Priority: P1)

审核人/指标Owner在质量异常或审核待办时，收到钉钉/邮件通知而非仅数据库入库。

**Why this priority**: 通知仅入库导致闭环断裂，用户无感知，审核时效无法保障。

**Independent Test**: 触发质量异常事件，验证钉钉机器人/邮箱收到通知。

**Acceptance Scenarios**:

1. **Given** 质量检测发现异常, **When** 异常事件写入数据库, **Then** 责任人钉钉/邮箱收到通知
2. **Given** 指标提交审核, **When** 审核待办创建, **Then** 审核人收到通知

---

### User Story 6 - DB会话提交一致性保障 (Priority: P1)

开发人员修改代码时，不再担心get_db_session自动commit与API层手动commit冲突导致数据不一致。

**Why this priority**: 双重提交是隐性数据一致性风险，可能导致审计丢失或业务状态错误。

**Independent Test**: 构造审计写入+业务commit场景，验证审计和业务在同一事务中原子提交。

**Acceptance Scenarios**:

1. **Given** API端点执行write_audit + 业务更新, **When** commit成功, **Then** 审计记录和业务数据同时持久化
2. **Given** API端点执行write_audit + 业务更新, **When** commit前抛异常, **Then** 审计记录和业务数据均不持久化（回滚）

---

### User Story 7 - 采集任务生产可靠 (Priority: P1)

数据开发人员触发采集任务后，即使服务重启任务也不丢失，且生产环境使用持久化队列。

**Why this priority**: 内存队列在生产环境不可接受，服务重启即丢失全量任务。

**Independent Test**: 触发采集任务后重启服务，验证任务恢复执行。

**Acceptance Scenarios**:

1. **Given** 采集任务在队列中, **When** 服务重启, **Then** 队列中任务不丢失，重启后继续执行
2. **Given** 生产环境配置, **When** 应用启动, **Then** 强制使用ArqCollectionQueue而非InMemory实现

---

### User Story 8 - CORS与ES安全加固 (Priority: P1)

安全工程师需要确保生产环境CORS严格限制Origin白名单，Elasticsearch启用认证和TLS。

**Why this priority**: 宽松CORS+ES无认证是等保2.0通信安全不达标项。

**Independent Test**: 1) 非白名单Origin请求被CORS拒绝；2) ES无认证连接被拒绝。

**Acceptance Scenarios**:

1. **Given** 生产环境CORS配置为白名单域名, **When** 非白名单Origin发起请求, **Then** 浏览器CORS策略拒绝
2. **Given** ES启用xpack.security, **When** 无认证客户端连接ES, **Then** 连接被拒绝

---

### User Story 9 - 埋点体系支撑运营度量 (Priority: P1)

运营人员需要看到平台日活、搜索量、审核时效等北极星指标数据，而非凭感觉判断平台健康度。

**Why this priority**: PRD §4.10要求运营度量，当前完全缺失，无法验证SLA和北极星指标。

**Independent Test**: 用户执行搜索操作后，验证埋点事件写入分析存储。

**Acceptance Scenarios**:

1. **Given** 用户在平台执行搜索, **When** 搜索完成, **Then** 埋点事件记录搜索关键词、耗时、结果数
2. **Given** 运营人员查看驾驶舱, **When** 请求日活/审核时效指标, **Then** 返回基于埋点聚合的真实数据

---

### User Story 10 - consume性能基线达标 (Priority: P1)

数据分析师使用Semantic API时，P95延迟需≤300ms而非当前1.73s。

**Why this priority**: 性能超标5.8倍，严重影响用户体验和SLA承诺。

**Independent Test**: k6压测consume核心端点，P95≤300ms。

**Acceptance Scenarios**:

1. **Given** 独立压测环境(Doris+MySQL+Redis), **When** k6以100 VU压测/consume/query, **Then** P95延迟≤300ms

---

### User Story 11 - 资产地图与推荐增强 (Priority: P2)

治理委员会成员在资产地图中看到交互式图谱和敏感分布热力图，数据分析师获得基于行为的个性化推荐。

**Why this priority**: 影响用户体验和PRD FR-18/FR-19完整度，但不阻断核心功能。

**Independent Test**: 1) 资产地图API返回图谱节点/边数据；2) 推荐API基于用户行为返回个性化结果。

**Acceptance Scenarios**:

1. **Given** 治理委员会成员访问资产地图, **When** 请求图谱数据, **Then** 返回含节点(指标/表)、边(血缘)、属性(PII标记)的图结构数据
2. **Given** 数据分析师使用推荐功能, **When** 请求个性化推荐, **Then** 推荐结果基于用户历史查询/收藏行为

---

### User Story 12 - LLM解析结构化输出 (Priority: P2)

数据开发人员采集元数据后，LLM解析结果包含置信度、推断依据，可按置信度分级分流。

**Why this priority**: 影响采集质量，但现有基础解析仍可工作。

**Independent Test**: 触发采集后验证LLM返回包含confidence字段且值在[0,1]区间。

**Acceptance Scenarios**:

1. **Given** 采集触发LLM解析, **When** 解析完成, **Then** 返回结构化结果含confidence + reasoning字段
2. **Given** confidence<0.7, **When** 解析结果入库, **Then** 标记为"待人工确认"而非自动采纳

---

### User Story 13 - 审计归档与PII血缘传播 (Priority: P2)

合规官需要审计日志3年可查且已归档，PII标记沿血缘下游自动传播。

**Why this priority**: 等保2.0和PIPL合规要求，但不阻断核心业务。

**Independent Test**: 1) 审计日志超30天自动归档至对象存储；2) PII字段下游指标自动继承PII标记。

**Acceptance Scenarios**:

1. **Given** 审计日志超过30天, **When** 归档任务执行, **Then** 日志迁移至S3/MinIO并从MySQL清理
2. **Given** 含PII字段的上游指标, **When** 下游派生指标创建, **Then** 自动继承PII标记

---

### User Story 14 - 代码架构统一抽象 (Priority: P3)

开发团队维护代码时，EventBus、BaseService、配置校验等统一抽象减少重复代码。

**Why this priority**: 技术债清理，提升可维护性，但不影响功能。

**Independent Test**: 1) _safe_publish统一为EventBus.publish；2) 新服务继承BaseService协议。

**Acceptance Scenarios**:

1. **Given** 事件发布场景, **When** 服务调用EventBus.publish, **Then** 事件统一路由至订阅者
2. **Given** 新服务开发, **When** 继承BaseService, **Then** 自动获得审计/事件/配置注入能力

---

### Edge Cases

- 当Redis宕机时限流器如何降级？→ 本地限流+告警日志
- 当Doris连接池耗尽时查询如何处理？→ 熔断器打开+503降级响应
- 当Fernet密钥轮换时已加密数据如何解密？→ 支持多密钥解密（旧密钥保留于密钥环）
- 前端QuickBI嵌入时SSO Token过期如何刷新？→ iframe postMessage监听+静默刷新
- 嵌套JSON深度攻击（递归扫描时栈溢出）？→ 限制最大递归深度10层

## Requirements

### Functional Requirements

**P0级（阻断交付）**:

- **FR-001**: SQL注入守卫MUST递归扫描JSON body中所有层级的字符串值（dict嵌套+list嵌套），最大递归深度10层
- **FR-002**: AI服务NL2SQL关键词匹配分支MUST使用参数化查询模板，禁止f-string拼接用户输入到SQL
- **FR-003**: 生产环境（UNISENSE_ENV=prod）MUST校验JWT_SECRET长度≥32字符，拒绝弱密钥启动
- **FR-004**: 生产环境MUST校验UNISENSE_FERNET_KEY独立配置，禁止从JWT_SECRET派生降级
- **FR-005**: consume服务execute_queryMUST实现真实OLAP执行引擎，通过Apache Doris HTTP API执行SQL并返回结果集
- **FR-006**: NL2SQL execute=trueMUST委托consume执行引擎实际执行SQL，返回真实执行结果
- **FR-007**: 限流器MUST使用Redis滑动窗口实现，支持多实例全局配额，Redis不可用时降级为本地限流
- **FR-008**: 前端MUST实现QuickBI嵌入（iframe SSO穿透）、治理驾驶舱（全局健康度+冲突趋势+审核时效聚合）、消费指南Tab
- **FR-009**: 前端MUST补充E2E测试覆盖核心页面渲染和关键交互流程

**P1级（影响SLA）**:

- **FR-010**: notify服务MUST实现真实通知投递通道（钉钉Webhook+邮件SMTP），事件写入DB后触发通知发送
- **FR-011**: get_db_sessionMUST移除yield后自动commit，改为仅异常时rollback，由API层统一控制commit时机
- **FR-012**: 生产环境MUST强制使用ArqCollectionQueue（Redis持久化队列），InMemoryCollectionQueue仅限测试环境
- **FR-013**: consume性能基线MUST达到P95≤300ms（独立压测环境验证）
- **FR-014**: 生产环境CORS MUST严格限制Origin白名单（UNISENSE_CORS_ORIGINS环境变量），禁止通配符+凭证组合
- **FR-015**: Elasticsearch生产环境MUST启用xpack.security（认证+TLS），docker-compose提供安全配置模板
- **FR-016**: 埋点体系MUST采集用户行为事件（搜索/查询/审核/浏览），写入独立分析存储，支撑驾驶舱和推荐算法

**P2级（体验与合规）**:

- **FR-017**: 资产地图MUST新增图谱数据端点（返回节点/边/属性的图结构）、敏感分布热力图端点、责任人视图端点
- **FR-018**: 推荐服务MUST引入基于用户行为（查询/收藏/浏览历史）的协同过滤推荐算法
- **FR-019**: 模板包CRUD端点MUST实现（创建/列表/详情/删除），驾驶舱聚合API MUST实现，消费指南Tab API MUST实现
- **FR-020**: LLM解析MUST返回结构化输出（Pydantic Schema），含confidence(0-1)+reasoning字段，confidence<0.7标记"待人工确认"
- **FR-021**: NPS采集MUST实现（前端嵌入评分组件+后端存储），反馈采纳状态MUST可追踪
- **FR-022**: 审计日志MUST实现冷热分离归档策略（MySQL热数据30天→S3/MinIO冷归档3年）
- **FR-023**: datetime.utcnow()MUST统一替换为datetime.now(UTC)
- **FR-024**: SQL注入守卫MUST支持递归扫描嵌套结构（与FR-001合并，此处标注P2补充：list内dict嵌套场景）

**P3级（技术债）**:

- **FR-025**: 服务层MUST抽象BaseService Protocol（统一审计/事件/配置注入能力）
- **FR-026**: _safe_publishMUST统一为EventBus.publish抽象，消除7+处重复实现
- **FR-027**: Redis客户端MUST纳入lifespan管理，应用关闭时释放连接池
- **FR-028**: 配置类MUST添加生产环境校验钩子（env=prod时强制jwt_secret≥32字符+独立Fernet密钥）
- **FR-029**: PII标记MUST沿血缘下游自动传播，下游派生指标继承上游PII标记

### Key Entities

- **OLAP执行引擎**: SQL生成→Doris HTTP API提交→结果集解析→分页返回，含连接池管理+熔断保护
- **Redis限流器**: 滑动窗口算法，全局配额+接入方级配额双维度，Redis不可用降级本地限流
- **通知通道**: 钉钉Webhook（异步HTTP POST）+ SMTP邮件，通知模板可配置
- **埋点事件**: 用户行为事件（search/query/approve/browse），含时间戳+用户ID+目标ID+上下文
- **图数据结构**: 节点（metric/table/column）+ 边（lineage_edge）+ 属性（pii_flag/owner/domain）
- **结构化LLM输出**: confidence(float 0-1) + reasoning(str) + candidates(list) + needs_human_review(bool)
- **审计归档**: 热存储(MySQL, 30天) → 冷存储(S3/MinIO, 3年)，归档任务定时执行

## Success Criteria

### Measurable Outcomes

- **SC-001**: 全部26项审查问题修复完成，综合加权得分从65.4提升至≥75（GB/T 36073 L3-稳健级）
- **SC-002**: SQL注入渗透测试通过率100%（含嵌套JSON/编码绕过/注释注入等变体）
- **SC-003**: consume查询P95延迟≤300ms（独立Doris压测环境验证）
- **SC-004**: NL2SQL execute=true返回真实执行结果，非空占位
- **SC-005**: 多实例部署时限流全局配额误差≤5%（2实例各60QPS/100QPS上限，拒绝约20请求）
- **SC-006**: 前端E2E测试覆盖核心页面≥5条用户路径
- **SC-007**: 通知投递成功率≥99%（钉钉+邮件双通道）
- **SC-008**: 审计+业务原子提交，零双重提交风险
- **SC-009**: 采集任务服务重启后零丢失
- **SC-010**: 埋点事件采集覆盖搜索/查询/审核/浏览4类核心行为

## Assumptions

- Apache Doris集群已部署且可通过HTTP API(8030端口)访问，数据库和表结构已创建
- 钉钉Webhook机器人已创建，Webhook URL可配置
- SMTP邮件服务已部署，连接参数可配置
- S3/MinIO对象存储已部署，归档桶已创建
- 前端React 18 + Vite项目结构已有基础框架，可扩展页面
- QuickBI已购买企业版，SSO集成参数可获取
- Redis集群已部署（生产环境），哨兵或集群模式可选
- 现有测试基础设施（testcontainers + pytest）可复用

## Open Questions

- [QuickBI SSO集成方案]：需要确认QuickBI嵌入的具体SSO协议（OAuth2/OIDC/自研Token），影响iframe鉴权实现路径
- [Doris查询权限隔离]：consume多接入方执行查询时，是否需要Doris层面的行级权限隔离？还是仅依赖应用层SQL过滤？
- [埋点存储选型]：用户行为事件存储选择ClickHouse（高吞吐分析）还是ES（与现有搜索共用）？
