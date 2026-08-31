# Unisense — 指标语义中台

面向数据治理团队的统一指标语义平台，覆盖指标定义、审批发布、血缘追踪、数据采集与治理、语义查询、质量监控、合规审计与消费下推的完整闭环。

## 项目状态

**生产级全面落地**：后端 20 个交付模块全部 `released`（`docs/module-status.yaml` 追踪），前端 40+ 页面、后端 268 个测试文件 / 约 4000 项测试、前端 1100+ 项测试全绿，13 道质量门禁 CI 强制（`.github/workflows/gateways.yml`）。支持 Docker Compose 单机生产部署（含备份/增量备份/审计归档）。交付详情见 `docs/module-status.yaml` 与 `docs/CHANGELOG_MODULES.md`。

## 核心能力

- **指标全生命周期**：注册（手动向导 + SQL 智能推断批量解析）→ 审批（统一审批中心：指标审批 / 主数据审批 / 冲突仲裁三合一）→ 发布 → 挂载变体 → 废弃 / 回收站（恢复 / 彻底删除 / 批量彻底删除）
- **主数据治理**：原子指标口径库（逻辑度量，三层口径：业务 / 技术 / 数仓SQL）、维度、术语、主题域、系统字典（度量分类 / 度量格式 / 源头系统等字典化在线管理）
- **数据采集与目录**：多源采集（MySQL / PostgreSQL / Hive / Spark / Doris / ClickHouse / Kafka / StarRocks / Hive Metastore）、采集目录、描述缺失治理（LLM 推断补全）、批量废弃与探活
- **血缘追踪**：G6 血缘图谱、上游来源 / 下游影响 / 双向分析、断链检测
- **全局搜索**：9 类资源（指标 / 维度 / 术语 / 模板 / 数据源 / 采集目录表 / 采集目录字段 / 主题域 / 度量目录），中英同义词双路（ES `cn_en_synonym` 分词 + MySQL 关键词扩展）
- **合规治理**：PII 合规复核（指标 + 目录资产双口径）、敏感规则、WORM 审计日志（MinIO 冷热归档 + 哈希链）、被遗忘权
- **质量与可观测**：质量巡检、指标健康度、依赖健康探针、混沌韧性、性能基线
- **消费**：指标查询工作台（OLAP 下推 / MySQL 降级）、QuickBI 嵌入
- **AI 能力**：多 LLM 网关路由（含熔断 / failover / 异常内容识别）、SQL 智能推断（建表注释驱动命名 / 维度提取 / 三层口径 LLM 增强）、描述 / 同义词 / 分类推断
- **用户与权限**：多角色 RBAC（平台管理员 / 域管理员 / 指标负责人 / 评审员 / 合规官 / 分析师 / 查看者）、用户管理、组织管理、多角色权限并集
- **对外集成**：OpenAPI（Swagger，`/docs`）、`docs/API_INTEGRATION.md` 对接指南、批量导入（JSON / CSV / Excel）、指标模板实例化

## 技术栈

| 层 | 技术 |
|------|------|
| 后端 | Python + FastAPI + SQLAlchemy 2.0 (async) + Pydantic v2 + arq（异步任务 worker） |
| 存储 | MySQL 8（主库）+ Neo4j 5（血缘图）+ Elasticsearch 8（搜索索引）+ Redis 7（队列 / SSE / 熔断 / 限流） + MinIO（审计归档，S3 兼容） |
| 前端 | React 18 + TypeScript + Vite + antd 5 + @antv/g6 5（血缘图）+ zustand + @ant-design/charts |
| AI | 多 LLM 网关路由（opencode / hy3 / kilo / 火山方舟 Agent Plan 等 OpenAI 兼容端点），本地无需 GPU |
| 质量 | pytest（unit / integration / security / chaos / perf）+ vitest + Playwright E2E + k6 性能基线 + mypy --strict + ruff |

## 领域模块

`docs/module-status.yaml` 追踪的 20 个交付模块：

collector / lineage / semantic / conflict / governance / users / consume / ai / quality / notify / observability / assetmap / recommend / glossary / dimension / frontend / subject_domain / system_dict / auto_fill / audit_remediation

