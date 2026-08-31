# Unisense — 企业级指标语义中台

> **一句话定位**：面向数据分析师、数据开发与治理者的企业级指标语义中台，让"每一个指标都有唯一可信的口径，且可被人与 AI 自助消费"。

---

## 一、项目背景与目标

### 1.1 业务背景与痛点

公司数据分析依赖大量指标，但指标口径长期分散、缺乏统一治理，导致：

| 痛点 | 表现 |
|------|------|
| **口径不统一** | 同一指标（如「活跃用户」「GMV」）在不同报表、不同团队计算逻辑各异，跨部门对不齐 |
| **口径黑盒** | 指标定义散落在 ETL 代码、Excel、邮件中口头传承，新人无从查证，老员工离职即失传 |
| **重复建设** | 同一口径在多处重复实现，修改一处漏一处，一致性无法保障 |
| **可信度缺失** | 数据异常时无人知晓，报表照常产出，基于不可信数据做决策 |
| **AI 不可用** | DataAgent 等 AI 能力无法直接消费结构化口径，只能"猜 SQL"，错误率高 |

### 1.2 产品目标

1. **统一语言**：让每一个指标有唯一可信的口径定义，且可被人与 AI 自助消费
2. **治理闭环**：指标从注册、审核、发布到废弃全生命周期有主、有门禁、可追溯
3. **降本提效**：显著降低分析师定位/确认口径耗时，减少重复 ETL 开发
4. **AI 就绪**：输出高度结构化的语义定义，为 DataAgent 提供可直接消费的 API

### 1.3 产品原则（设计判据）

1. **治理优先于功能堆砌**：先有口径所有权与门禁，再谈消费能力；无 Owner 不发布
2. **自助优先于求助**：用户应能自己搜到、确认、消费口径，而非依赖"问老员工"
3. **可信优先于覆盖**：宁肯少而准，不可多而乱；质量异常须透明可见
4. **AI 是消费侧延伸，不是主体**：LLM 用于加速录入与解析，决策与仲裁仍由人负责
5. **克制范围**：只做治理 + 语义 + 消费闭环，不重造调度 / BI / LLM

> 完整背景、价值假设与量化验收标准见 `docs/proposal.md` §1 / §9。

---

## 二、项目状态

**生产级全面落地，可单机 Docker Compose 生产部署。**

| 维度 | 现状 |
|------|------|
| 领域模块 | **20 个**交付模块（`docs/module-status.yaml` 追踪）：核心 16 个（**15 released** + 1 verified）+ P2/P3 增强 4 个（implemented） |
| 后端 | 23 个服务目录 / 34 个 API 模块 / **117 个**数据库迁移（当前 head `0117`） |
| 前端 | **41 个**页面 + 14 个共享组件 |
| 测试 | 后端 **258 个**测试文件（unit / integration / security / chaos / perf / observability，上次全量约 4000 项通过）+ 前端 **62 个**测试文件（约 1100 项通过） |
| 质量门禁 | **13 道** CI 强制门禁（`.github/workflows/gateways.yml`） |
| 部署 | 单机 Docker Compose，11 个服务（含全量备份 + binlog 增量备份 + 审计冷归档） |

> 模块状态以 `docs/module-status.yaml` 为**单一事实源**；变更审计链见 `docs/CHANGELOG_MODULES.md`。

---

## 三、核心能力

### 3.1 指标全生命周期

注册（手动向导 / **SQL 智能推断批量解析**）→ 冲突预检 → 审批 → 发布 → 挂载变体 → 消费 → 废弃 / 回收站（恢复 / 彻底删除 / **批量彻底删除**）。

- **统一审批中心**（`/approval`）：指标审批 + 主数据审批 + 冲突仲裁**三合一**，`?tab=` 深链直达
- **主数据审批**：维度 / 原子指标口径库（逻辑度量）/ 术语聚合，支持按类型聚焦筛选
- **指标目录**：按状态、域、分级、责任人、**有无下游引用**筛选；批量提交 / 通过 / 驳回 / 废弃 / 删除（草稿 + 已废弃）

### 3.2 主数据治理

