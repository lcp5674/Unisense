# Feature Specification: 语义建模模块工业级整改

**Created**: 2026-08-12  
**Status**: Draft  
**Input**: 语义建模模块深度审查报告(38项缺陷，57.0/100，TD合规率28.6%)

## Overview

语义建模模块（semantic）是Unisense指标语义中台的口径真相源，承载指标定义、状态机、版本化、口径管理的核心职责。深度审查发现当前实现与TD §12.3规范严重偏离：状态机缺失完整流转(DRAFT→REVIEW→PUBLISHED)、破坏性变更直接生效(无PENDING_VERSION缓冲)、缺失依赖校验/冲突预检/双写事件/下游通知/紧急发布/健康度评分等关键能力，综合评分57.0/100(L2级)，TD合规率仅28.6%。

本次整改目标：将语义模块从当前L2级提升至工业级标准，全面对齐TD §12.3规范，消除全部38项缺陷，使TD合规率达到100%。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 指标完整生命周期管理 (Priority: P0) 🎯

数据治理员需要按照完整的审核流程管理指标生命周期：创建指标(DRAFT)→提交审核(submit→REVIEW)→审核通过(approve→PUBLISHED)或驳回(reject→DRAFT)，废弃指标(DEPRECATED)时须校验下游影响并通知消费方。当前实现跳过REVIEW直接发布、无submit/approve/reject端点，生产环境无法执行审核流程。

**Why this priority**: 状态机是语义模块的核心骨架，所有其他功能(版本缓冲/灰度/废弃通知)都依赖正确的状态流转。没有完整状态机，指标发布无审核把控，破坏性变更直接生效可能导致下游报表全线错误。

**Independent Test**: 创建指标→submit提交→approve通过→验证status流转链正确；reject驳回→验证回退DRAFT；非法跃迁(如DRAFT直接deprecate)→验证409拒绝。

**Acceptance Scenarios**:

1. **Given** 指标处于DRAFT状态, **When** Owner调用submit, **Then** 状态变为REVIEW，记录audit+通知domain_admin待审
2. **Given** 指标处于REVIEW状态, **When** domain_admin调用approve, **Then** 状态变为PUBLISHED，版本转正，双写事件发布(Neo4j/ES)
3. **Given** 指标处于REVIEW状态, **When** domain_admin调用reject(reason), **Then** 状态回退DRAFT，通知Owner驳回原因
4. **Given** 指标处于DRAFT状态, **When** 尝试直接deprecate, **Then** 返回409非法状态跃迁
5. **Given** 指标处于PUBLISHED状态, **When** 调用deprecate(successor_code), **Then** 校验successor_code存在且PUBLISHED，状态变为DEPRECATED，通知下游消费方
6. **Given** 指标处于DRAFT状态, **When** 尝试deprecate, **Then** 返回409(仅PUBLISHED可废弃)

---

### User Story 2 - 破坏性变更PENDING_VERSION缓冲 (Priority: P0) 🎯

指标Owner修改已发布指标的口径定义时，系统需自动判定是否为破坏性变更(breaking change)。若为破坏性变更，不立即生效，而是生成PENDING_VERSION，旧版本保持CURRENT；需等14天消费方确认期(超时默认接受、明确拒绝驳回)，确认后才切换为新版本。当前实现破坏性变更直接生效，无缓冲期。

**Why this priority**: 生产环境中已发布指标的口径突变是最高风险操作——下游报表/仪表盘/数据产品可能因口径突变产生错误数据且无预警，PENDING_VERSION是TD设计的核心安全机制。

**Independent Test**: 发布指标→PUT修改expression(breaking)→验证生成PENDING_VERSION旧版本仍CURRENT→消费方confirm→新版本升CURRENT→消费方reject→PENDING_VERSION取消回旧版本→超时14天→默认接受。

**Acceptance Scenarios**:

1. **Given** 指标PUBLISHED v1, **When** Owner PUT修改expression(breaking), **Then** 生成v2 PENDING_VERSION，v1仍CURRENT，通知全部下游消费方
2. **Given** PENDING_VERSION v2等待中, **When** 消费方POST confirm, **Then** v2升CURRENT，触发物化表重建通知
3. **Given** PENDING_VERSION v2等待中, **When** 消费方POST reject(reason), **Then** v2取消(CANCELLED)，v1保持CURRENT，通知Owner驳回原因
4. **Given** PENDING_VERSION v2等待14天, **When** 无任何confirm/reject, **Then** 超时默认接受，v2升CURRENT
5. **Given** PENDING_VERSION v2等待中, **When** Owner请求延期, **Then** 延期+7天(最多延期1次)
6. **Given** PENDING_VERSION v2等待中且Schema Drift发生, **When** 采集模块检测到源表变更, **Then** 自动暂停版本切换+通知Owner复核

---

### User Story 3 - 依赖指标递归校验与环检测 (Priority: P0) 🎯

派生/复合指标发布前，系统需递归校验其依赖的所有指标均处于PUBLISHED状态且非DEPRECATED，并做DERIVED_FROM有向图环检测，防止循环依赖导致查询无限递归。当前实现无任何依赖校验。

**Why this priority**: 派生指标引用未发布/已废弃的原子指标→下游查询直接失败；循环依赖→无限递归→服务崩溃。这是数据正确性的硬性保障。

**Independent Test**: 创建派生指标A依赖B(B为DRAFT)→publish A→拒绝(依赖未发布)→publish B→publish A→成功；创建A依赖B、B依赖A→环检测→拒绝。

**Acceptance Scenarios**:

1. **Given** 派生指标A依赖B(B为DRAFT), **When** 尝试publish A, **Then** 拒绝，提示"依赖指标B未发布"
2. **Given** 派生指标A依赖B(B为DEPRECATED), **When** 尝试publish A, **Then** 拒绝，提示"依赖指标B已废弃"
3. **Given** 指标A依赖B、B依赖A, **When** 尝试publish任意一个, **Then** 环检测拒绝，提示"检测到循环依赖: A→B→A"
4. **Given** 复合指标C依赖A和B(均PUBLISHED), **When** publish C, **Then** 成功，DERIVED_FROM关系写入Neo4j
5. **Given** 指标A依赖B(B为PENDING_VERSION), **When** publish A, **Then** 允许(A依赖B的CURRENT版本)

---

### User Story 4 - 冲突预检与命名规范校验 (Priority: P1)

指标注册时，系统需自动执行冲突预检——调用conflict服务检测是否存在同名/相似口径指标，命中则挂pending_conflict标记(不阻塞注册但审核时提示)。同时metric_code需严格校验"域_业务对象_度量_统计周期"格式并检查保留词。

**Why this priority**: 无冲突预检→同域不同编码但语义相同的指标重复注册→口径歧义→治理混乱。命名规范是指标语义可读性的基础。

**Independent Test**: 注册指标sales_gmv_day→冲突预检→发现已存在sales_gmv_daily(相似)→挂pending_conflict标记；注册test_1→校验拒绝(格式不符)；注册temp_xxx→校验拒绝(保留词)。

**Acceptance Scenarios**:

1. **Given** 已存在指标sales_gmv_daily, **When** 注册sales_gmv_day, **Then** 创建成功但挂pending_conflict标记，审核时提示相似指标
2. **Given** metric_code为"a_b", **When** 注册, **Then** 拒绝(不符合4段格式)
3. **Given** metric_code为"test_gmv_day", **When** 注册, **Then** 拒绝(保留词test)
4. **Given** metric_code为"sales_gmv_day_2024", **When** 注册, **Then** 拒绝(5段格式，应为4段)
5. **Given** metric_code为"sales_gmv_day", **When** 注册, **Then** 通过(符合格式)

---

### User Story 5 - 事件驱动双写与下游通知 (Priority: P1)

指标发布/废弃/口径变更时，系统需通过EventBus发布事件→Neo4j(指标节点+DEFINED_BY/DERIVED_FROM边)、ES(全文索引)、notify(下游消费方通知)。当前实现仅清缓存，无任何事件发布。

**Why this priority**: 无双写→血缘图无指标节点、全文检索无索引、消费方无感知。事件驱动是系统间解耦的核心机制。