详见 TD §2.1/§12 与 `docs/module-status.yaml`。

## 快速开始（Docker Compose）

```bash
# 1. 配置环境变量
cp .env.example .env  # 编辑填写实际值（JWT 密钥 / DB 密码 / LLM 配置等）

# 2. 启动全部服务（MySQL/Neo4j/ES/Redis/MinIO/backend/worker/frontend）
docker compose up -d

# 3. 应用数据库迁移（首次或迁移变更后）
docker compose exec backend alembic upgrade head

# 4. 访问
# 前端       http://localhost:8180
# 后端 API   http://localhost:8100/api/v1
# API 文档   http://localhost:8180/docs   （Swagger，Authorize 填 token 可调试）
# 健康检查   http://localhost:8100/health

# 5. 查看日志 / 重建
docker compose logs -f backend
docker compose up -d --build backend frontend   # 代码变更后重建
```

详细启动 / 端口 / 配置 LLM 见 `docs/QUICKSTART-DOCKER.md`。

## 开发规范

**所有开发规范集中在 `docs/DEV_GUIDE.md`**（工具无关，Agent / 人均适用）。

核心纪律：
1. 最小改动、先读后写、真实验证、独立复核
2. 13 道质量门禁（CI 强制）：lint / type / secret / supply_chain / unit / integration / contract / security_reverse / chaos / perf_baseline / migration / observability / doc_sync
3. 状态追踪 `docs/module-status.yaml` + 变更审计 `docs/CHANGELOG_MODULES.md`
4. 提交信息格式：`[服务] 动作：简述 (TD§x.y, FR-xx)`

## 测试

```bash
make unit          # 后端单元测试 + 覆盖率
make integration   # 集成测试（需 docker compose up）
make security      # 安全反向测试（普通用户 token 调管理员接口必须 403）
make chaos         # 混沌 / 韧性测试（Redis/Neo4j/ES/OLAP 宕机核心链路 200）
make perf          # 性能基线（k6）
cd frontend && npm test          # 前端单元测试（vitest）
cd frontend && npm run test:e2e  # 前端 E2E（Playwright）
```

## 关键文档

| 文档 | 说明 |
|------|------|
| `docs/proposal.md` | PRD（产品需求） |
| `docs/technical-design.md` | TD（技术设计，20 章） |
| `docs/DEV_GUIDE.md` | 开发规范（唯一权威源） |
| `docs/module-status.yaml` | 模块交付状态追踪 |
| `docs/CHANGELOG_MODULES.md` | 模块变更审计链 |
| `docs/API_INTEGRATION.md` | 外部 agent / 数仓对接指南（批量导入字段规范） |
| `docs/QUICKSTART-DOCKER.md` | Docker 启动 / 端口 / LLM 配置速查 |
| `docs/指标设计以及界限说明.md` | 指标口径三层定义（业务 / 技术 / 数仓SQL） |
| `docs/指标语义平台产品需求说明书_v3.0.md` | 产品需求说明书 v3 |
| `.github/workflows/gateways.yml` | CI 质量门禁定义 |

## 常用命令

```bash
make help          # 查看所有命令
make gateways      # 运行全部门禁
make lint          # Lint + 格式检查
make type          # mypy --strict 类型检查
make test          # 运行所有测试（unit + integration）
make contract      # 契约一致性校验
make docsync       # 文档同步校验
make migrate-up    # 数据库迁移 upgrade head
make migrate-verify # 迁移可逆性验证（up + down + up）
```

## Git 多远程

项目配置了双远程（GitHub + 内部 Git），`git push` 默认双发：

```bash
# origin：GitHub（push 双发到内部 Git）
#   git@github.com:lcp5674/Unisense.git
#   git@git.guahao-inc.com:licp/unisense.git（push 目标追加）
# internal：内部 Git
#   git@git.guahao-inc.com:licp/unisense.git

git remote -v        # 查看全部远程
git push             # 同时推送到 GitHub + 内部 Git
git push internal master   # 单独推送内部 Git
git fetch internal   # 拉取内部 Git 分支
```
