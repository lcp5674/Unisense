# Unisense — 指标语义中台

面向数据治理团队的统一指标语义平台，提供指标注册、血缘追踪、语义查询、质量监控、合规治理与消费能力。

## 项目状态

**后端 14 个领域服务全部 delivered（`released`）**：指标注册、血缘、冲突仲裁、合规治理、质量、消费、AI、通知、可观测、资产地图、推荐、术语、维度、采集。前端脚手架、CI 门禁、docker-compose 基础设施就绪；`§1.5` 人工 ratify 待补。交付详情见 `docs/module-status.yaml` 与 `docs/CHANGELOG_MODULES.md`。

## 技术栈

Python(FastAPI) + MySQL + Neo4j + Elasticsearch + Redis + OLAP(Doris/StarRocks) + React

## 14 个领域服务

collector / lineage / semantic / conflict / quality / governance / consume / ai / notify / observability / assetmap / recommend / glossary / dimension

详见 TD §2.1/§12。

## 快速开始

```bash
# 1. 安装依赖
make dev

# 2. 启动本地服务（MySQL/Neo4j/ES/Redis）
make setup-services

# 3. 配置环境变量
cp .env.example .env  # 编辑填写实际值

# 4. 数据库迁移
make migrate-up

# 5. 运行测试
make test
```

## 开发规范

**所有开发规范集中在 `docs/DEV_GUIDE.md`**（工具无关，Agent/人均适用）。

核心纪律：
1. 最小改动、先读后写、真实验证
2. 13 道质量门禁（CI 强制）
3. 状态追踪 `docs/module-status.yaml` + 变更审计 `docs/CHANGELOG_MODULES.md`
4. 提交信息格式：`[服务] 动作：简述 (TD§x.y, FR-xx)`

## 关键文档

| 文档 | 说明 |
|------|------|
| `docs/proposal.md` | PRD（产品需求） |
| `docs/technical-design.md` | TD（技术设计，20 章） |
| `docs/DEV_GUIDE.md` | 开发规范（唯一权威源） |
| `docs/module-status.yaml` | 模块交付状态追踪 |
| `docs/CHANGELOG_MODULES.md` | 模块变更审计链 |
| `CI/.gateways.yml` | CI 质量门禁定义 |

## 常用命令

```bash
make help          # 查看所有命令
make gateways      # 运行全部门禁
make lint          # Lint 检查
make test          # 运行测试
make contract      # 契约一致性校验
```
