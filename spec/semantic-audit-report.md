# 语义建模模块深度审查报告

**审查范围**: `backend/app/services/semantic/` (service.py, repository.py, schemas.py, cache.py) + `backend/app/api/semantic.py` + `backend/app/api/metrics.py` + `backend/app/models/metric.py` + `backend/app/models/metric_template.py`
**审查方法**: 逐行审读全部源码，对照TD §12.3/§3.2接口规范，从工业级韧性角度评估
**审查日期**: 2026-08-12

---

## 综合评分

| 维度 | 得分 | 权重 | 加权 |
|------|------|------|------|
| 功能完整性 | 45/100 | 30% | 13.5 |
| 数据安全 | 70/100 | 20% | 14.0 |
| 容错韧性 | 55/100 | 25% | 13.75 |
| 代码质量 | 65/100 | 15% | 9.75 |
| 可维护性 | 60/100 | 10% | 6.0 |
| **综合** | | | **57.0/100 (L2-级)** |

> 评估为L2级（需重大修复）：核心状态机与TD规范严重偏离，多条P0级功能缺失。

---

## 缺陷清单

### P0 - 核心功能缺失（阻断生产）

#### S-01: 状态机实现与TD §12.3严重偏离
- **文件**: `service.py:227-232, 296-302, 338-354`
- **问题**: 
  - TD要求完整状态机: DRAFT→REVIEW→PUBLISHED/EXPERIMENTAL，含submit/approve/reject/promote/rollback
  - 实际实现: publish直接从DRAFT→PUBLISHED，跳过REVIEW阶段；无submit/approve/reject端点
  - TD要求EXPERIMENTAL灰度状态 → 完全缺失
  - TD要求PENDING_VERSION机制(破坏性变更14天消费方确认期) → 完全缺失
  - TD要求DATA_SOURCE_DROPPED异常子态处理 → 完全缺失
- **影响**: 生产环境无法执行指标审核流程，无灰度发布能力，破坏性变更直接生效可能导致下游报表全线错误
- **修复**: 实现完整6态状态机 + PENDING_VERSION + 灰度 + 回滚

#### S-02: 破坏性变更直接生效（无PENDING_VERSION缓冲）
- **文件**: `service.py:240-261`
- **问题**: TD §12.3第6点明确规定——breaking change应生成PENDING_VERSION，不立即生效，14天消费方确认期。实际实现中update_metric检测到breaking change后仅递增版本号+标记BREAKING，但状态直接生效(当前版本立即更新)
- **影响**: 下游消费方(报表/仪表盘)可能因口径突变产生错误数据，无预警无缓冲
- **修复**: 破坏性变更→创建PENDING_VERSION记录，旧版本保持CURRENT，14天确认期+超时默认接受+明确拒绝驳回

#### S-03: 缺失依赖指标发布前递归校验
- **文件**: `service.py:279-336` (publish_metric)
- **问题**: TD §12.3第4点要求"派生/复合指标发布前递归校验依赖指标均PUBLISHED且非DEPRECATED，并做DERIVED_FROM有向图环检测"。实际publish_metric无任何依赖校验
- **影响**: 派生指标引用未发布的原子指标→下游查询失败；循环依赖→无限递归
- **修复**: publish前检查definition_json.dependencies中所有依赖指标状态，检测DAG环

#### S-04: 缺失冲突预检
- **文件**: `service.py:72-141` (create_metric)
- **问题**: TD §12.3第2点要求"发conflict.check(metric_code, definition)→若命中同名/相似挂pending_conflict标记"。实际create_metric仅检查编码唯一性，无语义相似度检测
- **影响**: 同域不同编码但语义相同的指标可重复注册，无法检测口径冲突
- **修复**: 创建后异步调conflict服务，命中相似→挂pending_conflict标记