**Independent Test**: 发布指标→验证EventBus收到metric.published事件→Neo4j创建节点+边→ES索引更新→notify发送下游通知。

**Acceptance Scenarios**:

1. **Given** 指标approve通过, **When** 状态变为PUBLISHED, **Then** 发布metric.published事件(含metric_code/version/definition)，EventBus分发到lineage/search/notify
2. **Given** 指标deprecate, **When** 状态变为DEPRECATED, **Then** 发布metric.deprecated事件(含successor_code)，notify通知全部下游消费方(经lineage反查)
3. **Given** PENDING_VERSION生成, **When** 破坏性变更提交, **Then** 发布metric.pending_version事件，notify通知下游消费方确认
4. **Given** 指标创建, **When** DRAFT状态, **Then** 发布metric.created事件，conflict服务异步预检
5. **Given** EventBus发布失败, **When** Neo4j/ES不可达, **Then** 事件入重试队列(Arq)，读侧对缺失标stale

---

### User Story 6 - 灰度发布与回滚 (Priority: P1)

domain_admin可选择将指标发布为EXPERIMENTAL灰度状态(仅白名单租户/报表可见)，观察期满后一键全量promote→PUBLISHED；异常时一键rollback→回退上一PUBLISHED版本。当前完全缺失灰度能力。

**Why this priority**: 灰度发布是工业级系统的标配能力，允许安全观察新口径效果，异常时可快速回滚，避免全量发布风险。

**Independent Test**: approve时选择灰度→status=EXPERIMENTAL→观察7天→promote→PUBLISHED；灰度期间发现问题→rollback→回退上一版本。

**Acceptance Scenarios**:

1. **Given** 指标REVIEW状态, **When** approve时选择gray_tenant_ids, **Then** 状态变为EXPERIMENTAL，仅白名单租户可见
2. **Given** 指标EXPERIMENTAL, **When** 调用promote, **Then** 状态变为PUBLISHED，全量可见，通知+审计
3. **Given** 指标EXPERIMENTAL, **When** 调用rollback, **Then** 回退到上一PUBLISHED版本，EXPERIMENTAL版本标记ARCHIVED
4. **Given** 指标EXPERIMENTAL, **When** 超过30天未promote/rollback, **Then** 自动提醒Owner决策

---

### User Story 7 - 紧急发布快通道 (Priority: P1)

domain_admin可在紧急场景(监管要求/生产事故/数据泄露)下跳过REVIEW直接发布，但须：填写紧急原因、24小时内补审并留EMERGENCY_PUBLISH审计标记、补审未通过须立即回滚。含PII的指标紧急发布仍须compliance_officer复核通过(合规门禁不可跳过)。

**Why this priority**: 紧急数据需求(如监管报表)需要快速响应通道，但须有审计追溯和补审闭环。

**Independent Test**: domain_admin紧急发布(无PII)→直接PUBLISHED+EMERGENCY_PUBLISH标记→24h内补审→补审通过标记清除；紧急发布(含PII)→仍须合规复核→合规官不可达→仅INTERNAL分级发布。

**Acceptance Scenarios**:

1. **Given** 指标DRAFT且无PII, **When** domain_admin紧急发布(填紧急原因), **Then** 跳过REVIEW直接PUBLISHED，审计标记EMERGENCY_PUBLISH+reason
2. **Given** 紧急发布指标, **When** 24h内domain_admin补审, **Then** 补审通过→标记清除；补审拒绝→立即回滚DRAFT
3. **Given** 指标含PII, **When** 紧急发布, **Then** 合规门禁不可跳过，须compliance_officer先复核通过
4. **Given** 指标含PII且合规官不可达, **When** 紧急发布, **Then** 仅按INTERNAL分级发布(PII维度值全脱敏)
5. **Given** 紧急发布指标超24h未补审, **When** 定时任务检查, **Then** 自动告警+通知domain_admin

---

### User Story 8 - 指标健康度评分 (Priority: P1)

系统按五维加权模型自动计算指标健康度评分(0-100)：口径完整度25%/活跃度20%/质量25%/Owner响应15%/血缘覆盖15%。≥85优(绿)/70-84良(蓝)/55-69警(橙)/<55危(红)。红橙指标自动进整改待办。某维度数据缺失→该维记0并标"数据不足"。

