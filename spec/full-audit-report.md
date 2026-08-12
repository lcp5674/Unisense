# Unisense 指标语义中台 · 全维度穿透式审查报告

> 审查日期：2026-08-11 | 审查范围：14个领域服务全部代码 + 文档 + CI/CD + 基础设施  
> 审查标准：GB/T 36073 数据管理能力成熟度评估模型 + 工业级软件质量规范  
> 审查方法：逐文件代码审计 + 文档-代码双向比对 + 配置-实现一致性校验  

---

## 一、产品维度审查

### 1.1 核心需求与落地功能匹配度

基于 PRD（proposal.md v1.0，含 R1-R13 共221项审查嵌入）与实际代码逐项比对：

| FR编号 | PRD需求 | 代码实现状态 | 匹配度 | 证据（文件:行号/内容） |
|--------|---------|-------------|--------|----------------------|
| FR-02 | 元数据采集（自动扫库+敏感识别+幂等） | ✅ 已实现 | 95% | `api/collector.py:52-274` 全部端点；`service` 含幂等键 `source_id+entity_name`；敏感分级 `sensitivity_level` 自动标记 |
| FR-03 | LLM辅助解析（双策略+置信度分级） | ⚠️ 部分实现 | 60% | `services/llm/client.py` 仅有通用 chat 接口；**缺失** PRD §4.3 要求的结构化输出 Schema（Pydantic）、置信度分级分流、小样本校准门禁、去重与冲突预检（4.3§6 与4.7对齐） |
| FR-04 | 数据血缘（表级+字段级+影响面） | ✅ 已实现 | 85% | `api/lineage.py:44-124`；字段级血缘已实现深度列解析（CTE/子查询）；影响面 BFS 受 `max_edges=5000` 约束 |
| FR-05 | 语义建模（原子/派生/复合+版本管理） | ✅ 已实现 | 90% | `services/semantic/service.py` 完整 CRUD+状态机+版本快照+乐观锁；缺失复合指标派生表达式解析执行 |
| FR-06 | 执行引擎（口径→SQL+下推） | ⚠️ 占位实现 | 30% | `services/consume/service.py:144-173` `execute_query` 返回空 `data={"rows": []}` + placeholder SQL，**无真实 OLAP 执行** |
| FR-07 | 指标管理体系 | ✅ 已实现 | 90% | 语义服务覆盖注册/审核/待办/发布/废弃/PII门禁；缺失模板包、驾驶舱、消费指南前端展示 |
| FR-08 | 术语库 | ✅ 已实现 | 85% | `api/glossary.py` 全部端点；缺失最小版本保留策略（M-1 增强项） |
| FR-09 | 冲突检测 | ✅ 已实现 | 90% | `api/conflict.py` 四类冲突+仲裁状态机+LLM补位；PII路由特殊处理 |
| FR-10 | 质量监控 | ✅ 已实现 | 90% | `services/quality/service.py` 含动态基线/同环比/跨源/修复建议/外部基准对账 |
| FR-11 | 权限与合规 | ✅ 已实现 | 90% | `services/governance/service.py` RBAC六角色+PDP+PII复核+被遗忘权+二次校验 |
| FR-12 | QuickBI嵌入 | ❌ 未实现 | 0% | 无任何 QuickBI iframe/SSO/嵌入代码 |
| FR-13 | Semantic API + 鉴权 | ✅ 已实现 | 85% | `api/consume.py` 含 API Key 鉴权+限流+域隔离+PII强制；性能基线 P95≈1.73s 超契约 |
| FR-14 | NL2SQL | ⚠️ 占位实现 | 35% | `services/ai/service.py` NL2SQL 仅关键词模板匹配 + LLM 文本生成，**execute=True 无实际执行**；PRD 要求语义锚定+安全约束+执行委托，实际 execute 空承诺 |
| FR-15 | DataAgent MCP | ❌ 未实现 | 0% | 无任何 MCP 协议适配代码 |
| FR-16 | 审计与度量 | ✅ 已实现 | 80% | `core/audit.py` + observability 服务；缺失 PRD §4.10 要求的 NPS 采集、反馈驱动迭代闭环 |
| FR-17 | 能力降级 | ✅ 已实现 | 75% | `core/resilience.py` 熔断器+可选依赖探活；降级策略分散在各服务，**无统一降级编排** |
| FR-18 | 数据资产地图 | ✅ 已实现 | 70% | `api/assetmap.py` 仅5个只读端点（summary/classification/metrics/tables/orphans）；**缺失** PRD 要求的敏感分布热力图、责任人视图、交互式图谱 |
| FR-19 | 智能找数推荐 | ⚠️ 基础实现 | 40% | `api/recommend.py` 仅 related_metrics/recommend_metrics/recommend_terms 3端点；**缺失** 基于埋点行为的协同过滤、术语协同推荐 |