#### S-05: API路径与TD §3.2规范不一致
- **文件**: `api/metrics.py` vs TD §3.2
- **问题**: 
  - TD: `POST /metrics/{code}/submit` → 实际: 无此端点
  - TD: `POST /metrics/{code}/approve` → 实际: 无此端点
  - TD: `POST /metrics/{code}/reject` → 实际: 无此端点
  - TD: `POST /metrics/{code}/promote` → 实际: 无此端点
  - TD: `POST /metrics/{code}/rollback` → 实际: 无此端点
  - TD: `POST /metrics/{code}/watch` → 实际: 无此端点
  - TD: `POST /metrics/compare` → 实际: 无此端点
  - TD: `GET /metrics/dashboard` → 实际: 在`/semantics/dashboard`(路径不一致)
  - TD: `GET /metrics/templates` → 实际: 在`/semantics/templates`(路径不一致)
- **影响**: 前端无法对接TD设计的完整交互流程
- **修复**: 补齐缺失端点，对齐TD路径

---

### P1 - 重要功能缺陷

#### S-06: metric_code校验器过于宽松
- **文件**: `schemas.py:55-61`
- **问题**: 校验仅检查`"_" in v`，允许`a_b`、`___`等不符合格式的编码。TD §12.3要求"域_业务对象_度量_统计周期"格式（如`sales_gmv_day`），且需校验保留词
- **影响**: 生产中会出现`test_1`等无意义编码，无法从编码推断语义
- **修复**: 正则校验`^[a-z][a-z0-9]*(_[a-z][a-z0-9]*){3}$`+保留词黑名单

#### S-07: 发布时缺失双写事件(Neo4j/ES)
- **文件**: `service.py:279-336`
- **问题**: TD §12.3第5点要求"PUBLISHED→双写事件→Neo4j(指标节点+DEFINED_BY/DERIVED_FROM边)+ES(索引)+Cube"。实际publish_metric仅更新MySQL+清缓存，无任何事件发布
- **影响**: 血缘图无指标节点、全文检索无指标索引、消费方无感知
- **修复**: publish后发布metric.published事件，EventBus分发到lineage/search/cube

#### S-08: 废弃时缺失下游消费方通知
- **文件**: `service.py:338-378`
- **问题**: TD要求废弃时"发notify通知全部下游消费方(经metric_lineage反向边)"。实际deprecate_metric仅更新状态+清缓存，无通知
- **影响**: 下游报表/仪表盘使用已废弃指标无人知晓
- **修复**: deprecate后发布metric.deprecated事件→notify服务

#### S-09: deprecate_metric允许非PUBLISHED状态废弃
- **文件**: `service.py:338-354`
- **问题**: 仅检查`status == "DEPRECATED"`则拒绝，但DRAFT/REVIEW状态也可执行废弃操作。TD状态机图明确仅PUBLISHED→DEPRECATED合法
- **影响**: DRAFT指标被废弃后无法走正常发布流程
- **修复**: 增加`metric.status != "PUBLISHED"`校验

#### S-10: version转正与metric更新非原子操作
- **文件**: `service.py:317-326`
- **问题**: publish_metric中先update_with_optimistic_lock更新metric，再mark_version_published更新version。两步非原子——若第二步失败，metric已PUBLISHED但版本未转正
- **影响**: 数据不一致——metric.status=PUBLISHED但最新version.status=DRAFT
- **修复**: 合并为单次事务或在同session中先mark_version再update metric

#### S-11: 紧急发布快通道缺失
- **文件**: `service.py` 全文
- **问题**: TD §12.3明确要求domain_admin可紧急发布(跳过REVIEW但不跳过PII门禁)，需记录emergency_publish标记+24h补审。完全未实现
- **影响**: 紧急数据需求无法快速响应
- **修复**: 新增紧急发布参数+EMERGENCY_PUBLISH审计标记+补审定时任务

#### S-12: 指标健康度评分完全缺失
- **文件**: 全部语义服务文件
- **问题**: TD §12.3要求五维加权评分(口径完整度25%/活跃度20%/质量25%/Owner响应15%/血缘覆盖15%)，含分级(≥85优/70-84良/55-69警/<55危)和红橙整改待办。完全未实现
- **影响**: 治理驾驶舱无健康度数据，无法发现低质量指标
- **修复**: 实现健康度评分引擎+定时刷新+整改待办触发