**Why this priority**: 治理驾驶舱需要量化指标质量，低质量指标需主动发现和整改，这是数据治理闭环的关键环节。

**Independent Test**: 发布指标→等健康度评分刷新→验证5维各得分→口径不完整→完整度降分→Owner长期未响应→响应降分→总分<55→红标整改待办。

**Acceptance Scenarios**:

1. **Given** 指标一等字段齐全, **When** 计算口径完整度, **Then** 得分100/100
2. **Given** 指标缺少sla/source, **When** 计算口径完整度, **Then** 缺失字段扣分，完整度<70
3. **Given** 指标近30天无consume查询, **When** 计算活跃度, **Then** 活跃度=0
4. **Given** 指标总分<55, **When** 评分刷新, **Then** 标红+自动进整改待办(notify.todo)
5. **Given** 血缘未覆盖, **When** 计算血缘覆盖, **Then** 该维记0+标"数据不足"
6. **Given** 凌晨定时任务, **When** 批量重算, **Then** 全部指标评分刷新+红橙进待办

---

### User Story 9 - 指标对比工具 (Priority: P2)

数据治理员可选择两个指标进行并排对比，系统返回关键字段差异：口径定义diff、粒度、维度集差集/交集、单位/币种、来源表差异、时间语义、可加性、使用注意事项。差异字段标记difference_level(identical/similar/different)。

**Why this priority**: 同名不同义排查是指标治理的高频操作，对比工具提升治理效率。

**Independent Test**: 选择两个指标→POST /metrics/compare→返回并排diff+差异高亮。

**Acceptance Scenarios**:

1. **Given** 两个不同指标, **When** 调用compare, **Then** 返回关键字段并排对比+差异标记
2. **Given** 两个完全相同指标, **When** 调用compare, **Then** 所有字段标记identical
3. **Given** 两指标无查看权限, **When** 调用compare, **Then** 返回403
4. **Given** 传入超过2个指标, **When** 调用compare, **Then** 返回422校验拒绝

---

### User Story 10 - 批量注册 (Priority: P2)

数据治理员可选中宽表→LLM解析产出N候选指标(度量列+维度列映射)→逐条校验门禁→批量入库DRAFT(共享batch_id)→审核台按batch_id聚合展示→逐条审核/通过/驳回。

**Why this priority**: 大量表指标需要快速注册，手动逐条创建效率极低。批量注册是高频运营操作。

**Independent Test**: 提交宽表+度量列列表→LLM解析→批量入库10个DRAFT指标→按batch_id查询→逐条审核。

**Acceptance Scenarios**:

1. **Given** 宽表有5个度量列, **When** 批量注册, **Then** 生成5个DRAFT指标+共享batch_id+LLM预填字段
2. **Given** 批量注册中某条校验失败, **When** 入库, **Then** 失败条目标记validation_error，其余成功入库
3. **Given** 批量注册完成, **When** 审核台查询, **Then** 按batch_id聚合展示全部候选指标
4. **Given** 批量注册, **When** LLM解析超时, **Then** 降级为手动填写模式

---

### User Story 11 - Dashboard与消费指南重构 (Priority: P1)

消费者仪表盘需修复5次重复SQL+未排除软删除+API层写ORM查询的问题，迁移到Repository层。消费指南自动生成逻辑从API层迁移到Service层+缓存。模板创建需Schema校验替代裸dict。

**Why this priority**: 当前dashboard性能劣化(N+1查询)、包含已删除数据、违反分层架构。消费指南逻辑硬编码在API层不可测试。模板创建无校验导致脏数据。

**Independent Test**: dashboard查询→1次SQL(含deleted_at过滤)→结果正确；消费指南查询→命中缓存；模板创建→缺必填字段拒绝。

**Acceptance Scenarios**:

1. **Given** 1000个指标(含10个已软删除), **When** 查询dashboard, **Then** 单次聚合SQL+排除软删除+响应<500ms
2. **Given** 指标无consumption_guide, **When** GET消费指南, **Then** Service层自动生成+缓存结果
3. **Given** 模板创建缺name, **When** POST创建, **Then** 422校验拒绝
4. **Given** 模板创建code重复, **When** POST创建, **Then** 409冲突拒绝

---

### User Story 12 - 缓存韧性修复与数据安全加固 (Priority: P1)

修复缓存TTL不随版本绑定、熔断器不复位、预热无pipeline、LIKE通配符未转义、IntegrityError暴露5xx、assert生产失效等韧性+安全问题。

**Why this priority**: 这些是生产环境会直接触发的问题：Redis短暂故障后熔断永远不复位→缓存永久降级；LIKE注入→搜索异常；assert在-O模式下跳过→不明确异常。

**Independent Test**: Redis故障5次→熔断打开→Redis恢复→get成功→record_success复位；搜索`%`→无结果(通配符已转义)；并发创建同版本→409而非500。

**Acceptance Scenarios**:

1. **Given** Redis连续5次get失败, **When** 熔断打开后Redis恢复, **Then** 下一次get成功后record_success复位
2. **Given** 缓存键含版本号, **When** 版本变更, **Then** 旧键自然过期，新查询回源
3. **Given** 搜索keyword含%, **When** LIKE查询, **Then** %被转义为\%，仅精确匹配含%的编码
4. **Given** 并发创建(metric_id=1,version=2), **When** 两个请求同时flush, **Then** 返回409 ConflictError而非500
5. **Given** Python -O模式运行, **When** update_with_optimistic_lock, **Then** 不依赖assert，用显式校验+SystemError

---

### User Story 13 - 版本原子性与数据一致性保障 (Priority: P0)

指标发布时metric.status更新与metric_version.status转正必须原子执行。当前两步非原子——metric已PUBLISHED但version仍DRAFT可能导致数据不一致。version status枚举与metric status枚举需对齐。

**Why this priority**: 数据一致性是语义真相源的生命线。metric和version状态不一致→消费方无法确定生效版本→查询结果不可预测。

**Independent Test**: 发布指标→同一事务中metric.status=PUBLISHED且version.status=PUBLISHED→任一步失败→全部回滚。

**Acceptance Scenarios**:

1. **Given** 指标approve, **When** 发布, **Then** 同一事务中metric.status=PUBLISHED+version.status=PUBLISHED+published_at记录
2. **Given** 版本转正失败, **When** 事务回滚, **Then** metric.status保持REVIEW，无脏数据
3. **Given** Metric.status有EXPERIMENTAL, **When** 检查MetricVersion.status枚举, **Then** 版本也有对应EXPERIMENTAL状态

---

### Edge Cases

- 当PENDING_VERSION等待期间，源表发生Schema Drift(字段删除/类型变更)→自动暂停版本切换+通知Owner复核
- 紧急发布含PII指标且合规官不可达→仅INTERNAL分级发布(PII维度值全脱敏)
- 派生指标A依赖B→B被废弃→A的依赖校验应标记B为不可用→A需更新依赖或废弃
- 灰度指标超过30天未决策→自动提醒但不强制回滚
- 破坏性变更PENDING_VERSION期间，同一指标再次提交破坏性变更→替换PENDING_VERSION
- 批量注册时LLM解析超时→降级为手动填写模式
- 指标对比时两指标属不同域→须同时有两域查看权限
- dependencies列表比较时顺序无关(集合比较而非列表比较)
- metric_code中含保留词(如test/temp/dummy/demo)→软提醒(非硬阻断)
- PENDING_VERSION延期最多1次(+7天)

## Requirements *(mandatory)*

### Functional Requirements

**状态机与生命周期**:
- **FR-001**: 系统必须实现完整6态状态机(DRAFT/REVIEW/PUBLISHED/EXPERIMENTAL/DEPRECATED/DATA_SOURCE_DROPPED)，含submit/approve/reject/promote/rollback/deprecate端点
- **FR-002**: 系统必须校验状态跃迁合法性，非法跃迁返回409(如DRAFT不可直接deprecate，仅PUBLISHED可deprecate)
- **FR-003**: 系统必须实现submit端点(DRAFT→REVIEW)，提交时触发冲突预检+通知domain_admin待审
- **FR-004**: 系统必须实现approve端点(REVIEW→PUBLISHED或EXPERIMENTAL)，含PII门禁+依赖校验
- **FR-005**: 系统必须实现reject端点(REVIEW→DRAFT)，含驳回原因+通知Owner