- **原子指标口径库**（原「度量目录」，OneData 原子层）：三层口径——**业务口径 / 技术口径（源业务库口径SQL）/ 数仓SQL口径**，各层支持 LLM 生成与增强
- **维度**、**术语**、**主题域**管理（统一 `MasterDataReview` 审核流与 `MasterDataBatch` 批量操作）
- **系统字典**：度量分类、度量格式、源头系统等**字典化在线管理**（含 LLM 描述推断）

### 3.3 数据采集与目录

- 多源采集：MySQL / PostgreSQL / Hive / Spark / Doris / ClickHouse / Kafka / StarRocks / **Hive Metastore**
- 采集目录：表 / 字段元数据、探活、批量废弃、**描述缺失治理**（LLM 推断补全）

### 3.4 血缘追踪

G6 v5 血缘图谱，上游来源 / 下游影响 / 双向分析、断链检测。

### 3.5 全局搜索

**9 类资源**：指标 / 维度 / 术语 / 模板 / 数据源 / 采集目录表 / 采集目录字段 / 主题域 / 度量目录。
中英同义词**双路**——ES `cn_en_synonym` 分词 + MySQL 关键词扩展（搜「订单」命中 `sales_order`）。

### 3.6 合规治理

PII 合规复核（**指标 + 目录资产双口径**）、敏感规则、WORM 审计日志（MinIO 冷热归档 + **哈希链**防篡改）、被遗忘权。

### 3.7 质量与可观测

质量巡检、指标健康度、依赖健康探针、混沌韧性测试、OpenTelemetry + Prometheus 指标。

### 3.8 消费

指标查询工作台（OLAP 下推 / MySQL 降级引擎）、QuickBI 嵌入、API 客户端凭证。

### 3.9 AI 能力

多 LLM 网关路由（含熔断器 / failover / **异常流式内容识别**），本地**无需 GPU**。
SQL 智能推断（建表注释驱动命名 / 维度提取 / 三层口径）、描述 / 同义词 / 分类推断。

### 3.10 用户与权限

多角色 RBAC：平台管理员 / 域管理员 / 指标负责人 / 评审员 / 合规官 / 分析师 / 查看者；用户可持**多角色**（权限并集）。

### 3.11 对外集成

OpenAPI（Swagger，`/docs` 经前端 8180 直达）、`docs/API_INTEGRATION.md` 对接指南、批量导入（**JSON / CSV / Excel**）、指标模板实例化。

---

## 四、技术栈

| 层 | 技术 |
|------|------|
| **后端** | Python 3.11 · FastAPI 0.115 · SQLAlchemy 2.0 (async) · Pydantic v2 · Alembic · **arq**（异步任务 worker）· structlog · OpenTelemetry |
| **存储** | **MySQL 8**（主库）+ **Neo4j 5**（血缘图）+ **Elasticsearch 8.15**（搜索索引）+ **Redis 7**（队列 / SSE / 熔断 / 限流）+ **MinIO**（审计归档，S3 兼容） |
| **前端** | React 18.3 · TypeScript 5.6 · Vite 5.4 · antd 5.22 · **@antv/g6 5.1**（血缘图）· zustand 5 · @ant-design/charts 2.6 |
| **SQL 解析** | sqlglot 25 · sqlparse（血缘 / SQL 智能推断，跨 Hive / Spark / Doris / MySQL 方言） |
| **AI** | 多 LLM 网关路由（opencode / hy3 / kilo / **火山方舟 Agent Plan** 等 OpenAI 兼容端点），本地无需 GPU |
| **质量** | pytest（unit / integration / security / chaos / perf）+ vitest + Playwright E2E + mypy + ruff + pip-audit + gitleaks |

---

## 五、系统架构