#### S-13: dashboard查询5次重复SQL(性能问题)
- **文件**: `api/semantic.py:208-259`
- **问题**: dashboard端点执行5个独立SQL查询(总数/按状态/按分级/按域/PII占比)，且均未排除deleted_at IS NOT NULL的软删除记录
- **影响**: N+1查询问题，包含已删除数据，大数据量下性能劣化
- **修复**: 合并为单次带条件聚合查询+加deleted_at过滤

#### S-14: dashboard与消费指南直接在API层写ORM查询
- **文件**: `api/semantic.py:52-60, 208-259, 284-323`
- **问题**: API层直接写`select(Metric).where(...)`等ORM查询，违反DEV_GUIDE §8b.2"Repository层：数据访问，禁止service直接写ORM查询"
- **影响**: 查询逻辑分散在API层，无法复用、无法测试、无法统一优化
- **修复**: 迁移到MetricRepository或新建DashboardRepository/ConsumptionGuideRepository

---

### P2 - 中等优先级

#### S-15: 缓存TTL不随版本绑定失效
- **文件**: `cache.py:25` (固定TTL=600秒)
- **问题**: TD §12.0.2要求"查询缓存TTL按metric_version绑定失效"。实际使用固定10分钟TTL，版本变更时虽有invalidate但依赖写路径主动调用；若invalidate失败(如Redis抖动)，旧版本缓存最多10分钟不刷新
- **修复**: 缓存键含版本号`metric:def:{code}:v{version}`，版本变更时旧键自然过期

#### S-16: 缓存set()未调用record_success()
- **文件**: `cache.py:77-94`
- **问题**: set()方法在成功写入缓存后未调用`self._breaker.record_success()`，而get()方法也从未调用。熔断器只记录失败不记录成功→一旦触发熔断永远无法恢复
- **影响**: Redis短暂故障后熔断打开，即使恢复也无法关闭(无成功记录复位)
- **修复**: get()/set()成功时调用record_success()

#### S-17: 模板创建接口接受dict而非Schema校验
- **文件**: `api/semantic.py:92-121`
- **问题**: create_template接收`body: dict[str, Any]`，无Pydantic校验。用户可传入任意字段或缺失必填字段，code/name/domain等必填字段无校验→空字符串直接入库
- **影响**: 脏数据入库，模板可能无编码/名称/域
- **修复**: 创建MetricTemplateCreateRequest Schema

#### S-18: instantiate_template的merged dict直接解包传Schema
- **文件**: `api/semantic.py:178`
- **问题**: `MetricCreateRequest(**merged)`——merged包含template_id等非MetricCreateRequest字段，Pydantic v2默认忽略多余字段但会静默吞掉数据；如果merged包含type=None但模板未设type→覆盖defaults中的type
- **影响**: 静默数据丢失或意外覆盖
- **修复**: 精确过滤merged字段为MetricCreateRequest接受的字段集合

#### S-19: 消费指南自动生成逻辑硬编码在API层
- **文件**: `api/semantic.py:296-321`
- **问题**: 自动生成消费指南的逻辑(含PII/SEMI_ADDITIVE判断)直接写在API端点中，应属于Service层。且该逻辑在每次GET请求时重新生成，无缓存
- **影响**: 逻辑不可复用、不可测试、每次请求重复计算
- **修复**: 迁移到MetricService，结果缓存

#### S-20: MetricVersion.create_version无IntegrityError捕获
- **文件**: `repository.py:209-221`
- **问题**: create_version()直接add+flush，若违反uk_metric_version唯一约束(metric_id+version)→IntegrityError→500而非业务异常
- **影响**: 并发创建同版本号时暴露5xx而非409
- **修复**: 捕获IntegrityError→ConflictError