**PENDING_VERSION**:
- **FR-006**: 系统必须实现PENDING_VERSION机制——破坏性变更不立即生效，生成PENDING_VERSION旧版本保持CURRENT
- **FR-007**: 系统必须提供消费方confirm/reject端点——确认后PENDING_VERSION升CURRENT，拒绝则取消回旧版本
- **FR-008**: 系统必须实现14天超时默认接受+延期机制(最多延期1次+7天)
- **FR-009**: 系统必须在PENDING_VERSION期间检测Schema Drift→自动暂停版本切换+通知Owner

**依赖校验**:
- **FR-010**: 系统必须在指标发布前递归校验依赖指标均PUBLISHED且非DEPRECATED
- **FR-011**: 系统必须做DERIVED_FROM有向图环检测，检测到循环依赖则拒绝发布

**冲突预检与命名**:
- **FR-012**: 系统必须在指标创建后异步调conflict服务预检，命中相似口径→挂pending_conflict标记
- **FR-013**: 系统必须严格校验metric_code格式为"域_业务对象_度量_统计周期"(4段式)，命中保留词→软提醒

**事件驱动**:
- **FR-014**: 系统必须在指标PUBLISHED时发布metric.published事件→EventBus分发到lineage(Neo4j)/search(ES)/notify
- **FR-015**: 系统必须在指标DEPRECATED时发布metric.deprecated事件→notify通知下游消费方
- **FR-016**: 系统必须在指标创建时发布metric.created事件→conflict异步预检
- **FR-017**: 系统必须在PENDING_VERSION生成时发布metric.pending_version事件→notify通知下游
- **FR-018**: 事件发布失败须入重试队列(Arq)，读侧对缺失标stale

**灰度发布**:
- **FR-019**: 系统必须支持approve时选择灰度模式(EXPERIMENTAL)，指定白名单租户
- **FR-020**: 系统必须提供promote端点(EXPERIMENTAL→PUBLISHED)和rollback端点(→上一PUBLISHED版本)
- **FR-021**: 灰度超过30天未决策→自动提醒Owner

**紧急发布**:
- **FR-022**: 系统必须支持domain_admin紧急发布(跳过REVIEW直接PUBLISHED)，须填紧急原因+EMERGENCY_PUBLISH审计标记
- **FR-023**: 紧急发布24h内须补审，补审拒绝→立即回滚DRAFT
- **FR-024**: 含PII指标紧急发布不可跳过合规门禁；合规官不可达→仅INTERNAL分级发布(PII维度值全脱敏)

**健康度评分**:
- **FR-025**: 系统必须按五维加权计算指标健康度(口径完整度25%/活跃度20%/质量25%/Owner响应15%/血缘覆盖15%)
- **FR-026**: 系统必须实现分级(≥85优/70-84良/55-69警/<55危)+红橙进整改待办
- **FR-027**: 维度数据缺失→该维记0+标"数据不足"
- **FR-028**: 系统必须每日凌晨批量重算+关键事件(质量异常/状态变更)实时增量

**指标对比**:
- **FR-029**: 系统必须提供POST /metrics/compare端点→两指标关键字段并排diff+差异标记

**批量注册**:
- **FR-030**: 系统必须提供POST /metrics/batch-register端点→批量入库DRAFT+共享batch_id+LLM预填
- **FR-031**: 批量注册中校验失败条目标记validation_error，其余成功入库

**缓存韧性**:
- **FR-032**: 缓存键必须含版本号metric:def:{code}:v{version}，版本变更时旧键自然过期
- **FR-033**: 缓存get/set成功时必须调用record_success()复位熔断器
- **FR-034**: 缓存warm_up必须使用pipeline批量写入
- **FR-035**: LIKE查询必须转义keyword中的%和_通配符