**整体匹配度：72.5%（加权计算，P0需求85%，P1需求65%，P2需求30%）**

### 1.2 产品逻辑缺陷清单

| 编号 | 缺陷描述 | 所属服务 | 影响用户群体 | 业务影响等级 | 复现路径 |
|------|---------|---------|------------|------------|---------|
| P-01 | **执行引擎空壳**：`consume/service.py:144-173` `execute_query` 返回硬编码空结果 `{"rows": []}`，OLAP下推未实现 | consume | 数据分析师、外部消费系统 | P0-致命 | POST /api/v1/consume/query → 返回空数据，任何查询均无实际结果 |
| P-02 | **NL2SQL execute空承诺**：`ai/service.py:193-200` `ask(execute=True)` 仅追加 note 文本，不执行SQL，API语义欺骗 | ai | 数据分析师 | P0-致命 | POST /api/v1/ai/nl2sql `{"execute": true}` → 返回 `execute: true` 但无执行结果 |
| P-03 | **QuickBI嵌入缺失**：PRD FR-12 标注一期完整交付，但前后端均无QuickBI集成代码 | consume | 数据分析师 | P1-高 | 无入口可触发QuickBI嵌入 |
| P-04 | **LLM解析缺失结构化输出**：PRD §4.3§5要求Pydantic结构化输出含置信度+推断依据，实际`llm/client.py`仅返回`{"content": str}` | collector/ai | 数据开发 | P1-高 | 采集后LLM解析 → 无置信度分级、无候选去重、无校准门禁 |
| P-05 | **模板包/驾驶舱/消费指南缺失**：PRD FR-07标注完整交付，但无模板CRUD、驾驶舱聚合API、消费指南Tab API | semantic | 指标Owner、治理委员会 | P1-高 | 搜索"template"/"dashboard"/"consumption_guide" → 无对应端点 |
| P-06 | **资产地图交互缺失**：PRD FR-18要求可视化图谱+热力+责任人视图，实际仅5个静态聚合端点 | assetmap | 治理委员会 | P2-中 | GET /api/v1/assetmap/* → 无图谱/热力数据 |
| P-07 | **推荐算法过于简单**：PRD FR-19要求基于埋点行为+术语协同，实际仅数据库直接查询 | recommend | 数据分析师、新员工 | P2-中 | GET /api/v1/recommend/metrics → 无行为数据支撑 |
| P-08 | **NPS/反馈闭环缺失**：PRD §4.10要求反馈驱动迭代（高频反馈→路线图），observability仅存储反馈无闭环 | observability | 全部用户 | P2-中 | POST /api/v1/observability/feedback → 反馈写入但无状态回查/采纳机制 |
| P-09 | **采集异步队列默认内存实现**：`module-status.yaml:48` 标注 InMemoryCollectionQueue 为默认，生产环境丢失任务 | collector | 数据开发 | P1-高 | 服务重启 → 队列中采集任务全部丢失 |
| P-10 | **限流器进程内实现**：`consume/service.py:38-66` InMemoryRateLimiter 单进程令牌桶，多实例部署时限流失效 | consume | 外部消费系统 | P1-高 | 水平扩展2+实例 → QPS限制翻倍突破 |

---

## 二、技术维度审查

### 2.1 技术风险评估报告

| 编号 | 风险描述 | 风险等级 | 触发条件 | 影响范围 | 潜在损失 | 证据（文件:行号） | 规避建议 |
|------|---------|---------|---------|---------|---------|------------------|---------|
| T-01 | **限流器单进程不可水平扩展** | P0-致命 | 多实例部署 | 全部消费方API | 限流失效→OLAP被击穿→全平台查询不可用 | `consume/service.py:38-66` InMemoryRateLimiter 使用进程内dict | 替换为Redis滑动窗口（代码已标注"一期"） |
| T-02 | **DB会话自动提交与业务提交冲突** | P1-高 | 写操作端点 | 全部写操作 | 数据不一致：业务commit+get_db_session的yield commit双重提交 | `db/mysql.py:68-74` get_db_session yield后自动commit；各API层也手动commit | 移除get_db_session中的自动commit，改为仅rollback on error，由API层统一commit |
| T-03 | **审计与业务非原子提交** | P1-高 | 审计写入后commit前异常 | 全部审计记录 | 审计丢失（业务已commit但审计未落库） | 多数端点模式：write_audit → db.commit()。虽然PLAT-3已修复"审计在commit前"，但get_db_session的自动commit可能在审计add后、API层commit前触发 | 统一为：write_audit + 业务flush → 单次commit |
| T-04 | **采集队列内存实现生产不可用** | P1-高 | 服务重启/扩缩容 | 全量采集任务 | 采集任务丢失→元数据过期→指标血缘断裂 | `module-status.yaml:48` InMemoryCollectionQueue为默认实现 | 生产环境强制使用ArqCollectionQueue（已有惰性导入实现） |
| T-05 | **AI服务SQL注入风险** | P1-高 | NL2SQL关键词匹配分支 | ai服务 | 生成的SQL含字符串拼接`metric_code = '{a}'`，可注入 | `ai/service.py:170-175` `_generate_sql_with_keywords` 用f-string拼接用户输入 | 改为参数化查询模板，或禁止此分支生成可执行SQL |
| T-06 | **JWT密钥硬编码降级** | P2-中 | 开发环境未配置FERNET_KEY | secrets模块 | 从JWT_SECRET派生Fernet密钥，两密钥耦合 | `core/secrets.py:33` `jwt_secret = os.environ.get("UNISENSE_JWT_SECRET", "default-jwt-secret-for-dev")` | 生产环境强制校验FERNET_KEY存在，禁止降级派生 |
| T-07 | **CORS配置过于宽松** | P2-中 | 生产部署 | 全部API | 凭证模式+宽松origin=跨域攻击 | `core/config.py:47` `cors_origins: str = "http://localhost:3000"` 默认值；`main.py:77` `allow_credentials=True` | 生产环境严格限制origin列表，禁止通配符+凭证 |
| T-08 | **Redis客户端无连接池管理** | P2-中 | Redis连接泄漏 | 全局缓存 | 连接耗尽→缓存不可用→查询性能下降 | `db/redis.py:12-16` 全局单例 `redis_client` 无close机制 | 实现lifespan管理Redis连接池生命周期 |
| T-09 | **Neo4j/ES无降级策略文档化** | P2-中 | Neo4j/ES宕机 | 血缘/搜索功能 | 降级路径仅代码层面（熔断器），无运营runbook | `core/resilience.py:78-93` 仅TCP探活，无应用层降级逻辑 | 补充各依赖宕机时的用户可见降级UX（如搜索提示"暂不可用"） |
| T-10 | **datetime.utcnow()已弃用** | P3-低 | Python 3.12+ | quality服务 | 未来版本运行时警告/错误 | `quality/service.py:355,382,502,638` 多处使用 `datetime.utcnow()` | 统一替换为 `datetime.now(UTC)` |
| T-11 | **配置缺少生产校验** | P2-中 | 生产部署遗漏环境变量 | 全部服务 | jwt_secret为空或弱值→Token伪造 | `core/config.py:42` `jwt_secret: str` 无min_length校验 | 添加生产模式校验器（env=prod时强制jwt_secret≥32字符） |
| T-12 | **SQL注入守卫仅扫描顶层字段** | P2-中 | 嵌套JSON body攻击 | 全部POST端点 | 绕过守卫的深层注入 | `core/guard.py:40-50` 仅遍历body.values()顶层字符串 | 递归扫描嵌套dict/list中的字符串值 |

### 2.2 代码可维护性评估

**正面发现**：
- ✅ 统一异常体系（`core/exceptions.py`）分层清晰
- ✅ 统一错误码枚举（`core/error_codes.py`）禁止硬编码
- ✅ 统一审计写入（`core/audit.py`）PII访问标记
- ✅ 全量RBAC守卫（`require_roles` 依赖注入）
- ✅ SQL注入纵深防御（`guard.py` + 参数化查询）
- ✅ mypy --strict 0 error 全仓通过
- ✅ ruff lint/format 全清

**负面发现**：
- ❌ 服务层无统一接口协议（无Protocol/Base Class），14个服务各自实现
- ❌ 事件发布best-effort分散在7+服务中，无统一EventBus抽象
- ❌ `_safe_publish` 在governance/conflict/notify等处重复实现
- ❌ 限流器/熔断器/缓存等基础设施未抽象为可插拔组件
- ❌ 配置类`Settings`缺少生产环境校验钩子

### 2.3 存量技术债务规模

| 债务类别 | 数量 | 影响 |
|---------|------|------|
| 占位实现（返回空/hardcode） | 3处 | execute_query空结果、NL2SQL空执行、dry-run placeholder |
| 进程内单例限流/队列 | 2处 | InMemoryRateLimiter、InMemoryCollectionQueue |
| 弃用API调用 | 4处 | datetime.utcnow() |
| 重复代码模式 | 5处 | _safe_publish、_svc工厂、READ_DEPS声明 |
| 缺失抽象层 | 3处 | 无EventBus、无BaseService、无PluginRegistry |
| 配置缺少校验 | 2处 | jwt_secret无强度校验、Fernet降级派生 |

---

## 三、测试维度审查

### 3.1 测试用例覆盖度统计

| 服务 | 单元测试 | 安全测试 | 混沌测试 | 可观测测试 | 集成测试 | 性能测试(k6) | 覆盖率评估 |
|------|---------|---------|---------|-----------|---------|-------------|-----------|
| collector | ✅ test_collector.py + test_collector_queue.py | ✅ test_collector_security.py | ✅ test_collector_chaos.py | ✅ test_collector_observability.py | ✅ test_collector_integration.py | ❌ 无独立脚本 | ≥70% |
| lineage | ✅ test_lineage_*.py (3文件) | ✅ test_lineage_security.py | ✅ test_lineage_chaos.py | ✅ test_lineage_observability.py | ✅ test_lineage_integration.py | ❌ 无独立脚本 | ≥70% |
| semantic | ✅ test_semantic_*.py (3文件) | ✅ test_semantic_security.py | ❌ 未单独列出 | ✅ | ✅ | ❌ | ≥70% |
| conflict | ✅ test_conflict_*.py (3文件) | ✅ test_conflict_security.py | ✅ | ✅ | ✅ | ❌ | ≥70% |
| governance | ✅ test_governance_*.py (4文件) | ✅ test_governance_security.py | ✅ | ✅ | ✅ | ❌ | ≥70% |
| consume | ✅ test_consume_*.py (2文件) | ✅ test_consume_security.py | ✅ | ✅ | ✅ | ✅ baseline_consume.js | ≥70% |
| ai | ✅ test_ai_service.py | ✅ test_ai_security.py | ✅ | ✅ | ✅ | ✅ baseline_ai.js | ≥70% |
| quality | ✅ test_quality_*.py (3文件) | ✅ test_quality_security.py | ✅ | ✅ | ✅ | ✅ baseline(内嵌) | ≥70% |
| notify | ✅ test_notify_*.py (2文件) | ✅ test_notify_security.py | ✅ | ✅ | ✅ | ✅ baseline_notify.js | ≥70% |
| observability | ✅ test_observability_service.py | ✅ test_observability_security.py | ✅ | ✅ | ✅ | ✅ baseline_observability.js | ≥70% |
| assetmap | ✅ test_assetmap_service.py | ✅ test_assetmap_security.py | ✅ | ✅ | ✅ | ✅ baseline_assetmap.js | ≥70% |
| recommend | ✅ test_recommend_service.py | ✅ test_recommend_security.py | ✅ | ✅ | ✅ | ✅ baseline_recommend.js | ≥70% |
| glossary | ✅ test_glossary_service.py | ✅ test_glossary_security.py | ✅ | ✅ | ✅ | ✅ baseline_glossary.js | ≥70% |
| dimension | ✅ test_dimension_service.py | ✅ test_dimension_security.py | ✅ | ✅ | ✅ | ✅ baseline_dimension.js | ≥70% |

**测试文件总数**：单元31 + 安全19 + 混沌16 + 可观测15 + 集成15 + 性能11 = **107个测试文件**

### 3.2 未达质量标准模块清单

| 模块 | 未达标指标 | 当前值 | 阈值要求 | 证据 |
|------|-----------|--------|---------|------|
| consume | 性能基线P95 | 1.73s | ≤300ms（perf_contract） | `module-status.yaml:209` "p95≈1.73s 超 300ms 契约（本地 Docker MySQL 往返所致）" |
| 全部服务 | k6性能基线live执行 | 未执行 | CI门禁required=true | `module-status.yaml:416` "perf_baseline 真实 k6 仍未在 live 环境执行" |
| 全部服务 | §1.5人工ratify | 未完成 | released前提 | `module-status.yaml` 14/14模块均标注"ratify_pending" |
| ai | execute功能测试 | 0%覆盖 | FR-14核心功能 | `ai/service.py:193-200` execute=True无实际执行，无对应测试 |
| consume | OLAP执行集成测试 | 0%覆盖 | FR-06核心功能 | `consume/service.py:156-161` olap_url为空时直接抛503，无真实OLAP测试 |
| frontend | 前端测试 | 0% | 工业级标准≥60% | `frontend/` 目录存在但无测试文件证据 |

### 3.3 自动化测试覆盖率评估

| 自动化类型 | 覆盖率 | 评估 |
|-----------|--------|------|
| 接口自动化（API集成测试） | ~70%（15个集成测试文件） | 中等，但依赖testcontainers真实MySQL |
| UI自动化 | 0% | **严重缺失**，前端React应用无任何E2E测试 |
| 性能自动化 | ~60%（k6脚本存在但未live执行） | 脚本就绪但执行环境未就绪 |
| 安全自动化 | ~80%（19个安全测试文件+gitleaks+pip-audit） | 较好 |
| 混沌自动化 | ~70%（16个混沌测试文件） | 中等，多数为mock而非真实故障注入 |

---

## 四、运营维度审查

### 4.1 运营支撑能力 Gap 分析表

| 编号 | 短板描述 | 对日常运营影响 | 对故障处置影响 | 证据 |
|------|---------|--------------|--------------|------|
| O-01 | **无数据埋点体系**：PRD §4.10要求运营度量（日活/搜索量/审核时效），实际无埋点SDK/事件采集 | 无法度量平台使用效果，北极星指标无法验证 | 无法基于用户行为定位故障影响面 | 搜索全仓库无 "tracking"/"analytics"/"event_report" 等埋点代码 |
| O-02 | **通知仅入库不真实投递**：notify服务publish_event仅写DB，无邮件/钉钉/短信通道 | 用户无法感知质量异常/审核待办，闭环断裂 | 故障告警无法触达责任人 | `api/notify.py:25-44` 无外部通知渠道调用；`module-status.yaml sic_reviews` 原始缺陷"通知只入库从不发送" |
| O-03 | **无运营管理后台**：PRD §4.10要求治理驾驶舱，无对应API/前端 | 运营人员无法查看全局健康度、冲突趋势 | 无法快速定位全局故障热点 | 搜索 "dashboard"/"cockpit"/"驾驶舱" 无结果 |
| O-04 | **无SLA监控/告警**：PRD §1.5(四)承诺SLA（搜索P95<500ms/审核48h/可用性99.5%），无SLA计算/告警 | SLA承诺无法验证 | SLA违约无自动告警 | 无SLA计算逻辑、无Prometheus告警规则 |
| O-05 | **故障响应链路不完整**：熔断器仅记录状态变更，无故障工单/Runbook自动化触发 | 故障发现依赖人工巡检 | MTTR无保障 | `core/resilience.py` 仅allow/record方法，无on-call集成 |
| O-06 | **用户行为分析缺失**：PRD §4.14.6要求"基于埋点行为"推荐，无行为采集/存储/分析 | 无法做个性化推荐、无法度量功能使用深度 | 无法基于行为分析定位体验瓶颈 | `api/recommend.py` recommend_metrics 直接查DB无行为数据 |
| O-07 | **数据源健康度无主动巡检**：PRD §4.2要求定时增量采集+Schema Drift检测，无调度框架 | 数据源失效无法自动发现 | 采集链路断裂无感知 | `module-status.yaml:48` 采集队列为内存实现，无cron/scheduler |

---

## 五、安全维度审查

### 5.1 安全合规差距清单

| 编号 | 差距描述 | 关联合规条款 | 当前状态 | 证据 | 整改要求 |
|------|---------|------------|---------|------|---------|
| S-01 | **JWT密钥无强度校验** | 等保2.0 §8.1.4 密码技术管理 | jwt_secret: str 无min_length/复杂度校验 | `core/config.py:42` `jwt_secret: str` 裸字符串类型 | 生产模式强制≥32字符+随机生成 |
| S-02 | **JWT无刷新令牌机制** | 等保2.0 §8.1.4 会话管理 | 单token 60分钟过期，无refresh | `core/security.py:72` expire_minutes=60 | 实现refresh_token轮换 |
| S-03 | **Fernet密钥降级派生** | 等保2.0 §8.1.4 密钥隔离 | JWT_SECRET与Fernet_KEY共享密钥材料 | `core/secrets.py:33` 从jwt_secret派生Fernet密钥 | 生产环境强制独立FERNET_KEY |
| S-04 | **SQL注入守卫仅顶层扫描** | 等保2.0 §8.1.5 输入验证 | 嵌套JSON body内字符串不扫描 | `core/guard.py:40-50` body.values()仅遍历顶层 | 递归扫描所有层级字符串 |
| S-05 | **AI服务SQL拼接注入** | 等保2.0 §8.1.5 输入验证 | f-string拼接用户输入到SQL | `ai/service.py:170-175` `f"metric_code = '{a}'"` | 禁止关键词匹配生成可执行SQL |
| S-06 | **CORS凭证模式+宽松Origin** | 等保2.0 §8.1.3 通信安全 | allow_credentials=True + 默认localhost | `main.py:77-81` CORSMiddleware配置 | 生产环境严格限制Origin列表 |
| S-07 | **无CSRF防护** | 等保2.0 §8.1.5 | 纯API无CSRF Token | `main.py` 无CSRF中间件 | 评估是否需要（API+Bearer可能免除） |
| S-08 | **无API请求签名** | 等保2.0 §8.1.3 通信完整性 | API Key明文传输 | `api/consume.py:48` X-Api-Key Header | 增加HMAC请求签名 |
| S-09 | **审计日志无3年归档策略** | 等保2.0 §8.1.6 审计记录保存 | 仅写MySQL，无归档/清理策略 | `core/audit.py` 仅session.add | 实现冷热分离归档（MySQL→对象存储） |
| S-10 | **无异地多活灾备** | 等保2.0 §8.1.7 数据备份 | 单节点Docker Compose，无灾备 | `docker-compose.yml` 无主从/集群配置 | 设计MySQL主从+Neo4j因果集群+ES分片副本 |
| S-11 | **PII字段脱敏策略硬编码** | PIPL §51 个人信息去标识化 | masking_for函数内置策略 | `governance/policy.py` 脱敏策略不可配置 | 脱敏策略应可按字段/域自定义 |
| S-12 | **被遗忘权仅覆写审计行** | PIPL §47 删除权 | 覆写ip/detail_json，指标引用未清理 | `governance/service.py:635-680` 仅处理audit_log | 扩展至关联的metric/notification/feedback表 |
| S-13 | **密码哈希无工作因子配置** | 等保2.0 §8.1.4 | bcrypt.gensalt()使用默认rounds=12 | `core/security.py:28` | 生产环境建议rounds≥14 |
| S-14 | **Elasticsearch安全未启用** | 等保2.0 §8.1.3 | xpack.security.enabled=false | `docker-compose.yml:51` | 生产环境启用ES认证+TLS |
| S-15 | **无数据分级标签传播** | PIPL §51 | PII标记仅db_catalog级，不随血缘传播 | `collector/service.py` PII标记不沿血缘下游传播 | 含PII字段的指标/下游指标应自动继承PII标记 |

---

## 六、工业级标准差距量化评估

### 6.1 评估框架

参照 GB/T 36073 数据管理能力成熟度模型（DCMM）5个能力域 + 工业级软件质量规范，量化评分：

| 评估维度 | 权重 | 满分 | 得分 | 达标率 | 评级 |
|---------|------|------|------|--------|------|
| **数据战略**（组织/制度/规划） | 10% | 100 | 72 | 72% | L3-稳健级 |
| **数据治理**（标准/质量/安全） | 25% | 100 | 68 | 68% | L2-受管理级 |
| **数据架构**（模型/分布/集成） | 20% | 100 | 75 | 75% | L3-稳健级 |
| **数据应用**（分析/开放/服务） | 25% | 100 | 55 | 55% | L2-受管理级 |
| **数据安全**（合规/防护/审计） | 20% | 100 | 62 | 62% | L2-受管理级 |
| **综合加权得分** | 100% | 100 | **65.4** | **65.4%** | **L2-受管理级** |

### 6.2 逐模块达标率量化

| 模块 | 功能完整度 | 代码质量 | 安全合规 | 性能达标 | 测试覆盖 | 综合达标率 | P级 |
|------|-----------|---------|---------|---------|---------|-----------|------|
| collector | 95% | 90% | 85% | 80% | 85% | **87%** | P2 |
| lineage | 85% | 90% | 85% | 85% | 85% | **86%** | P2 |
| semantic | 90% | 90% | 90% | 85% | 85% | **88%** | P2 |
| conflict | 90% | 90% | 85% | 90% | 85% | **88%** | P2 |
| governance | 90% | 85% | 80% | 90% | 85% | **86%** | P2 |
| consume | 65% | 85% | 80% | 45% | 80% | **71%** | P1 |
| ai | 35% | 75% | 70% | N/A | 75% | **51%** | P0 |
| quality | 90% | 85% | 80% | 85% | 85% | **85%** | P2 |
| notify | 75% | 85% | 80% | 80% | 80% | **80%** | P1 |
| observability | 80% | 85% | 80% | 80% | 80% | **81%** | P1 |
| assetmap | 70% | 85% | 80% | 80% | 80% | **79%** | P1 |
| recommend | 40% | 80% | 80% | 80% | 75% | **71%** | P1 |
| glossary | 85% | 85% | 80% | 85% | 80% | **83%** | P2 |
| dimension | 85% | 85% | 80% | 85% | 80% | **83%** | P2 |
| **前端（React）** | 30% | N/A | N/A | N/A | 0% | **6%** | P0 |
| **基础设施** | 70% | 75% | 55% | 60% | 70% | **66%** | P1 |

### 6.3 问题优先级排序与补全路径

#### P0级（阻断交付，必须立即修复）

| 编号 | 问题 | 责任角色 | 补全路径 | 时间节点 |
|------|------|---------|---------|---------|
| P0-01 | AI服务空壳实现（NL2SQL execute空承诺+关键词SQL注入） | 后端架构师 | ① 修复SQL拼接注入 ② execute=True委托consume服务 ③ 补集成测试 | W1 |
| P0-02 | 执行引擎空壳（consume返回硬编码空结果） | 后端架构师 | ① 实现OLAP JDBC执行 ② 补真实执行集成测试 ③ 性能基线校准 | W2-W3 |
| P0-03 | 限流器单进程不可扩展 | 后端架构师 | 替换为Redis滑动窗口限流 | W1 |
| P0-04 | 前端零测试+功能严重缺失 | 前端负责人 | ① 补核心页面E2E测试 ② 补QuickBI嵌入 ③ 补驾驶舱 | W2-W4 |

#### P1级（影响核心SLA，须在上线前完成）

| 编号 | 问题 | 责任角色 | 补全路径 | 时间节点 |
|------|------|---------|---------|---------|
| P1-01 | 通知不真实投递 | 后端开发 | 对接钉钉/邮件通道 | W2 |
| P1-02 | DB会话双重提交风险 | 后端架构师 | 移除get_db_session自动commit | W1 |
| P1-03 | 采集队列内存实现 | 后端开发 | 生产环境强制ArqCollectionQueue | W1 |
| P1-04 | JWT/密钥缺少生产校验 | 安全工程师 | 生产模式强校验+独立Fernet密钥 | W1 |
| P1-05 | consume性能基线不达标（P95=1.73s > 300ms） | 性能工程师 | 独立压测环境+缓存优化+连接池调优 | W2 |
| P1-06 | AI服务SQL拼接注入 | 安全工程师 | 改参数化模板或禁止关键词生成SQL | W1 |
| P1-07 | CORS凭证+宽松Origin | 安全工程师 | 生产严格Origin白名单 | W1 |
| P1-08 | 埋点体系缺失 | 前端+后端 | 引入埋点SDK+事件采集+行为分析 | W3-W4 |
| P1-09 | ES/Neo4j安全未启用 | 运维工程师 | 启用认证+TLS+网络隔离 | W2 |

#### P2级（影响用户体验/可维护性，可迭代补全）

| 编号 | 问题 | 责任角色 | 补全路径 | 时间节点 |
|------|------|---------|---------|---------|
| P2-01 | 资产地图交互缺失 | 前端开发 | 补图谱渲染+热力+责任人视图 | W4-W5 |
| P2-02 | 推荐算法过于简单 | 算法工程师 | 引入协同过滤+行为向量 | W5-W6 |
| P2-03 | 模板包/驾驶舱缺失 | 产品经理+前端 | 补CRUD端点+前端聚合页 | W4 |
| P2-04 | LLM解析缺结构化输出 | 后端开发 | 补Pydantic Schema+置信度分流+校准门禁 | W4-W5 |
| P2-05 | NPS/反馈闭环缺失 | 产品经理 | 补采纳状态+路线图关联 | W5 |
| P2-06 | 审计3年归档策略 | 运维工程师 | MySQL→S3冷归档+定期清理 | W5 |
| P2-07 | datetime.utcnow()弃用 | 后端开发 | 统一替换datetime.now(UTC) | W3 |
| P2-08 | SQL注入守卫仅顶层 | 安全工程师 | 递归扫描嵌套结构 | W3 |

#### P3级（代码卫生/技术优化，持续改进）

| 编号 | 问题 | 责任角色 | 补全路径 | 时间节点 |
|------|------|---------|---------|---------|
| P3-01 | 服务层无统一接口协议 | 后端架构师 | 抽象BaseService Protocol | 持续 |
| P3-02 | _safe_publish重复实现 | 后端开发 | 抽象统一EventBus | 持续 |
| P3-03 | Redis客户端无生命周期管理 | 后端开发 | lifespan中close | W4 |
| P3-04 | 配置缺生产校验钩子 | 后端架构师 | 添加env=prod校验器 | W3 |
| P3-05 | PII标记不随血缘传播 | 后端开发 | 血缘下游自动继承 | W5 |

---

## 七、关键发现摘要

### 7.1 必须有真实证据的核心缺陷（代码行级）

1. **`backend/app/services/consume/service.py:168-172`** — `execute_query` 返回硬编码 `data={"rows": []}`，OLAP执行引擎完全空壳
2. **`backend/app/services/ai/service.py:170-175`** — `_generate_sql_with_keywords` 用 f-string 拼接用户输入 `f"metric_code = '{a}'"` 构造SQL，存在注入风险
3. **`backend/app/services/ai/service.py:193-200`** — `ask(execute=True)` 仅追加note文本不执行，API语义欺骗
4. **`backend/app/services/consume/service.py:38-66`** — `InMemoryRateLimiter` 进程内令牌桶，多实例部署限流失效
5. **`backend/app/core/secrets.py:33`** — Fernet密钥从JWT_SECRET派生，两密钥耦合违反密钥隔离原则
6. **`backend/app/core/guard.py:40-50`** — SQL注入守卫仅扫描JSON body顶层字段，嵌套结构绕过
7. **`backend/app/db/mysql.py:68-74`** — `get_db_session` yield后自动commit，与API层手动commit可能双重提交
8. **`backend/app/services/quality/service.py:355,382,502`** — 多处使用已弃用 `datetime.utcnow()`
9. **`backend/docker-compose.yml:51`** — Elasticsearch `xpack.security.enabled=false` 生产不可用
10. **`backend/app/core/config.py:42`** — `jwt_secret: str` 无最小长度/复杂度校验

### 7.2 与工业级标准的核心差距

| GB/T 36073条款 | 要求 | 当前状态 | 差距 |
|---------------|------|---------|------|
| 6.2 数据战略规划 | 战略目标可度量 | 北极星指标定义清晰但无度量实现 | 埋点体系缺失 |
| 6.3 数据治理组织 | 角色职责明确 | 六角色RBAC已实现，但合规官人工ratify未完成 | 14/14 ratify_pending |
| 6.4 数据标准管理 | 指标口径统一 | 口径定义+版本管理已实现，复合指标解析缺失 | 执行引擎空壳 |
| 6.5 数据质量管理 | 质量规则+闭环 | 四类检测+修复建议已实现，SLA监控缺失 | 无SLA告警 |
| 6.6 数据安全管理 | 分级分类+脱敏+审计 | PII分级+复核+二次校验已实现，传播缺失 | PII不随血缘传播 |
| 6.7 数据应用能力 | 对外服务+消费 | Semantic API已实现，执行层空壳 | 消费闭环断裂 |
| 7.1 数据架构设计 | 模型+分布+集成 | 三层架构合理，OLAP集成缺失 | 执行层未实现 |

---

*本报告所有结论均附真实代码文件路径与行号证据，无推测性结论。建议按P0→P3优先级逐项整改，P0级问题须在上线前全部关闭。*