#### S-21: list_metrics关键词搜索SQL注入风险(LIKE通配符)
- **文件**: `repository.py:122-124`
- **问题**: `Metric.metric_code.contains(keyword)`生成`LIKE '%keyword%'`，但keyword中的`%`和`_`未被转义→用户输入`%`可匹配所有记录，输入`_`可匹配任意单字符
- **影响**: 非精确搜索结果，可能的DoS(匹配全表)
- **修复**: 转义keyword中的LIKE通配符

#### S-22: 更新时change_reason总是被设置(即使非口径变更)
- **文件**: `service.py:263`
- **问题**: `updates["change_reason"] = request.change_reason`在所有update路径上都执行，包括仅更新name/sla等非口径字段。change_reason应仅关联口径变更
- **影响**: 审计日志中非口径变更也记录了change_reason，混淆语义
- **修复**: 仅在definition_json变更时设置change_reason到版本记录

#### S-23: _is_breaking_change与_compute_diff对dependencies判定一致但逻辑不完整
- **文件**: `service.py:425-460`
- **问题**: BREAKING_DEF_FIELDS包含dependencies，但dependencies是list类型——`old_def.get("dependencies") != new_def.get("dependencies")`做的是Python list比较，顺序不同也判为breaking。实际dependencies顺序无关
- **影响**: 依赖项顺序调整(但集合相同)被误判为破坏性变更
- **修复**: dependencies比较用set()而非直接!=

#### S-24: 缺失指标对比工具
- **文件**: 全部
- **问题**: TD §12.3要求`POST /metrics/compare`→两指标关键字段并排diff。完全未实现
- **修复**: 实现compare端点

#### S-25: 缺失批量注册
- **文件**: 全部
- **问题**: TD §12.3要求`POST /metrics/batch-register`→批量入库DRAFT+batch_id关联。完全未实现
- **修复**: 实现batch-register端点

---

### P3 - 低优先级/代码质量

#### S-26: metric_code validator使用了@field_validator但未用mode="before"
- **文件**: `schemas.py:55-61`
- **问题**: Pydantic v2中@field_validator默认mode="after"，在类型校验后执行。metric_code先被校验为str再被自定义校验——这没问题，但如果要strip空格等预处理需mode="before"
- **影响**: 无功能影响，仅为代码规范
- **修复**: 无需修改，但可考虑strip()

#### S-27: service.py中from datetime import timedelta在方法内部
- **文件**: `service.py:356`
- **问题**: `from datetime import timedelta`在deprecate_metric方法内部导入，应放在模块顶部
- **影响**: 代码风格
- **修复**: 移到顶部import区域

#### S-28: repository.py:184 assert updated is not None
- **文件**: `repository.py:184`
- **问题**: `assert updated is not None`——在优化模式(-O)下assert被跳过，若updated确实为None→后续访问属性行抛AttributeError而非明确业务异常
- **影响**: 生产环境可能抛不明确的AttributeError
- **修复**: 替换为`if updated is None: raise SystemError(...)`

#### S-29: cache.py warm_up逐条写入(无pipeline)
- **文件**: `cache.py:124-149`
- **问题**: warm_up对每个metric单独调用redis.set()，无pipeline→N次网络往返
- **影响**: 预热1000指标=1000次Redis SET调用
- **修复**: 使用redis.pipeline()批量写入

#### S-30: MetricResponse.sunset_until类型为str|None而非date|None
- **文件**: `schemas.py:130`
- **问题**: Metric模型中sunset_until是`date`类型，但Response Schema中是`str | None`→序列化/反序列化可能不一致
- **影响**: 前端接收到字符串而非结构化日期
- **修复**: 改为`date | None`

#### S-31: MetricListParams无deleted_at过滤
- **文件**: `schemas.py:84-92`
- **问题**: 虽然repository查询加了deleted_at IS NOT NULL过滤，但无硬删除的恢复接口。一旦误删无法恢复
- **影响**: 误删不可恢复
- **修复**: 考虑增加恢复端点或仅在API层做软删除标记

