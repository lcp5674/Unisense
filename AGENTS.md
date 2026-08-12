# Unisense

指标语义中台（统一指标语义平台）。面向数据治理团队，提供指标注册/血缘/语义查询/质量/合规/消费能力。

## 项目状态
- 当前阶段：**后端 14 个领域服务全部 delivered（`released`）**；前端脚手架、CI、docker-compose 基础设施就绪。
- 技术栈（已落地，详见 TD §1）：Python 3.11(FastAPI) + MySQL 8 + Neo4j 5 + ES 8.15 + Redis 7 + OLAP(可选) + React 18(Vite)。
- 交付门槛：14/14 服务 `released`（verified + runbook + migration 可逆 + §6.3 双视角 0 High）；`§1.5` 人工 ratify 待补（agent 不可代签）。

## 构建 / 测试 / Lint
```bash
# 后端（Poetry 管理，Python 3.11）
poetry install                      # 安装依赖（含 test 组）
poetry run pytest backend/tests     # 测试（unit/integration/security/chaos/observability）
poetry run ruff check backend && poetry run ruff format --check backend   # lint
poetry run mypy --strict backend    # 类型
# 预提交
poetry run pre-commit install
# CI 门禁校验
poetry run python scripts/contract_check.py --mode contract
poetry run python scripts/contract_check.py --mode doc_sync
```

## 架构与入口（详见 TD）
- 14 个领域服务：collector/lineage/semantic/conflict/quality/governance/consume/ai/notify/observability/assetmap/recommend/glossary/dimension（TD §2.1/§12）。
- 三层：接入层 / 领域服务层 / 存储与计算层（TD §0）。

## Agent 开发规范（强制）
> **完整规范以 `docs/DEV_GUIDE.md` 为单一事实源**（工具无关，覆盖 CodeBuddy/Claude Code/OpenCode）。
> 摘要（详见 DEV_GUIDE §1-§6）：

1. **最小改动**：用 `replace_in_file` 精准改文档/代码，禁止整文件 overwrite；一变更一 PR。
2. **先读后写**：改前重新 `read_file`，防上下文过期。
3. **真实验证**：声明完成必须附 pytest/curl/SQL 真实产物；禁止伪造。
4. **门禁递进**：`planned→dev→implemented→verified→released`，`verified` 须独立复核方重跑。
5. **状态追踪**：每步更新 `docs/module-status.yaml`（带 `evidence_path`）；变更记 `docs/CHANGELOG_MODULES.md`。
6. **文档同步**：改接口→§3；改表→§4.1；改状态机→§12.x；PR 描述填 `TD影响章节`。
7. **禁止项**：`except: pass`、硬编码错误码、连生产库测试、无证据声明完成、沉默失败绕过。

> 跨工具读取约定：`AGENTS.md`(CodeBuddy) / `CLAUDE.md`(Claude Code) / `AGENT.md`(OpenCode) 均指向 `docs/DEV_GUIDE.md`。

## 关键文件
- `docs/proposal.md` — PRD（产品需求）
- `docs/technical-design.md` — TD（技术设计，18+ 章）
- `docs/module-status.yaml` — 模块交付状态追踪（单一事实源）
- `docs/CHANGELOG_MODULES.md` — 模块变更审计链
- `CI/.gateways.yml` — CI 质量门禁定义
- `.pre-commit-config.yaml` — 预提交 lint/类型/密钥/提交信息规范
- `scripts/contract_check.py` — TD/代码/状态一致性校验
- `scripts/check_commit_msg.py` — 提交信息格式校验