```
                          ┌──────────────────────────────────────┐
   浏览器 ───────────────▶│  frontend (nginx :8180)              │
                          │  React SPA + /api /docs /openapi 反代 │
                          └────────────────┬─────────────────────┘
                                           │ /api/v1
                          ┌────────────────▼─────────────────────┐
                          │  backend (FastAPI :8100)             │
                          │  uvicorn（单进程，可按需加 worker）   │
                          │  ├─ 23 个领域服务（services/）        │
                          │  ├─ RBAC + PDP 鉴权 + 注入守卫        │
                          │  └─ 审计（WORM）+ 降级注册表          │
                          └──┬────────┬────────┬────────┬────────┘
                             │        │        │        │
        ┌────────────────────▼──┐  ┌──▼───┐ ┌──▼───┐ ┌──▼──────────┐
        │ MySQL 8               │  │Neo4j │ │ ES 8 │ │ Redis 7     │
        │ 指标/目录/审计/用户    │  │血缘图│ │搜索  │ │队列/SSE/熔断│
        └───────────────────────┘  └──────┘ └──────┘ └──────┬──────┘
                    ▲                                        │
                    │ binlog                          ┌──────▼──────┐
        ┌───────────┴────────┐                        │ worker(arq) │
        │ backup + binlog    │                        │ 采集/归档/  │
        │ 全量7天 + 增量5min │                        │ 巡检/清理   │
        └────────────────────┘                        └──────┬──────┘
                                                             │
                                                      ┌──────▼──────┐
                                                      │ MinIO       │
                                                      │ 审计冷归档  │
                                                      └─────────────┘
```

**架构要点**

- **无状态应用层**：backend 与 worker 均**无本地状态**，可水平扩展；会话与限流状态在 Redis
- **SSE 跨进程**：通知实时推送走 **Redis pub/sub**（禁用进程内内存推送），保证多实例间可靠广播
- **降级链**：LLM / ES / Neo4j / OLAP / Redis 任一不可用时核心链路**降级而非 500**（降级事件入 `degradation_event` 表）
- **WORM 审计**：审计日志只增不改，30 天后由 worker 归档为 JSONL 至 MinIO（含 **SHA-256 哈希链**）后清理热表行
- **合规闭环**：写入路径统一 `write_audit`（actor / IP / trace_id），审计动作中文模板由 `core/audit_i18n.py` 覆盖

---

## 六、项目结构

```
Unisense/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI 应用入口（路由注册 / 中间件 / 生命周期）
│   │   ├── api/                    # 34 个 API 模块（路由层，仅做鉴权 + 参数校验 + 编排）
│   │   ├── services/               # 23 个领域服务（业务逻辑，repository + schemas + service 三层）
│   │   ├── models/                 # ORM 模型（34 个模型模块）
│   │   ├── core/                   # 基础设施：config / security / guard(注入守卫) / ssrf /
│   │   │                           #   secrets(Fernet) / eventbus / resilience(熔断) / audit / es_client
│   │   ├── tasks/                  # arq 异步任务（采集 / 归档 / 巡检 / 清理 / 锁）
│   │   └── db/                     # 数据库会话与 Base
│   ├── alembic/versions/           # 117 个数据库迁移（head 0117）
│   ├── scripts/                    # backup.sh 等运维脚本
│   └── tests/                      # unit / integration / security / chaos / perf / observability / eval
├── frontend/
│   └── src/
│       ├── pages/                  # 41 个页面
│       ├── components/             # 14 个共享组件（Layout / MasterDataReview / SchemaTable 等）
│       ├── api.ts                  # 统一 API 封装（401 重放 / 超时 / 错误归一化）
│       ├── types.ts                # 前后端契约类型
│       └── utils/                  # 工具（metricCode 校验 / 时间格式化等）
├── docs/                           # PRD / TD(20章) / DEV_GUIDE / 模块状态 / 对接指南 等
├── scripts/                        # contract_check.py（契约+文档同步校验）等
├── docker-compose.yml              # 11 个服务编排（含资源限制 / 备份 / 归档）
├── Makefile                        # 开发 & 门禁命令入口
└── CI/.gateways.yml                # 门禁门槛定义（protected，禁止开发绕过）
```

---

## 七、领域模块作用

以 `docs/module-status.yaml` 为事实源。**核心模块 16 个**（15 released + 1 verified）：