#### S-32: create_metric未发布metric.created事件
- **文件**: `service.py:72-141`
- **问题**: 创建指标后仅写日志，未发布metric.created事件。其他服务(如conflict预检)无法感知新指标
- **影响**: 下游事件驱动流程断裂
- **修复**: 创建后发布metric.created事件

#### S-33: MetricVersion.status枚举与Metric.status不一致
- **文件**: `metric.py:250-255` vs `metric.py:143-156`
- **问题**: MetricVersion有PENDING_REVIEW/ARCHIVED状态，Metric无；Metric有EXPERIMENTAL/DATA_SOURCE_DROPPED状态，MetricVersion无。两者状态空间不对齐
- **影响**: 版本状态与指标状态映射困难
- **修复**: 统一状态枚举，或明确映射关系

#### S-34: 缺失命名规范保留词校验
- **文件**: `schemas.py:55-61`
- **问题**: TD §12.3要求"命中保留词→提示修正"。无保留词列表
- **影响**: test/temp/dummy等保留词编码可注册
- **修复**: 添加保留词列表校验

#### S-35: deprecate_metric未校验successor_code是否存在
- **文件**: `service.py:338-378`
- **问题**: 允许设置不存在的successor_code→下游引用断裂
- **影响**: 废弃指标指向不存在的替代指标
- **修复**: 校验successor_code对应的Metric存在且PUBLISHED

#### S-36: review_compliance缺少PII标记为False时的拒绝
- **文件**: `service.py:380-409`
- **问题**: 允许对pii_flag=False的指标执行合规复核→无意义操作
- **影响**: 审计日志记录无意义的复核操作
- **修复**: pii_flag=False时拒绝复核

#### S-37: update_metric允许PUBLISHED状态直接更新(应走版本审批)
- **文件**: `service.py:227-232`
- **问题**: `metric.status not in ("DRAFT", "REVIEW", "PUBLISHED")`→PUBLISHED状态允许直接更新。但TD要求PUBLISHED状态改口径→生成PENDING_VERSION，非直接生效
- **影响**: 已发布指标可被直接修改，绕过版本审批流程
- **修复**: PUBLISHED状态更新走PENDING_VERSION机制

#### S-38: 缺失SLA例外日历
- **文件**: 全部
- **问题**: TD §3.2要求`GET/POST /sla-calendar`→SLA例外日历查询/配置。完全未实现
- **修复**: 后续迭代实现

---

## 关键代码位置索引

| 缺陷 | 文件 | 行号 | 问题类型 |
|------|------|------|----------|
| S-01 | service.py | 227-232,296-302,338-354 | 状态机偏离TD |
| S-02 | service.py | 240-261 | 破坏性变更直接生效 |
| S-03 | service.py | 279-336 | 缺失依赖校验 |
| S-04 | service.py | 72-141 | 缺失冲突预检 |
| S-05 | api/metrics.py | 全文 | API路径不一致 |
| S-06 | schemas.py | 55-61 | 校验器过松 |
| S-07 | service.py | 279-336 | 缺失双写事件 |
| S-08 | service.py | 338-378 | 缺失下游通知 |
| S-09 | service.py | 338-354 | 非PUBLISHED可废弃 |
| S-10 | service.py | 317-326 | 非原子操作 |
| S-11 | service.py | - | 紧急发布缺失 |
| S-12 | 全部 | - | 健康度评分缺失 |
| S-13 | api/semantic.py | 208-259 | 5次重复SQL |
| S-14 | api/semantic.py | 52-323 | API层写ORM查询 |
| S-15 | cache.py | 25 | TTL不随版本 |
| S-16 | cache.py | 60-94 | 熔断不复位 |
| S-17 | api/semantic.py | 92-121 | 无Schema校验 |
| S-18 | api/semantic.py | 178 | 静默数据丢失 |
| S-19 | api/semantic.py | 296-321 | 逻辑硬编码API层 |
| S-20 | repository.py | 209-221 | 无IntegrityError捕获 |
| S-21 | repository.py | 122-124 | LIKE通配符未转义 |
| S-22 | service.py | 263 | change_reason语义混淆 |
| S-23 | service.py | 425-460 | dependencies顺序误判 |
| S-28 | repository.py | 184 | assert生产失效 |
| S-29 | cache.py | 124-149 | 无pipeline |