**数据安全**:
- **FR-036**: MetricVersion创建必须捕获IntegrityError→ConflictError(非5xx)
- **FR-037**: update_with_optimistic_lock中assert必须替换为显式校验+SystemError
- **FR-038**: metric.status与MetricVersion.status枚举必须对齐
- **FR-039**: deprecate时必须校验successor_code存在且PUBLISHED
- **FR-040**: review_compliance须校验pii_flag=True(非PII指标拒绝复核)
- **FR-041**: 模板创建须Schema校验(非裸dict)
- **FR-042**: 指标发布时metric.status更新与version.status转正必须原子执行(同事务)

**架构重构**:
- **FR-043**: dashboard查询逻辑迁移到Repository层+合并为单次聚合SQL+加deleted_at过滤
- **FR-044**: 消费指南自动生成逻辑迁移到Service层+缓存结果
- **FR-045**: API层禁止直接写ORM查询(DEV_GUIDE §8b.2)

**dependencies比较**:
- **FR-046**: dependencies列表比较必须用集合比较(顺序无关)

### Key Entities

- **MetricState**: 状态机6态枚举(DRAFT/REVIEW/PUBLISHED/EXPERIMENTAL/DEPRECATED/DATA_SOURCE_DROPPED)+合法跃迁矩阵
- **PendingVersion**: 破坏性变更缓冲实体(关联MetricVersion+确认状态+超时时间+延期次数)
- **MetricHealthScore**: 五维加权评分实体(口径完整度/活跃度/质量/Owner响应/血缘覆盖)
- **VersionConfirmation**: 消费方版本确认记录(确认/拒绝+原因+时间戳)
- **BatchRegistration**: 批量注册批次(关联batch_id+候选指标列表+LLM解析状态)

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: TD §12.3合规率达到100%(当前28.6%，21项中6项达标→全部21项达标)
- **SC-002**: 审查报告38项缺陷全部消除，综合评分≥90/100(当前57.0)
- **SC-003**: 状态机6态+8种合法跃迁全部可测试通过(submit/approve/reject/promote/rollback/deprecate/confirm/reject_version)
- **SC-004**: 破坏性变更100%走PENDING_VERSION缓冲，0例直接生效
- **SC-005**: 依赖校验100%覆盖派生/复合指标，环检测100%拦截循环依赖
- **SC-006**: 事件发布率100%(PUBLISHED/DEPRECATED/PENDING_VERSION均触发EventBus)
- **SC-007**: 缓存熔断器可自动复位(5次失败后熔断→恢复后成功复位)
- **SC-008**: 所有5xx类数据安全问题(IntegrityError/assert/LIKE注入)归零
- **SC-009**: 发布操作原子性100%(metric+version同事务，无脏数据)
- **SC-010**: 健康度评分每日刷新+红橙指标100%进整改待办

## Assumptions

- conflict服务已实现check接口(可调用检测相似口径)
- EventBus已实现(前序整改中已完成，位于app.core.eventbus)
- Neo4j/ES写入端点已由lineage/search服务提供(事件驱动调用)
- notify服务已实现todo/通知功能(前序整改中已完成)
- Arq任务队列已配置(前序整改中已完成)
- Redis连接池由app.db.redis.get_redis()提供(前序整改中已改为lifespan管理)
- PENDING_VERSION超时检查用Arq cron定时任务实现(每日检查)
- 健康度评分的活跃度数据来自consume.queried事件(经observability聚合)
- 健康度评分的质量数据来自quality_event(经quality服务聚合)
- LLM批量解析复用ai服务已有能力
- 合规官不可达判定：compliance_officer角色无活跃用户→仅INTERNAL分级发布

## Open Questions

- [PENDING_VERSION超时精确度]: 14天超时是精确到秒还是日？默认精确到秒(按created_at+14*86400)，但可接受日级粒度
- [健康度评分权重配置化]: 五维权重是否需要运行时可配置？默认硬编码但预留配置入口，后续迭代开放domain_admin自定义
- [批量注册LLM降级]: LLM不可用时是否允许纯手动批量注册(前端逐行填写)？默认允许，batch-register接口支持llm_prefill=false跳过LLM