| 模块 | 职责 | 关键 TD 章节 |
|------|------|-------------|
| **collector** | 多源元数据采集、数据源管理、描述缺失治理 | §12.1 |
| **lineage** | SQL 解析血缘、图谱存储与查询、断链检测 | §12.2 |
| **semantic** | 指标注册 / 审批 / 发布 / 废弃、口径定义、SQL 智能推断 | §12.3 |
| **conflict** | 指标冲突检测、仲裁、升级与关闭 | §12.4 |
| **governance** | 治理策略、PII 合规、被遗忘权、RBAC 策略(PDP) | §12.5 |
| **users** | 用户与多角色 RBAC、组织、认证 | §12.5 |
| **consume** | 指标查询工作台、OLAP 下推与 MySQL 降级 | §12.6 |
| **ai** | LLM 网关路由、配置管理、推断编排 | §12.7 |
| **quality** | 质量巡检、健康度、依赖探针 | §12.8 |
| **notify** | 站内通知、SSE 推送、邮件外发 | §12.9 |
| **observability** | 指标暴露、降级事件、混沌韧性 | §12.10 / §16 |
| **assetmap** | 资产地图总览、多维度统计、血缘视图 | §12.11 |
| **recommend** | 指标与模板推荐 | §12.12 |
| **glossary** | 业务术语管理与审核 | §12.14 |
| **dimension** | 维度管理、成员、映射与对账 | §12.15 |
| **frontend** | 前端应用（41 页面） | §12.3 |

**P2/P3 增强模块 4 个**（status: implemented）：

| 模块 | 职责 |
|------|------|
| **subject_domain** | 主题域树管理与默认域 |
| **system_dict** | 系统字典 / 参照数据（度量分类、度量格式、源头系统等） |
| **auto_fill** | 指标自动填充建议（auto-suggest）与 SQL 画像推断 |
| **audit_remediation** | 审计整改追踪 |

另有跨切面服务：`search`（全局搜索编排）、`global_search`、`llm`（LLM 客户端与路由）、`master_data_review`（主数据审核共享 mixin）、`metric_mount`（指标挂载变体）、`measure_catalog`（原子指标口径库）、`sensitive_rules`（敏感规则）。

---

## 八、快速开始（开发环境）

```bash
# 1. 配置环境变量
cp .env.example .env        # 编辑填写实际值（JWT 密钥 / DB 密码 / LLM 配置等）

# 2. 启动全部服务（MySQL / Neo4j / ES / Redis / MinIO / backend / worker / frontend）
docker compose up -d

# 3. 应用数据库迁移（首次或迁移变更后）
docker compose exec backend alembic upgrade head

# 4. 访问
# 前端        http://localhost:8180
# 后端 API    http://localhost:8100/api/v1
# API 文档    http://localhost:8180/docs    （Swagger，Authorize 填 token 可在线调试）
# 健康检查    http://localhost:8100/health
# Prometheus  http://localhost:8100/metrics

# 5. 查看日志 / 重建
docker compose logs -f backend
docker compose up -d --build backend frontend     # 代码变更后重建
```

**端口映射**（宿主端口已做避让，避免与本机其他服务冲突）：

| 服务 | 宿主端口 | 容器内 | 说明 |
|------|---------|--------|------|
| frontend | **8180** | 8080 | React SPA + nginx 反代 |
| backend | **8100** | 8000 | FastAPI |
| MySQL | **3307** | 3306 | 偏移避让 |
| Neo4j | 7474 / 7687 | 同 | HTTP / Bolt |
| Elasticsearch | **19200** | 9200 | 偏移避让 |
| Redis | **16379** | 6379 | 偏移避让 |
| MinIO | 19000 / 19001 | 9000 / 9001 | S3 API / 控制台 |

默认账号：`admin` / `changeme123`（**生产必须修改**）。

---

## 九、生产部署

### 9.1 资源基线

推荐 **8 核 / 32G / 500GB NVMe**（数据盘独立挂载到 Docker 数据目录）。`docker-compose.yml` 已内置资源限制（可通过 `.env` 覆盖），合计约 **25.5G** 内存上限，留 ~6.5G 给系统与页缓存：