---

## TD §12.3 合规差距矩阵

| TD要求 | 实现状态 | 缺陷编号 |
|--------|----------|----------|
| 完整6态状态机(DRAFT/REVIEW/PUBLISHED/EXPERIMENTAL/DEPRECATED/DATA_SOURCE_DROPPED) | ❌ 仅4态,无REVIEW流转 | S-01 |
| submit/approve/reject端点 | ❌ 缺失 | S-01,S-05 |
| PENDING_VERSION(14天确认期) | ❌ 缺失 | S-02 |
| 灰度发布+promote/rollback | ❌ 缺失 | S-01,S-05 |
| 冲突预检(conflict.check) | ❌ 缺失 | S-04 |
| PII门禁(compliance_officer复核) | ⚠️ 部分实现(仅review_compliance) | S-11 |
| 依赖递归校验+环检测 | ❌ 缺失 | S-03 |
| 双写事件(Neo4j/ES/Cube) | ❌ 缺失 | S-07 |
| 废弃通知下游 | ❌ 缺失 | S-08 |
| 紧急发布快通道 | ❌ 缺失 | S-11 |
| 健康度评分(5维加权) | ❌ 缺失 | S-12 |
| 命名规范校验(保留词) | ⚠️ 弱校验 | S-06,S-34 |
| 指标对比工具 | ❌ 缺失 | S-24 |
| 批量注册(batch_id) | ❌ 缺失 | S-25 |
| 消费指南 | ⚠️ 自动生成在API层 | S-19 |
| SLA例外日历 | ❌ 缺失 | S-38 |
| watch关注 | ❌ 缺失 | S-05 |
| 乐观锁 | ✅ 实现 | - |
| 版本快照+diff | ✅ 实现(但缺PENDING_VERSION) | S-02 |
| 缓存cache-aside+熔断 | ✅ 实现(但TTL/熔断复位有bug) | S-15,S-16 |
| PII访问审计 | ✅ 实现 | - |
| 合规复核禁自审 | ✅ 实现 | - |

**合规率**: 6/21 = 28.6%

---

## 与采集模块审查对比

| 维度 | 采集模块 | 语义模块 |
|------|----------|----------|
| 综合评分 | 40.5/100 | 57.0/100 |
| P0缺陷数 | 4 | 5 |
| TD合规率 | ~20% | 28.6% |
| 核心问题 | 仅MySQL+无容错 | 状态机偏离+缺失关键流程 |
| 代码质量 | 较差(硬编码/静默异常) | 中等(有乐观锁/版本化/审计) |
| 韧性基础 | 差 | 中等(有缓存熔断/乐观锁) |

语义模块代码质量优于采集模块(已有乐观锁、版本快照、PII审计、缓存熔断)，但功能完整性严重不足——TD设计了完整的状态机/灰度/版本缓冲机制，实际实现是简化版的直接生效模型。

---

## 修复优先级建议

### MVP(必须先修): S-01 + S-02 + S-03 + S-09 + S-10
- 完整6态状态机 + PENDING_VERSION + 依赖校验 + 废弃校验 + 发布原子性

### 第二批: S-04 + S-05 + S-06 + S-07 + S-08 + S-13 + S-14
- 冲突预检 + API端点补齐 + 校验增强 + 事件发布 + 通知 + dashboard修复 + 分层重构

### 第三批: S-11 + S-12 + S-15 + S-16 + S-17 + S-20 + S-21 + S-23
- 紧急发布 + 健康度 + 缓存修复 + 模板校验 + 仓库加固 + SQL安全 + 依赖比较修复

### 第四批: S-18 + S-19 + S-22 + S-24 + S-25 + S-28 + S-29 + S-30 + S-33 + S-35 + S-36 + S-37
- 代码质量提升 + 缺失功能补齐