| 服务 | 内存 | CPU | 环境变量覆盖 |
|------|------|-----|-------------|
| mysql | 5g | 2 | `UNISENSE_MYSQL_MEM` / `UNISENSE_MYSQL_CPUS` |
| **worker** | **6g** | **3** | `UNISENSE_WORKER_MEM` / `UNISENSE_WORKER_CPUS`（采集峰值主力） |
| backend | 5g | 2 | `UNISENSE_BACKEND_MEM` / `UNISENSE_BACKEND_CPUS` |
| elasticsearch | 4g | 1.5 | `UNISENSE_ES_MEM` / `UNISENSE_ES_CPUS`（堆 2g） |
| neo4j | 2g | 1 | `UNISENSE_NEO4J_MEM` / `UNISENSE_NEO4J_CPUS` |
| redis | 1g | 0.5 | `UNISENSE_REDIS_MEM` / `UNISENSE_REDIS_CPUS` |
| minio | 0.5g | 0.25 | `UNISENSE_MINIO_MEM` / `UNISENSE_MINIO_CPUS` |
| frontend | 0.5g | 0.25 | `UNISENSE_FRONTEND_MEM` / `UNISENSE_FRONTEND_CPUS` |
| backup / binlog-backup | 各 0.5g | 各 0.25 | `UNISENSE_BACKUP_*` / `UNISENSE_BINLOG_*` |

> **Doris（OLAP）默认为注释状态**：内置 `doris-fe` / `doris-be` 不启用，`consume` 查询走 **MySQL 降级引擎**（`degraded=true` 正常返回）。若已有外部 Doris，配 `UNISENSE_OLAP_URL` 即可下推。

### 9.2 生产环境变量（必须修改）

```bash
# .env（生产）
UNISENSE_ENV=prod                                  # 触发生产校验（弱密钥 / CORS 禁通配符等）
UNISENSE_JWT_SECRET=<≥32 字符强随机>                # 严禁使用 dev 默认值
UNISENSE_FERNET_KEY=<独立生成，勿与 JWT 复用>
UNISENSE_MYSQL_ROOT_PASSWORD=<强密码>
UNISENSE_MYSQL_PASSWORD=<强密码>
UNISENSE_ES_PASSWORD=<强密码>
UNISENSE_NEO4J_PASSWORD=<强密码>
UNISENSE_BACKUP_ENCRYPTION_KEY=<≥16 字符>           # 备份 AES-256 加密落盘
UNISENSE_MINIO_ACCESS_KEY / UNISENSE_MINIO_SECRET_KEY=<强凭据>
UNISENSE_BACKUP_DATABASES="unisense e2e_biz"       # 多库备份（含降级业务库）
```

> ⚠️ 生产校验规则：`UNISENSE_ENV` **非 local/dev/test 时**即触发强校验（JWT 弱值、Fernet 与 JWT 复用、CORS 通配符等一律启动失败），避免环境名写成 `staging`/`production` 而绕过。

### 9.3 首次部署

```bash
# 1. 准备环境文件
cp .env.example .env && vi .env       # 按 9.2 填写全部密钥

# 2. 构建并启动
docker compose --env-file .env up -d --build

# 3. 应用数据库迁移
docker compose exec backend alembic upgrade head

# 4. 验证
curl -fsS http://localhost:8100/health            # 应返回 {"status":"ok"}
curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost:8180

# 5. 修改默认管理员密码（生产必须）
#    登录 admin / changeme123 → 账户设置 → 修改密码
```

### 9.4 备份与恢复

| 能力 | 机制 | 恢复点目标 |
|------|------|-----------|
| **全量备份** | `backup` 服务每日 mysqldump（gzip，可选 AES-256 加密），保留 7 天 | 日级 |
| **增量备份** | `binlog-backup` 每 5 分钟拉取 binlog，保留 7 天 | **RPO ≤ 15min** |
| **审计归档** | worker 将 30 天前审计日志导出 JSONL（SHA-256 哈希链）传 MinIO 后清理热表 | — |

```bash
# 手动触发一次全量备份
docker compose exec backup sh /usr/local/bin/backup.sh

# 查看备份文件
docker compose exec backup ls -lh /backups

# 恢复（示例）
gunzip < unisense-YYYYMMDD.sql.gz | docker compose exec -T mysql \
  mysql -uunisense -p<密码> unisense
```

> 备份卷 `unisense_backups` 是磁盘最大变量（全量 7 份 + binlog 7 天），**必须监控水位**；紧张时可降 `RETENTION_DAYS`。

### 9.5 升级与回滚

```bash
# 升级：拉取代码 → 重建应用容器（镜像 tag 可锁定发布版本）
docker compose --env-file .env up -d --build backend worker frontend
docker compose exec backend alembic upgrade head

# 回滚：锁定上一版本镜像 tag
UNISENSE_IMAGE_TAG=<上一版本> docker compose --env-file .env up -d backend worker frontend
docker compose exec backend alembic downgrade -1     # 迁移可逆（up+down+up 已验证）
```

### 9.6 运维要点

- **采集错峰**：全量采集 + LLM 批量推断为峰值负载，`schedule_cron` 建议配凌晨
- **日志**：backend/worker 走 stdout（JSON 格式），由 Docker 日志驱动收集，建议配 rotation
- **监控**：Prometheus 抓取 `http://localhost:8100/metrics`；关注 ES 堆使用率、Redis 队列积压、MySQL 慢查询
- **审计增长**：WORM 表只增，务必开启 `audit_archive` 归档（worker 定时任务）

---

## 十、开发规范

**所有开发规范集中在 `docs/DEV_GUIDE.md`**（工具无关，Agent / 人均适用）。

核心纪律：

1. **最小改动、先读后写、真实验证、独立复核**
2. **13 道质量门禁**（CI 强制）：lint / type / secret / supply_chain / unit / integration / contract / security_reverse / chaos / perf_baseline / migration / observability / doc_sync
3. **状态追踪**：`docs/module-status.yaml` + 变更审计 `docs/CHANGELOG_MODULES.md`
4. **提交信息格式**：`[服务] 动作：简述 (TD§x.y, FR-xx)`
5. **文档同步**：新增页面 / 模块 / 部署 / 技术栈变更时，须同步更新本 README 对应章节（DEV_GUIDE §4）

---

## 十一、测试

```bash
make unit          # 后端单元测试 + 覆盖率
make integration   # 集成测试（需 docker compose up）
make security      # 安全反向测试（普通用户 token 调管理员接口必须 403）
make chaos         # 混沌 / 韧性测试（Redis/Neo4j/ES/OLAP 宕机核心链路 200）
make perf          # 性能基线（k6）
make contract      # 契约一致性校验
make docsync       # 文档同步校验
make gateways      # 运行全部门禁

cd frontend && npm test          # 前端单元测试（vitest）
cd frontend && npm run test:e2e  # 前端 E2E（Playwright）
```

---

## 十二、关键文档

| 文档 | 说明 |
|------|------|
| `docs/proposal.md` | PRD（产品需求，含背景 / 目标 / 量化验收） |
| `docs/technical-design.md` | TD（技术设计，20 章 + §21 审查整改附录） |
| `docs/DEV_GUIDE.md` | 开发规范（唯一权威源） |
| `docs/module-status.yaml` | 模块交付状态追踪（单一事实源） |
| `docs/CHANGELOG_MODULES.md` | 模块变更审计链（不可改写历史行） |
| `docs/API_INTEGRATION.md` | 外部 agent / 数仓对接指南（批量导入字段规范） |
| `docs/QUICKSTART-DOCKER.md` | Docker 启动 / 端口 / LLM 配置速查 |
| `docs/指标设计以及界限说明.md` | 指标口径三层定义（业务 / 技术 / 数仓SQL） |
| `docs/指标语义平台产品需求说明书_v3.0.md` | 产品需求说明书 v3 |
| `.github/workflows/gateways.yml` | CI 质量门禁定义 |

---

## 十三、常用命令

```bash
make help           # 查看所有命令
make lint           # Lint + 格式检查
make type           # 类型检查
make test           # 运行所有测试（unit + integration）
make migrate-up     # 数据库迁移 upgrade head
make migrate-verify # 迁移可逆性验证（up + down + up）
make gateways       # 运行全部门禁
```

---

## 十四、Git 多远程

项目配置了双远程（GitHub + 内部 Git），`git push` 默认**双发**：

```bash
# origin：GitHub（push 双发到内部 Git）
#   git@github.com:lcp5674/Unisense.git
#   git@git.guahao-inc.com:licp/unisense.git（push 目标追加）
# internal：内部 Git
#   git@git.guahao-inc.com:licp/unisense.git

git remote -v               # 查看全部远程
git push                    # 同时推送到 GitHub + 内部 Git
git push internal master    # 单独推送内部 Git
git fetch internal          # 拉取内部 Git 分支
```

> 双发时若一侧失败不会回滚另一侧，失败后需单独 `git push internal` 补齐。
