# Unisense 开发规范与交付追踪指南（工具无关版）

> 本文档是**所有 coding agent 与开发者共用的唯一规范源**，不绑定任何特定工具（CodeBuddy / Claude Code / OpenCode / Cursor / 人均适用）。
> 各工具读取约定文件（`AGENTS.md` / `CLAUDE.md` / `AGENT.md`）均指向本文档，避免多份维护漂移。
> 门禁的真正拦截点在 **CI（`.github/workflows/gateways.yml`）+ 本地 `pre-commit`**，不依赖单个 agent 自觉。
>
> **与 TD §19/§20 的关系**：TD §19（开发规范）和 §20（Agent 协作约定）是本文档的**设计依据和背景说明**，本文档是其**执行层权威源**。若二者有冲突，以本文档为准（本文档更新更频繁、与 CI/脚本联动更紧密）。

---

## 0. 项目状态
- 阶段：设计完成，待编码启动（仅有 `docs/proposal.md` 与 `docs/technical-design.md`）。
- 技术栈（待落地，详见 TD §1）：Python(FastAPI) + MySQL + Neo4j + ES + Redis + OLAP(Doris/StarRocks) + React。
- 14 个领域服务：collector/lineage/semantic/conflict/quality/governance/consume/ai/notify/observability/assetmap/recommend/glossary/dimension（TD §2.1/§12）。

---

## 1. 开发纪律（强制）

1. **最小改动**：用精准 diff/edit 改文件，禁止整文件 overwrite；一变更一 PR，禁止顺手改不相关服务。
2. **先读后写**：改动前重新读取目标文件最新内容（防止上下文过期导致修复未落位）。
3. **真实验证**：声明"完成/通过"必须附真实执行产物（测试日志片段 / curl 返回 JSON / 实际 SQL 结果），禁止"应该没问题"式断言。
4. **禁止沉默失败**：命令失败必须停下报告，不得换命令绕过（如测试挂了就改阈值放行）。
5. **独立复核（有记录才可认）**：开发方声明 `verified` 后，必须由**独立复核方**（另一 agent / 人）重新独立运行全部门禁并核对证据，禁止自证清白。**复核产出物必须写入 `docs/CHANGELOG_MODULES.md`**（含复核方、日期、门禁重跑结论、evidence 核对结果），无记录视为未复核。
6. **禁改门禁配置**：`CI/.gateways.yml` 标记 `protected: true`，其 `required`/阈值/门禁清单**禁止开发 agent 修改以绕过**；确需调整门槛须经 PR + 独立复核 + 在 CHANGELOG_MODULES 记录理由。
7. **evidence 真实性**：`verified`/`released`/`implemented` 的 `evidence_path` 必须指向**真实存在、非空、≥50 字节**的测试报告文件（由 `contract_check.py --mode gateways_verify` 校验），禁止填假路径或占位文件。
8. **截图证据用完即删**：验证过程中产生的截图（`.png` / `.jpg` 等）、临时 JSON 响应、分析脚本等中间产物，在验证完成并提交后**必须立即清理**，统一存放于 `/tmp/` 的子目录，用完后用 `rm -rf` 删除，不得遗留在工作区或 `/tmp` 中。所有 agent 应养成"验证完即清理"的习惯，避免磁盘空间被持续累积的截图证据占用。

---

## 2. 代码质量门禁（CI + pre-commit 强制）

| 门禁 | 工具 | 要求 |
|------|------|------|
| lint | ruff + black | 零违规 |
| 类型 | mypy --strict | 零错误 |
| 密钥 | gitleaks | 0 泄漏 |
| 供应链 | pip-audit | 0 高危 CVE |
| 单测 | pytest | 核心算法 ≥90% / CRUD ≥70% 覆盖 |
| 集成 | pytest + testcontainers | 双写/事件总线/幂等通过 |
| 契约 | `python3 scripts/contract_check.py --mode contract`（须 `python3`，仓库系统默认 `python` 为 Python 2，脚本含版本守卫） | TD §3/§4 与代码/状态一致 |
| 安全反向 | pytest security | 越权 403 / 注入拦截 / PII 审计 |
| 混沌 | pytest chaos | Redis/Neo4j/ES/OLAP 任一宕机核心链路 200 |
| 性能基线 | k6 | 对照各模块 `perf_contract` |
| 迁移可逆 | alembic | up+down 无损 |
| 可观测 | pytest observability | trace_id 透传 / 指标非零 |
| 文档同步 | `python3 scripts/contract_check.py --mode doc_sync`（同上，须 `python3`） | PR 填 TD 影响章节 |

---

## 3. 状态追踪（单一事实源）

- `docs/module-status.yaml`：14 服务状态（planned→dev→implemented→verified→released→blocked）+ 门禁集合 + 性能契约 + `evidence_path` + `verified_at`（最后验证时间，用于判断是否过期）。
- **状态只能前进**；`verified`/`released`/`implemented` 必挂 `evidence_path`（真实测试报告路径，且文件须存在非空）；回退须写 `rollback_reason`；`blocked` 须写 blocker 原因。
- **blocked 不得长期停留**：单次 `blocked` 超过 5 个工作日须升级（在 CHANGELOG_MODULES 标记 `ESCALATED` + 通知 owner），禁止以 blocked 逃避交付。
- `docs/CHANGELOG_MODULES.md`：变更审计链，**只追加不改写**；每次状态跃迁 + 每次独立复核结论均须记录。

### 状态晋升规则
- `implemented` = 单测全绿 + `evidence_path` 填测试报告
- `verified` = implemented + 集成/契约/安全反向/混沌/性能/迁移/可观测 全绿 + 独立复核通过
- `released` = verified + runbook 存在 + migration 可逆验证通过
- 整体可信度（通过门禁/应过门禁）≥ 0.85 才许 released

---

## 4. 文档更新规范

- 改接口 → 同步 TD §3；改表 → 同步 TD §4.1；改状态机 → 同步 TD §12.x + §3。
- PR 描述必填具体章节：`TD影响章节: §12.x`（须为真实章节号，**禁止填"无"或空**，`contract_check.py --mode doc_sync` 校验）。
- TD §12.x 头部变更表只追加，不改写历史行。

---

## 5. 提交信息规范

### 5.1 格式（pre-commit `check_commit_msg.py` 强制校验；Merge/Revert 除外）

```
[服务] 动作：简述 (TD§x.y, FR-xx)

<可选提交体：动机/影响/breaking change>
```

- **Headline**：`[服务] 动作：简述 (TD§x.y, FR-xx)`
  - 示例：`[quality] fix: PII豁免逻辑补全 (TD§12.8, FR-09)`
  - `[服务]` 须为 14 个合法服务名之一（`check_commit_msg.py` 从 `module-status.yaml` 读取白名单校验）。
  - 动作词：`feat|fix|refactor|test|docs|chore|perf|style`（Conventional Commits 子集）。
  - 简述 ≥ 4 字符，用中文或英文均可，但禁止仅写"修改"/"更新"等无信息量描述。
- **提交体（body，可选但推荐）**：
  - 说明"为什么改"（非"改了什么"，diff 已说明）。
  - 破坏性变更须标注 `BREAKING CHANGE: <说明>`。
  - 关联 issue：`Closes #xxx` / `Refs #xxx`。
- **Co-authored-by**：agent 生成的提交须标注 `Co-Authored-By: CodeBuddy <agent@codebuddy.ai>`。
- **禁止**：一个提交包含多个不相关服务的改动（拆分为多个提交）。

---

## 6. 协作与发布

### 6.1 分支保护与发布

- `master` 保护，所有改走 PR + 至少 1 个独立复核方 review。
- 发布清单：runbook + migration 可逆 + 性能基线 + 安全反向 + 可观测断言全过 → 才许 `released`。
- 每 sprint 读 PRD §9 验收项建子任务；迭代结束生成验收报告（哪些 FR verified / 缺口 / 文档同步）。

### 6.2 代码审查流程

**Review Checklist（reviewer 必检 7 项）**：
1. **安全**：无 PII 明文日志/错误响应；SQL 参数化；权限校验存在；无硬编码密钥。
2. **性能**：无 N+1 查询；批量操作优先；缓存使用合理；无阻塞主线程的同步调用。
3. **错误处理**：无 `except: pass`；异常带 `trace_id`；错误码来自枚举；对外不泄露栈。
4. **日志**：关键操作有日志；日志级别合理；日志内容结构化；无敏感字段。
5. **命名**：符合命名约定（§15）；函数/变量 `snake_case`；类 `PascalCase`。
6. **测试**：覆盖率达标；反向用例存在；测试名表达"场景+预期"；mock 仅 mock 外部依赖。
7. **文档同步**：改接口→§3 同步；改表→§4.1 同步；改状态机→§12.x 同步；PR 填 TD 影响章节。

**Review SLA**：
- 工作日 24h 内首次响应（comment / approve / request changes）。
- 72h 内合并或 close；超期须在 PR 说明延期原因。
- 非工作日不计入 SLA。

**PR 规范**：
- **大小限制**：单 PR ≤ 500 行 diff（不含自动生成代码/迁移脚本）；超限须拆分并说明。
- **自我审查**：作者提交 PR 前须在 GitHub 完成 self-review（逐文件确认）。
- **Reviewer 指派**：按 `module-status.yaml` 的 `owner` 字段指派；owner 空缺时由 `platform_admin` 指派；禁止自指派（self-assign）。
- **Disagreement 处理**：reviewer 与作者意见冲突时，升级到 `platform_admin` 裁决；裁决结论记入 PR comment。

**独立复核（对齐 §1.5）**：
- 开发方声明 `verified` 后，须由独立复核方（另一 agent / 人）重新独立运行全部门禁。
- 复核结论（复核方/日期/门禁重跑结果/evidence 核对）写入 `CHANGELOG_MODULES.md`，无记录视为未复核。
- **等价重提交验证用 delta 比对**：当同一改动因并行 rebase/回退被重新提交（如 `A` 原始提交、`B` 等价重提交），**不得**用 `git diff A B` 做整树比对断言「应为空」——两 commit parent 不同，整树必然存在差异（合并并行会话内容/祖先差异），会产生误报。正确口径：分别比对 `A^..A` 与 `B^..B` 的**增量（delta）**，逐文件、逐字节一致即视为等价重提交；同时确认提交内不含并行会话文件。

### 6.3 交付后双视角漏洞审查（强制约束，新增于 2026-08-07）

**约束触发点**：任一领域服务达到 `verified`（门禁全绿 + 独立复核通过）之后、**宣告交付/进入 `released` 之前**，必须再完成一次「产品视角 + 技术视角」双维度漏洞审查，且结论写入 `docs/CHANGELOG_MODULES.md`，否则视为未真正完成（状态不得晋升 `released`）。

> 理由：门禁（lint/类型/单测/混沌/安全反向…）验证「是否按要求实现」，但不验证「要求本身是否充分、实现是否仍存在设计/产品/合规漏洞」。本约束补齐这一盲区。

**审查必须由独立方执行**（与声明 `verified` 的开发方不同角色/agent；可复用 §6.2 的 reviewer 指派规则），禁止自审自证。

**产品视角审查清单（是否存在产品/业务/合规漏洞）**：
1. **需求覆盖**：module-status 中该服务的 FR 是否全部落地？有无 PRD/验收项缺口？
2. **边界与异常路径**：空值/超大批量/并发/部分失败（批量 `207`）是否都有合理业务语义与用户提示？
3. **合规与数据分级**：PII/敏感数据是否在任何读/写/列表/导出路径被遗漏审计或明文暴露？脱敏是否到位？
4. **权限与越权**：写操作是否都有 RBAC 闸门？最小权限是否满足？跨域/跨租户隔离是否生效？
5. **状态机与生命周期**：状态流转是否完备、有无死锁/悬空状态？废弃/下线是否影响下游？
6. **可观测与可运营**：关键操作是否有审计与指标？运营能否发现异常？

**技术视角审查清单（是否存在实现/架构漏洞）**：
1. **注入与参数化**：所有 DB/外部查询是否参数化？用户输入是否经统一守卫（无字符串拼接 SQL）？
2. **错误处理**：有无 `except: pass`/沉默失败？异常是否带 `trace_id`？对外是否泄露栈/内部路径？
3. **并发与一致性**：乐观锁/幂等是否覆盖写路径？批量操作事务边界是否正确（部分失败不污染已成功）？
4. **依赖韧性**：外部依赖（Redis/Neo4j/ES/OLAP/LLM）宕机时核心链路是否 200 降级？熔断/舱壁是否真实接线（非仅定义）？
5. **性能**：有无 N+1 / 深分页 / 同步阻塞 IO？是否对照 `perf_contract` 有基线且留余量？
6. **安全纵深**：密钥/连接串是否经 Secret Manager？敏感字段是否脱敏日志？缓存是否误存 PII 明文？
7. **测试真实性**：单测/集成/混沌/安全反向是否真覆盖反向用例？mock 是否仅限外部依赖？有无「为过门禁改阈值」的作弊？

**审查产出物**：在 `CHANGELOG_MODULES.md` 追加一行，含：
- 审查方（独立 agent / 人）、日期、`verified_at` 对照。
- 两个视角各自结论（发现项清单 + 风险等级 High/Medium/Low + 处置：已修复 / 已知接受 / 待办）。
- 结论：**发现 0 个 High 漏洞方可晋升 `released`**；Medium/Low 须有处置计划。
- 证据：若审查中发现漏洞并已修复，须附修复后的门禁重跑记录与对应 commit/证据路径。

**记录示例**：
```
| 2026-08-07 | reviewer-agent | semantic | 交付后双视角漏洞审查（新增约束）：
  产品视角：FR-05/06/07 全覆盖；批量 PII 列表审计已补；0 High。
  技术视角：CircuitBreaker 已接入语义实时读路径（缓存降级舱壁）；0 High。
  结论：0 High，可晋升 released | 见 evidence_path |
```

---

## 7. 关键文件索引

| 文件 | 作用 |
|------|------|
| `docs/proposal.md` | PRD |
| `docs/technical-design.md` | TD（§19 开发规范 / §20 协作追踪 / §9.5 门禁复核） |
| `docs/DEV_GUIDE.md` | 本文档（工具无关规范源） |
| `docs/module-status.yaml` | 模块状态事实源 |
| `docs/CHANGELOG_MODULES.md` | 变更审计链 |
| `CI/.gateways.yml` | 门禁声明（人类可读，供 CI 引用） |
| `.github/workflows/gateways.yml` | CI 实际拦截 |
| `.pre-commit-config.yaml` | 本地预提交 |
| `scripts/contract_check.py` | 一致性校验 |
| `scripts/check_commit_msg.py` | 提交信息校验 |

---

## 8. 前端开发规范（编码启动后生效）

- **框架**：React + TypeScript（strict mode）+ Ant Design Pro。
- **状态管理**：全局用 Zustand；组件内用 `useState`/`useReducer`；禁止 Redux。
- **组件命名**：PascalCase；页面组件 `pages/XxxPage.tsx`；业务组件 `components/XxxCard.tsx`。
- **无障碍**：所有交互元素可键盘访问（Tab + Enter/Space）；表单控件须 ARIA `role`/`label`；颜色对比度 WCAG AA。
- **TypeScript 严格**：`tsconfig.json` 启用 `strict: true` + `noUncheckedIndexedAccess`；禁止 `any`（除第三方类型补丁）。
- **前端门禁**：CI 执行 `tsc --noEmit`（`npm run typecheck`）+ `vitest`（`npm test`），拦截类型错误与测试回归（P2-13 落地）；ESLint/Prettier 为建议格式规范（未作硬门禁，`printWidth=100`/`singleQuote` 与后端对齐）。
- **状态流转**：每个页面须实现 5 态（加载→空→正常→错误→降级），对齐 TD §7.7。

---

## 8a. 后端代码风格规范（编码启动后生效）

### 8a.1 Python 命名约定

| 对象 | 约定 | 示例 |
|------|------|------|
| 函数/方法 | `snake_case` | `def get_metric_by_code():` |
| 变量 | `snake_case` | `metric_code = "sales_gmv_day"` |
| 类 | `PascalCase` | `class MetricService:` |
| 常量 | `UPPER_SNAKE` | `MAX_BATCH_SIZE = 50` |
| 私有成员 | `_prefix` | `def _validate_pii():` |
| 模块文件 | `snake_case.py` | `metric_service.py` |
| 测试文件 | `test_*.py` | `test_metric_service.py` |
| Alembic 迁移 | `{YYYYMMDD}_{HHMM}_简述.py` | `20260806_1430_add_metric_batch_id.py` |
| Pydantic Schema | `PascalCase` + 后缀 | `MetricCreateRequest` / `MetricResponse` |
| 枚举值 | `UPPER_SNAKE` | `MetricStatus.PUBLISHED` |

### 8a.2 格式规范

- **行宽**：100 字符（`ruff` / `black` `line-length=100`，与前端 Prettier `printWidth=100` 对齐）。
- **缩进**：Python 4 空格；TS/TSX/YAML 2 空格；**禁止 Tab**。
- **导入排序**（ruff isort 规则）：
  ```
  # 1. stdlib
  import os
  import sys

  # 2. third-party
  from fastapi import APIRouter
  from sqlalchemy import select

  # 3. first-party（Unisense 内部）
  from app.core.config import settings
  from app.models.metric import Metric

  # 4. local（同模块内）
  from .schemas import MetricCreateRequest
  ```
  组间 1 空行；组内按字母序。
- **尾随逗号**：多行集合/调用末尾保留尾随逗号（black 默认）。
- **空行**：顶层函数/类间 2 空行；方法间 1 空行；函数内逻辑块间 1 空行。
- **字符串引号**：Python 双引号（`ruff` `quote = "double"`）；TS 单引号（Prettier `singleQuote: true`）。

### 8a.3 API 路径命名

- 资源路径用 **kebab-case 复数**：`/api/v1/metric-definitions`、`/api/v1/metric-definitions/{code}`。
- 嵌套资源不超过 2 层：`/api/v1/metrics/{code}/versions` 合法；`/api/v1/metrics/{code}/versions/{v}/snapshots/{id}` 禁止（用查询参数替代）。
- 动作端点（非 CRUD）用动词：`POST /api/v1/metrics/{code}/publish`、`POST /api/v1/pii/anonymize`。

### 8a.4 数据库对象命名

| 对象 | 约定 | 示例 |
|------|------|------|
| 表名 | 单数 `snake_case` | `metric`、`metric_version`、`lineage_edge` |
| 字段名 | `snake_case` | `metric_code`、`created_at` |
| 主键 | `id`（BIGINT UNSIGNED AUTO_INCREMENT） | `id` |
| 外键 | `fk_{从表}_{主表}` | `fk_metric_version_metric` |
| 索引 | `idx_{表}_{字段}` / `idx_{表}_{字段1}_{字段2}` | `idx_metric_domain` |
| 唯一约束 | `uk_{表}_{字段}` | `uk_metric_metric_code` |
| 枚举字段 | `ENUM('VAL1','VAL2')` 大写值 | `status ENUM('DRAFT','PUBLISHED')` |

---

## 8b. 目录结构规范（编码启动后生效）

### 8b.1 后端目录布局

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI 应用入口
│   ├── api/                    # 路由层（按服务分文件）
│   │   ├── __init__.py
│   │   ├── deps.py             # 公共依赖（鉴权/分页/trace）
│   │   ├── metrics.py          # /api/v1/metric-definitions
│   │   ├── lineage.py
│   │   └── ...
│   ├── services/              # 领域服务（14 个子包）
│   │   ├── __init__.py
│   │   ├── metric/            # semantic 服务
│   │   │   ├── __init__.py
│   │   │   ├── service.py     # 业务逻辑
│   │   │   ├── repository.py  # 数据访问
│   │   │   └── schemas.py     # Pydantic 入参/出参
│   │   ├── lineage/
│   │   ├── quality/
│   │   └── ...
│   ├── models/                # SQLAlchemy ORM 模型
│   │   ├── __init__.py
│   │   ├── base.py            # Base / 公共 mixin
│   │   ├── metric.py
│   │   └── ...
│   ├── core/                  # 横切关注点
│   │   ├── __init__.py
│   │   ├── config.py          # pydantic-settings 配置
│   │   ├── security.py        # JWT / RBAC
│   │   ├── exceptions.py      # 异常基类体系
│   │   ├── middleware.py       # 中间件（trace/log/cors）
│   │   ├── logging.py         # structlog 配置
│   │   └── observability.py   # OpenTelemetry
│   ├── db/                    # 数据库连接管理
│   │   ├── __init__.py
│   │   ├── mysql.py           # SQLAlchemy engine/session
│   │   ├── neo4j.py
│   │   ├── redis.py
│   │   └── es.py
│   └── utils/                 # 通用工具
│       ├── __init__.py
│       ├── pagination.py
│       └── redact.py          # PII 脱敏工具
├── alembic/                   # 迁移脚本
│   ├── env.py
│   └── versions/
├── tests/
│   ├── __init__.py
│   ├── conftest.py            # 全局 fixture
│   ├── unit/                  # 单元测试（按服务分目录）
│   │   ├── metric/
│   │   │   ├── conftest.py
│   │   │   └── test_service.py
│   │   └── ...
│   ├── integration/           # 集成测试（testcontainers）
│   ├── security/              # 安全反向测试
│   ├── chaos/                 # 混沌/韧性测试
│   ├── observability/         # 可观测测试
│   └── perf/                  # k6 性能脚本
├── pyproject.toml             # Poetry 依赖管理
└── alembic.ini
```

### 8b.2 领域服务内部结构

每个 `services/{service}/` 须包含：
- `service.py`：业务逻辑层（编排 repository + 调用其他 service）。
- `repository.py`：数据访问层（SQLAlchemy 查询，禁止 service 直接写 ORM 查询）。
- `schemas.py`：Pydantic 入参/出参模型（与 ORM model 分离）。
- `__init__.py`：导出 public 接口。

### 8b.3 前端目录布局

```
frontend/
├── src/
│   ├── main.tsx               # 应用入口
│   ├── App.tsx                # 路由配置
│   ├── api/                   # API 调用层
│   ├── components/            # 通用业务组件
│   ├── pages/                 # 页面组件（路由级）
│   ├── hooks/                 # 自定义 hooks
│   ├── stores/                # Zustand 状态管理
│   ├── types/                 # TypeScript 类型定义
│   ├── utils/                 # 工具函数
│   └── styles/                # 全局样式
├── public/
├── package.json
└── tsconfig.json
```

---

## 9. 数据库变更规范

- **迁移工具**：Alembic；每个迁移文件须 `upgrade()` + `downgrade()` 均可执行且数据无损。
- **命名约定**：`{YYYYMMDD}_{HHMM}_{简述}.py`（如 `20260806_1430_add_metric_batch_id.py`）。
- **DDL 审核**：新增表/字段须与 TD §4.1 DDL 对齐（`contract_check.py --mode contract` 校验）；废弃字段先标 `deprecated` 保留 2 版本再清。
- **大表变更**：>1000 万行的表变更须使用 pt-online-schema-change 或 gh-ost，禁止直接 `ALTER TABLE` 锁表。
- **索引变更**：新增索引须注明 TD §4.1a 索引设计原则依据；删除索引须确认无查询引用。
- **WORM 表**：`audit_log` 表禁止 UPDATE/DELETE（MySQL 触发器强制，见 TD §4.1）。

---

## 10. API 版本管理规范

- **当前版本**：`v1`（Header `X-API-Version: v1`）。
- **版本升级**：新增可选字段/端点 = 兼容（不改版本号）；删除/重命名必选字段/改变语义 = 破坏性（升 `v2`）。
- **废弃流程**：旧版本标记 `Sunset` → 响应头 `Sunset: <date>` + `Link: <新版本文档>` → 6 个月后返回 `410 API_VERSION_SUNSET`。
- **通知**：API 版本废弃须提前 6 个月在 `/metrics/ops` 运营看板公示 + 消费方 `notify` 推送。

---

## 11. 分支与发布策略

### 11.1 分支命名

- **feature**：`feat/{服务}-{简述}`（如 `feat/quality-pii-exempt`）。
- **fix**：`fix/{服务}-{简述}`（如 `fix/lineage-parse-null-ptr`）。
- **hotfix**：`hotfix/{服务}-{简述}`（从 `master` 切出，紧急修复）。
- **release**：`release/v{X.Y}`（如 `release/v1.2`）。

### 11.2 分支保护

- **`master` 保护**：需 PR + ≥1 review + CI 全绿；禁止 force push；禁止直推。
- **`release/*` 保护**：需 ≥2 review + CI 全绿；仅接受 cherry-pick，不直接开发。

### 11.3 合并策略

| 分支目标 | 策略 | 原因 |
|----------|------|------|
| → `master` | **Squash merge** | 保持 master 历史线性，每个 commit 对应一个 PR |
| → `release/*` | **Merge commit** | 保留回滚点，便于追溯 cherry-pick 来源 |
| → feature 分支 | **Rebase** | 保持 feature 分支与 master 同步 |

- Squash merge 后提交信息用 PR 标题（须符合 §5 格式）。
- 合并后**自动删除源分支**（GitHub branch protection 配置）。

### 11.4 分支生命周期

| 分支类型 | 最长存活 | 超期处理 |
|----------|----------|----------|
| `feat/*` | 1 sprint（10 工作日） | 须在 PR 说明延期原因；超 2 sprint 须 `platform_admin` 审批 |
| `fix/*` | 5 工作日 | 同上 |
| `hotfix/*` | 2 工作日 | 须当天合并或说明原因 |
| `release/*` | 至下一版本发布 | 发布 + 验证后归档 |

### 11.5 冲突解决

- **Rebase 冲突**由分支作者解决，reviewer 不代劳。
- 作者 rebase 后须重新自测 + 通知 reviewer re-review。
- 禁止用 `git push --force` 到 `master`/`release/*`；feature 分支 force push 须通知 reviewer。

### 11.6 Hotfix 流程

1. 从 `master` 切 `hotfix/{服务}-{简述}`。
2. 修复 + 测试（含回归测试）。
3. PR 合入 `master`（squash merge）。
4. Cherry-pick 到当前 `release/*`（如存在）。
5. 合并后删除 hotfix 分支。

### 11.7 Release 流程

1. 从 `master` 切 `release/v{X.Y}`。
2. 仅接受 cherry-pick（bug fix），不接受新功能。
3. 发布验证通过后打 tag `v{X.Y}.0`。
4. `release/*` 保留至下一版本发布后归档。

---

## 12. 配置变更规范

### 12.1 配置审批

- **变更审批**：平台级参数改 → `platform_admin` 审批；域级 → `domain_admin`；合规约束类（如 PII/审计留存）→ 须经 `compliance_officer` 复核。
- **影响预览**：变更前须执行 `contract_check.py --mode doc_sync` 确认 TD §13 参数表同步。
- **审计留痕**：参数变更入 `audit_log`（`entity_type=config_param`），含 before/after 值。
- **配置来源**：环境变量 > 配置文件 > 数据库默认值（优先级递减），敏感值走 Secret Manager。

### 12.2 配置加载框架

- **框架选型**：`pydantic-settings`（`BaseSettings` + `env_prefix=UNISENSE_`）。
- **启动校验**：应用启动时校验必填配置项，缺失则 **fail-fast**（拒绝启动，日志输出缺失项清单）。
- **配置文件**：`settings.yaml`（业务参数，对齐 TD §13）+ 环境变量（基础设施参数）。
- **禁止**：代码中直接 `os.getenv()` 绕过 Settings 模型（除 `main.py` 启动入口读取 Settings 实例外）。

### 12.3 多环境管理

| 环境 | 配置来源 | 用途 |
|------|----------|------|
| **local** | `.env.local`（gitignore）+ `settings.yaml` | 本地开发 |
| **dev** | 配置中心（dev namespace） | 开发联调 |
| **staging** | 配置中心（staging namespace） | 预发布验证，须是 prod 子集 |
| **prod** | 配置中心（prod namespace） + Secret Manager | 生产 |

- **`.env.example`**：提交 Git，作为环境变量模板，须与 pydantic Settings 字段 1:1 对齐（`contract_check` 校验）。
- **`.env.local`**：`gitignore`，开发者本地覆盖。
- **环境一致性**：staging 须是 prod 的子集；禁止 staging 有 prod 无的配置项（防配置漂移）。
- **环境标识**：`UNISENSE_ENV=local|dev|staging|prod`（Settings 必填字段）。

### 12.4 环境变量清单（基础设施层）

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `UNISENSE_ENV` | 环境标识（必填） | `prod` |
| `UNISENSE_DB_URL` | MySQL 连接串 | `mysql+pymysql://user:pass@host:3306/unisense` |
| `UNISENSE_REDIS_URL` | Redis 连接串 | `redis://host:6379/0` |
| `UNISENSE_NEO4J_URL` | Neo4j Bolt 连接串 | `bolt://host:7687` |
| `UNISENSE_NEO4J_USER` / `_PASSWORD` | Neo4j 认证 | `neo4j` / `***` |
| `UNISENSE_ES_URL` | Elasticsearch 地址 | `http://host:9200` |
| `UNISENSE_OLAP_DSN` | OLAP 连接串（Doris/StarRocks） | `doris://host:9030/unisense` |
| `UNISENSE_OTLP_ENDPOINT` | OpenTelemetry 上报地址 | `http://otel-collector:4317` |
| `UNISENSE_JWT_SECRET` | JWT 签名密钥（Secret Manager 注入） | `***` |
| `UNISENSE_KMS_KEY_ID` | KMS 密钥 ID | `kms-xxxx` |
| `UNISENSE_LLM_DEFAULT_MODEL` | LLM 默认模型别名 | `deepseek-chat` |
| `UNISENSE_LLM_API_KEY` | LLM API 密钥（Secret Manager 注入） | `***` |
| `UNISENSE_CORS_ORIGINS` | CORS 白名单（逗号分隔） | `https://app.unisense.io` |

### 12.5 Secrets 管理

- **Secret Manager**：生产敏感值（JWT 密钥/LLM API Key/DB 密码/KMS Key）统一走 Secret Manager（Vault / 云 KMS），禁止明文入 Git/配置文件/环境变量文件。
- **读取方式**：通过 pydantic Settings 的 `model_config = SettingsConfigDict(env_prefix="UNISENSE_")` 从环境变量注入；环境变量由部署平台从 Secret Manager 拉取注入（代码不直接调用 Secret Manager API）。
- **轮转流程**：JWT 密钥 90 天轮转（对齐 TD §15.2）；轮转须有 runbook（含轮转步骤/验证命令/回滚方案）；轮转后旧密钥保留 7 天用于已签发 token 的过渡。
- **本地开发**：`.env.local` 可含明文测试密钥，但 `gitleaks` 须配置 `.gitleaks.toml` 白名单本地测试密钥（仅 local 环境）。

---

## 13. 安全开发专项（对齐 TD §15）

### 13.1 PII 处理 Checklist

- 标注 `pii_flag` 须走合规门禁。
- 查询/日志/埋点/错误响应禁止含 PII 明文。
- PII 维度值须脱敏后输出。
- 测试数据用脱敏工厂生成，禁止连生产库。

### 13.2 注入防护

- **SQL 注入**：禁止字符串拼接 SQL；ORM 参数化查询；消费方查询经口径 AST→方言翻译（不接受裸 SQL）。
- **NL2SQL 沙箱**（四期）：LLM 输出 SQL 须沙箱校验——禁止 DDL/DML（仅 SELECT）、禁止 `INTO OUTFILE`/`LOAD DATA`、查询深度 ≤ 3 层子查询、扫描行数上限强制。
- **口径表达式沙箱**：AST 校验防注入/无限递归依赖。
- **API 参数**：统一入参校验中间件（防 SQL 注入/XSS/路径穿越），`INJECTION_DETECTED` 返回 403。

### 13.3 密码与密钥存储

- 用户密码 `bcrypt` 哈希（cost ≥ 12）。
- API 密钥经 Secret Manager 托管，数据库仅存 `api_key_hash`（SHA-256）。
- JWT 签名密钥 / HMAC 密钥 / 加密密钥 → KMS 托管，轮转周期 90 天（对齐 §12.5）。

### 13.4 安全 Header

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains`
- `Content-Security-Policy: default-src 'self'`（按前端实际需求收紧）
- `X-XSS-Protection: 1; mode=block`

### 13.5 CORS 规范

- **允许 Origin**：仅配置白名单（`UNISENSE_CORS_ORIGINS`），禁止 `*`。
- **允许方法**：`GET, POST, PUT, DELETE, OPTIONS`（禁止 `TRACE`/`CONNECT`）。
- **允许 Header**：`Authorization, Content-Type, X-API-Version, X-Trace-Id`。
- **凭证传递**：如需 cookie 认证则 `allow_credentials=True`，但 Origin 白名单必须严格（禁止配合 `*`）。
- **预检缓存**：`max_age=3600`（减少 OPTIONS 请求）。

### 13.6 CSRF 防护

- **Bearer Token 模式**（主要）：API 使用 `Authorization: Bearer <token>`，CSRF 豁免（不自动携带 cookie）。
- **Cookie 认证模式**（如有）：须双重提交 cookie + `SameSite=Strict` + Origin/Referer 校验。
- **状态变更请求**：仅接受 `POST/PUT/DELETE`，禁止 `GET` 修改状态。

### 13.7 文件上传安全

- **类型白名单**：仅允许 `.xlsx`/`.csv`/`.json`/`.yaml`（交底书/批量注册/配置导入）。
- **大小限制**：单文件 ≤ 50MB（`check-added-large-files` pre-commit 已限 1MB；API 层独立限制）。
- **魔数校验**：不能仅校验扩展名，须校验文件头魔数（防伪装）。
- **存储隔离**：上传文件存 MinIO 独立 bucket，禁止存本地文件系统；路径用 UUID 生成，禁止用用户输入的文件名。
- **病毒扫描**（可选）：生产环境接入 ClamAV 扫描后入库。

### 13.8 限流实现规范

- **算法**：Redis 令牌桶（Lua 原子操作，避免竞态）。
- **响应**：超限返回 `429 TOO_MANY_REQUESTS`，须含 `Retry-After`（秒）+ `X-RateLimit-Limit` + `X-RateLimit-Remaining` + `X-RateLimit-Reset` Header。
- **维度**：客户端级（`api.client_qps_limit`）+ 用户级 + IP 级（防匿名滥用）。
- **降级**：Redis 不可用时回退内存令牌桶（单机限流），并告警（对齐 TD §14.1 写失败补偿）。

### 13.9 依赖安全扫描

- **CI 扫描**：每次 PR 运行 `pip-audit`（门禁 `supply_chain`，0 高危 CVE）。
- **定期全量扫描**：每周一定时运行全量依赖扫描（含间接依赖），结果通知 `platform_admin`。
- **漏洞响应 SLA**：
  - 高危（CVSS ≥ 7.0）：24h 内评估，7 天内修复或缓解。
  - 中危（CVSS 4.0–6.9）：3 天内评估，30 天内修复。
  - 低危（CVSS < 4.0）：下次迭代修复。

### 13.10 密钥轮转操作手册

- **轮转对象**：JWT 签名密钥 / HMAC 密钥 / 加密密钥 / LLM API Key / DB 密码。
- **轮转流程**：
  1. 在 Secret Manager 生成新密钥。
  2. 部署支持新旧密钥并存的版本（双密钥期，7 天）。
  3. 验证新密钥生效（测试环境验证 + 生产灰度验证）。
  4. 移除旧密钥（确认无引用后删除）。
- **回滚**：保留旧密钥 7 天，发现新密钥问题可即时回退。
- **验证命令**：轮转后执行 `scripts/verify_secrets.py`（编码启动后建立）确认所有服务可读新密钥。

### 13.11 被遗忘权

- `POST /pii/anonymize` 覆写脱敏非删除，审计留痕（含 SHA256 前缀），对齐 TD §12.5。

---

## 14. LLM 开发规范（四期前预建立）

- **Prompt 变更**：走 `POST /llm/prompt-templates` 审核流（DRAFT→REVIEW→APPROVED），变更后须跑 Golden Set 校准。
- **Golden Set 维护**：每域 ≥ 20 条标准 QA 对，覆盖率按季度评估；新增术语/口径后须补充测试对。
- **LLM 输出校验**：① NL2SQL 输出须经语义锚定（映射到 `metric_code`，不接受裸 SQL）；② MCP 工具输出须校验 Schema 合规；③ 检测提示词注入 → `403 INJECTION_DETECTED`。
- **成本可观测**：每次 LLM 调用记入 `llm_test_report`（token 数/成本/延迟），月度汇总入运营看板。

---

## 15. 错误处理规范（对齐 TD §19.2 / §5.4）

### 15.1 异常分层体系

```python
# app/core/exceptions.py
class UnisenseError(Exception):
    """所有业务异常基类。携带 error_code + trace_id + ctx。"""
    error_code: str = "INTERNAL_ERROR"
    http_status: int = 500

class BusinessError(UnisenseError):
    """业务逻辑错误（4xx），可预期，须给用户友好提示。"""
    http_status: int = 400

class ValidationError(BusinessError):
    """入参校验失败。"""
    error_code: str = "VALIDATION_ERROR"
    http_status: int = 422

class AuthError(BusinessError):
    """认证/授权失败。"""
    error_code: str = "FORBIDDEN"
    http_status: int = 403

class SystemError(UnisenseError):
    """系统内部错误（5xx），不可预期，须告警。"""
    error_code: str = "INTERNAL_ERROR"
    http_status: int = 500

class ExternalDependencyError(SystemError):
    """外部依赖异常（DB/Redis/ES/LLM），含 retry 标记。"""
    error_code: str = "EXTERNAL_DEPENDENCY_ERROR"
    retryable: bool = True
```

### 15.2 异常处理层级

| 层级 | 职责 | 示例 |
|------|------|------|
| **Repository** | 转换 DB 异常为 `ExternalDependencyError` | `except SQLAlchemyError → raise ExternalDependencyError` |
| **Service** | 检测业务条件，raise `BusinessError` | `if not has_permission: raise AuthError("FORBIDDEN")` |
| **API（全局 handler）** | 捕获所有 `UnisenseError`，转换为统一响应；未捕获异常转 `SystemError` | `@app.exception_handler(UnisenseError)` |

- **禁止**：在 API 层（路由函数内）直接 try/except 业务逻辑（应在 service 层处理）。
- **禁止**：`except: pass` / `except Exception: pass`（TD §19.2）；如需兜底须 `except Exception as e: logger.error(...); raise SystemError(...)`。

### 15.3 统一错误响应格式

```json
{
  "code": "PII_ACCESS_DENIED",
  "message": "无权访问 PII 指标",
  "trace_id": "a1b2c3d4e5f6",
  "detail": null
}
```

- `code`：错误码枚举值（来自 TD §5.4，禁止硬编码数字）。
- `message`：用户可读的中文提示（不含内部栈/SQL/路径等敏感信息）。
- `trace_id`：用于排查问题的链路 ID。
- `detail`：可选，附加上下文（如校验字段列表）。

### 15.4 重试策略

```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    before_sleep=lambda rs: logger.warning("retry", attempt=rs.attempt, error=str(rs.outcome.exception())),
)
async def call_external(): ...
```

- **重试条件**：仅对 `ExternalDependencyError`（`retryable=True`）且为连接超时/网络错误重试。
- **禁止重试**：`BusinessError`（4xx）不重试（重试无意义）。
- **LLM 调用重试**：须记录每次重试的 token 消耗（防重复计费）。
- **熔断**：连续失败触发熔断（对齐 TD §13 `circuit_breaker.error_rate_pct=50`），熔断期间直接返回降级响应。

### 15.5 异常日志级别映射

| 异常类型 | 日志级别 | 记录内容 |
|----------|----------|----------|
| `ValidationError` | DEBUG | 字段名+值（脱敏后） |
| `BusinessError`（非 Validation） | WARN | error_code + ctx + trace_id |
| `ExternalDependencyError`（超时） | WARN | 依赖名 + 耗时 + retry 标记 |
| `ExternalDependencyError`（熔断） | ERROR | 依赖名 + 熔断状态 + 影响范围 |
| `SystemError` | ERROR | 完整栈 + trace_id + ctx |
| 未捕获异常 | ERROR（+ 告警） | 完整栈 + 请求信息（脱敏） |

### 15.6 错误码管理

- 错误码定义在 TD §5.4 枚举表，新增/修改错误码须：
  1. 同步更新 TD §5.4。
  2. PR 描述填写 `TD影响章节: §5.4`。
  3. `contract_check.py --mode doc_sync` 校验。
- **禁止**：代码中硬编码错误码字符串（须用 `app/core/error_codes.py` 枚举常量）。

---

## 16. 日志记录规范（对齐 TD §16.2）

### 16.1 日志框架

- **选型**：`structlog`（JSON 输出），与 OpenTelemetry 集成自动注入 `trace_id`/`span_id`。
- **配置**（`app/core/logging.py`）：
  ```python
  structlog.configure(
      processors=[
          structlog.contextvars.merge_contextvars,
          structlog.processors.add_log_level,
          app.utils.redact.redact_processor,   # PII 脱敏
          structlog.processors.TimeStamper(fmt="iso"),
          structlog.processors.JSONRenderer(),
      ],
  )
  ```
- **生产环境**：JSON 格式输出到 stdout（容器日志收集）。
- **开发环境**：彩色控制台输出（`structlog.dev.ConsoleRenderer`）。

### 16.2 日志级别

| 级别 | 使用场景 | 示例 |
|------|----------|------|
| **ERROR** | 系统错误/熔断/数据丢失风险/须告警 | `logger.error("db_write_failed", table="metric", error=str(e))` |
| **WARN** | 降级生效/重试/外部依赖超时/业务规则拒绝 | `logger.warning("circuit_breaker_open", dep="neo4j")` |
| **INFO** | 审计/关键操作（CRUD/审批/发布） | `logger.info("metric_published", code="sales_gmv_day", actor="user1")` |
| **DEBUG** | 开发调试/详细参数/SQL 语句；生产默认关闭 | `logger.debug("query_executed", sql=sql, params=params)` |

### 16.3 日志内容规范

- **结构化字段**：所有日志须包含 `trace_id`/`span_id`/`service`/`level`/`ts`/`event`；业务日志须含 `ctx_json`（上下文字段）。
- **ctx_json 统一字段命名**：
  - `metric_code` / `domain` / `tenant_id` / `actor_id` / `action` / `latency_ms` / `entity_type` / `entity_id`。
- **必记操作**：
  - 指标 CRUD（INFO：code + actor + action）。
  - 审批流转（INFO：entity_id + from_status + to_status + actor）。
  - 降级触发（WARN：dep + fallback + reason）。
  - 重试（WARN：attempt + error + next_retry_in）。
  - 熔断触发（ERROR：dep + state + affected_services）。
  - 外部依赖超时（WARN：dep + timeout_ms + threshold）。
- **禁止**：将整个请求体/响应体写入日志（可能含 PII）；如需记录须脱敏后仅记关键字段。

### 16.4 敏感字段过滤

- **redact_processor**：structlog processor 链中注入脱敏处理器（`app/utils/redact.py`）。
- **脱敏规则**：按 TD §13 `pii.identification.regex_list` 配置（id_card/phone/email/name），匹配字段值替换为 `***REDACTED***`。
- **字段名黑名单**：`password`/`token`/`secret`/`api_key`/`access_token`/`refresh_token` 字段的值一律脱敏，不论内容。
- **深度脱敏**：嵌套 JSON（如 `ctx_json`）中的敏感字段也须递归脱敏。

### 16.5 日志采样

- **高频路径采样**：Semantic API 查询日志采样 10%（`if random() < 0.1: logger.info(...)`）；慢查询（P95）100% 采样。
- **审计日志不采样**：所有审计事件全量记录（TD §16.2 审计日志独立通道）。
- **DEBUG 级别**：生产默认关闭；如需临时开启，通过配置中心动态调整（不重启服务），开启后 30 分钟自动恢复关闭。

### 16.6 日志保留

- 热存 30 天（ELK / Loki）。
- 冷归档 180 天（S3 / OSS）。
- 审计日志保留 ≥ 3 年（对齐 PIPL，TD §15.4）。

---

## 17. 性能优化准则（对齐 TD §14 / module-status.yaml perf_contract）

### 17.1 数据库查询优化

- **N+1 查询禁止**：关联查询用 SQLAlchemy `selectinload`（默认）/ `joinedload`（一对一）；CI 测试期开启 `echo=True` 检测 N+1（>3 次相同查询告警）。
- **批量操作**：>10 条写入用 `bulk_insert_mappings()` / `bulk_update_mappings()`，禁止循环单条 `session.add()`。
- **只读查询**：读操作用 `session.execute(select(...))`，禁止 `session.query()` 全量加载 ORM 对象（仅按需选列）。
- **分页**：
  - 浅分页（offset < 10000）：`LIMIT offset, size`。
  - 深分页（offset ≥ 10000）：cursor pagination（`WHERE id > last_id ORDER BY id LIMIT size`），禁止大 offset。
- **慢查询**：P95 > 500ms 的 SQL 须优化或加索引；慢查询日志阈值 1s（MySQL `long_query_time=1`）；每周 review 慢查询 TOP 10。

### 17.2 缓存策略

- **缓存原则**：读多写少 + 非实时数据走 Redis；实时性要求高的数据不缓存（如审批状态）。
- **TTL**：按 TD §13 `cache.ttl_default_hours=24`；指标元数据缓存 1h；维度值缓存 24h。
- **防穿透**：空值也缓存（TTL 60s），防止恶意查询不存在的 key 压垮 DB。
- **防击穿**：热点 key 用互斥锁（`SET NX EX 5`）重建缓存，避免大量请求同时回源。
- **防雪崩**：TTL 加随机抖动（±10%），避免大量 key 同时过期。
- **失效策略**：写操作后主动删缓存（`DELETE key`），禁止先删后写（防并发读旧值回填）。
- **禁止缓存**：PII 明文数据（仅缓存 PII 标记/脱敏值）；审计日志。

### 17.3 异步处理规范

| 操作类型 | 处理方式 | 示例 |
|----------|----------|------|
| API 响应内可完成（< 500ms） | 同步 | 指标查询/维度映射 |
| 耗时但用户可等待（500ms–8s） | `async/await` + 非阻塞 IO | LLM 解析/血缘解析 |
| 超长耗时/批量任务 | 后台任务（Redis Stream） | 全量采集/批量注册/对账 |
| 定时任务 | Celery / Airflow | 每日对账/PII 重扫 |

- **禁止**：在请求处理中同步调用阻塞 IO（如 `requests.get`），须用 `httpx.AsyncClient`。
- **事件消费者**：必须幂等（同事件重放无副作用），对齐 TD §19.2。

### 17.4 Neo4j 性能规范

- **读写分离**：写入走 Leader 连接池；查询走 Follower 只读副本（`bolt://follower:7687`）。
- **批量写入**：用 `UNWIND + MERGE` 批量操作（`neo4j.write_batch_size=100`），禁止逐条 CREATE。
- **查询深度**：影响面查询深度 ≤ 5 跳（对齐 `module-status.yaml` perf_contract）；超 5 跳须分步查询 + 缓存中间结果。
- **索引**：所有查询字段须有 Neo4j 索引（`CREATE INDEX FOR (n:Metric) ON (n.code)`）。

### 17.5 前端性能规范

- **Bundle 大小**：首屏 Bundle ≤ 300KB（gzip）；路由级代码分割（`React.lazy` + `Suspense`）。
- **列表渲染**：> 50 行的列表用虚拟滚动（`react-window` / `@tanstack/react-virtual`）。
- **图片**：懒加载（`loading="lazy"`）；缩略图用 WebP 格式。
- **请求**：API 请求并发 ≤ 6（浏览器限制）；串行依赖须用 `Promise.all` 并行化无依赖请求。
- **重渲染**：避免在 render 中创建新对象/函数（用 `useMemo`/`useCallback`）；列表项用 `React.memo`。

### 17.6 性能 Profile 方法论

- **何时 profile**：新模块 `verified` 前；性能回归（P95 较基线退化 > 20%）；用户反馈慢。
- **工具**：`py-spy`（生产火焰图，无侵入）/ `cProfile`（开发期详细分析）/ `k6`（负载测试）。
- **基线记录**：每次 `verified` 须在 evidence 报告中记录 P50/P95/P99 基线值，作为后续回归对照。

---

## 18. 依赖管理规则（编码启动前必须完成）

### 18.1 依赖管理工具

- **工具**：Poetry（`pyproject.toml` + `poetry.lock`）。
- **Python 版本**：>=3.11（与 CI `actions/setup-python@v5 python-version: "3.11"` 对齐）。
- **CI 集成**：CI 中用 `poetry export --without-hashes -f requirements.txt -o requirements.txt` 生成临时文件供 `pip-audit` 扫描。
- **锁定文件**：`poetry.lock` 须提交 Git，确保可重现构建。

### 18.2 版本范围规范

| 依赖类型 | 版本约束 | 示例 |
|----------|----------|------|
| 生产依赖 | `~=`（兼容版本，允许 patch/minor 升级） | `"fastapi ~=0.115.0"` |
| 开发依赖 | `>=`（宽松，允许最新） | `"pytest >=8.0"` |
| 精确锁定 | `==`（仅 `poetry.lock` 中） | `poetry.lock` 自动生成 |

- **禁止**：使用 `*` 或不指定版本（不可重现）。
- **禁止**：直接编辑 `poetry.lock`（须通过 `poetry add` / `poetry update` 修改）。

### 18.3 依赖分层

```toml
[tool.poetry.dependencies]
python = "^3.11"
fastapi = "~0.115.0"
sqlalchemy = "~2.0.0"
pydantic = "~2.0"
redis = "~5.0"
neo4j = "~5.0"
elasticsearch = "~8.0"
structlog = "~24.0"
# ... 生产依赖

[tool.poetry.group.dev.dependencies]
pytest = ">=8.0"
pytest-cov = ">=5.0"
pytest-mock = ">=3.0"
ruff = ">=0.6.0"
mypy = ">=1.11"
pip-audit = ">=2.0"
pre-commit = ">=3.0"
# ... 开发依赖
```

### 18.4 新增依赖审批流程

1. **评估**：PR 须说明用途 + 替代方案评估（是否已有等价依赖）。
2. **许可证检查**：新增依赖须在 PR 中声明许可证；**禁止 GPL/AGPL** 许可证依赖（与项目商业闭源冲突）。
3. **安全检查**：`pip-audit` 须在 CI 中通过（0 高危 CVE）。
4. **审批**：由 `platform_admin` 或 reviewer 审批。
5. **记录**：新增依赖记入 `docs/CHANGELOG_MODULES.md`（依赖变更审计）。

### 18.5 依赖更新策略

- **工具**：Renovate（GitHub App），每周检查更新。
- **更新策略**：
  - **patch/minor**：自动创建 PR（`automerge: true` 仅在 CI 全绿后）。
  - **major**：创建 PR 但不自动合并，须人工评审。
- **更新频率**：每周一扫描；紧急安全更新（高危 CVE）立即创建 PR。

### 18.6 禁用清单

- **许可证**：GPL / AGPL / SSPL（与商业闭源冲突）。
- **供应链风险**：已知投毒/恶意包（参考 npm/pypi 安全公告）。
- **弃用包**：已弃用且无维护的包（如 `distutils`）。

---

## 19. 测试规范补充（对齐 TD §19.4 / §2 门禁表）

### 19.1 测试文件组织

```
tests/
├── conftest.py                    # 全局 fixture（DB session / mock client）
├── unit/                          # 单元测试
│   ├── metric/                    # 按服务分目录
│   │   ├── conftest.py            # 服务级 fixture
│   │   ├── test_service.py        # service 层测试
│   │   └── test_repository.py     # repository 层测试
│   ├── lineage/
│   └── ...
├── integration/                   # 集成测试（testcontainers）
│   ├── test_metric_mysql_neo4j_sync.py
│   └── ...
├── security/                      # 安全反向测试
├── chaos/                         # 混沌/韧性测试
├── observability/                 # 可观测测试
└── perf/                          # k6 性能脚本
    └── baseline.js
```

- **命名**：`test_{被测对象}_{场景}_{预期}.py` 或 `test_{被测对象}.py` 内用方法名表达场景。
- **一个测试文件对应一个被测模块**（`test_metric_service.py` ↔ `metric/service.py`）。

### 19.2 Mock 规范

- **Mock 库**：`pytest-mock`（`mocker` fixture）。
- **Mock 粒度**：仅 mock 外部依赖（DB/Redis/ES/LLM/HTTP），**不 mock 内部 service**（避免测试脱离真实逻辑）。
- **Mock 位置**：在 `conftest.py` 或测试函数内 mock，禁止在模块级 mock（影响其他测试）。
- **断言**：mock 调用后须断言调用次数/参数（`mocker.assert_called_once_with(...)`），禁止仅断言返回值。

### 19.3 Fixture 规范

- **层级**：`tests/conftest.py`（全局）+ `tests/unit/{service}/conftest.py`（服务级）。
- **命名**：`fixture_{对象}`（如 `fixture_metric` / `fixture_db_session` / `fixture_mock_redis`）。
- **scope**：默认 `function`（隔离）；DB session 用 `function`（每测试回滚）；mock client 用 `function`；只读参考数据用 `session`。
- **工厂 fixture**：用工厂模式生成测试数据（`fixture_metric_factory` 返回工厂函数，按参数生成不同 metric）。
- **数据清理**：`function` scope fixture 须在 teardown 清理（DB session 自动回滚；Redis `flushdb`；ES `delete_index`）。

### 19.4 测试数据准备

- **脱敏工厂**（对齐 §13.1）：测试数据用 `factory_boy` + `faker` 生成，禁止从生产库导出数据。
- **种子数据**：`tests/seed/` 下放 YAML 种子数据（参考数据，如枚举值/角色/权限），用 `session` scope fixture 加载。
- **禁止**：测试中硬编码真实用户名/手机号/身份证号（用 faker 生成）。

### 19.5 测试标签

```python
# tests/conftest.py
import pytest

def pytest_configure(config):
    config.addinivalue_line("markers", "slow: 耗时 >1s 的测试")
    config.addinivalue_line("markers", "integration: 需要 testcontainers 的测试")
    config.addinivalue_line("markers", "smoke: 冒烟测试（核心链路快速验证）")
```

- **CI 默认执行**：除 `slow` 外全部（`pytest -m "not slow"`）。
- **本地快速验证**：`pytest -m "smoke"`。
- **完整运行**：`pytest`（含 slow）。

### 19.6 E2E 测试（前端）

- **工具**：Playwright（跨浏览器）。
- **覆盖范围**：核心用户旅程（注册指标→审批→查询→血缘查看→导出）。
- **执行**：CI 中 `frontend-e2e` job（单独 job，仅在 `backend/` 和 `frontend/` 均存在时触发）。
- **数据隔离**：E2E 测试用独立测试环境（staging），测试后清理数据。

### 19.7 性能测试执行

- **工具**：k6（`backend/tests/perf/baseline.js`）。
- **基线对照**：每次 `verified` 须运行 k6 并在 evidence 报告中记录 P50/P95/P99，对照 `module-status.yaml` 的 `perf_contract`。
- **性能回归判定**：P95 较基线退化 > 20% 判为回归，须优化后方可合并。
- **负载阶梯**：10 → 50 → 100 → 200 VU，每阶梯 2 分钟，记录各阶梯延迟/错误率。

---

## 20. 文档编写标准

### 20.1 代码注释规范

- **docstring 格式**：Google 风格（统一）。
- **必写 docstring 的对象**：
  - 模块级（文件头）：说明模块职责。
  - 类：说明类用途 + 关键属性。
  - public 函数/方法（非 `_` 前缀）：说明功能 + Args + Returns + Raises + Example。
- **复杂算法**：口径翻译/冲突检测/血缘解析等复杂逻辑须有算法说明注释（步骤/时间复杂度/边界条件）。
- **禁止**：用注释复述代码（如 `# 设置 x 为 1` 对应 `x = 1`）；注释应说明"为什么"而非"是什么"。

**docstring 示例**：
```python
def publish_metric_version(code: str, version: int, actor: str) -> MetricVersion:
    """发布指标版本（DRAFT → PUBLISHED 状态流转）。

    Args:
        code: 指标编码，须符合命名约定（域_业务对象_度量_统计周期）。
        version: 待发布版本号，须为当前 DRAFT 版本。
        actor: 操作人 user_id，须有 `metric.publish` 权限。

    Returns:
        发布后的 MetricVersion 对象（status=PUBLISHED）。

    Raises:
        AuthError: actor 无 `metric.publish` 权限。
        BusinessError: 版本非 DRAFT 状态或不存在。

    Example:
        >>> v = publish_metric_version("sales_gmv_day", 2, "user1")
        >>> v.status
        'PUBLISHED'
    """
```

### 20.2 API 文档

- **自动生成**：FastAPI 自动生成 OpenAPI（`/docs` Swagger UI + `/redoc` ReDoc）。
- **端点要求**：所有 API 端点须有：
  - `summary`：一句话描述。
  - `description`：详细说明（含业务规则/约束）。
  - `response_model`：响应 Pydantic 模型。
  - `responses`：非 2xx 响应示例（含错误码）。
  - `tags`：按服务分组（如 `tags=["指标管理"]`）。
- **版本**：OpenAPI 文档版本与 API 版本对齐（`X-API-Version: v1` → OpenAPI `v1`）。

### 20.3 Runbook 模板

`released` 须附带 runbook（`docs/runbooks/{service}.md`），模板：

```markdown
# {服务名} Runbook

## 1. 服务概述
- 职责 / 依赖 / 关键指标

## 2. 部署步骤
- 前置条件 / 部署命令 / 验证步骤

## 3. 监控指标
- Prometheus 指标 / 告警规则 / 告警阈值

## 4. 告警处置
| 告警 | 原因 | 处置步骤 |
|------|------|----------|

## 5. 回滚步骤
- migration down / K8s rollback / 口径版本回退

## 6. 联系人
- Owner / Backup / 升级路径
```

### 20.4 ADR（架构决策记录）

- **何时写 ADR**：重大技术决策（框架选型/架构变更/关键 trade-off）。
- **位置**：`docs/adr/ADR-{NNN}-{标题}.md`（如 `ADR-001-选择-poetry-作为依赖管理工具.md`）。
- **模板**：
  ```markdown
  # ADR-{NNN}: {标题}
  ## 状态：Proposed | Accepted | Deprecated | Superseded
  ## 日期：YYYY-MM-DD
  ## 背景
  ## 决策
  ## 备选方案
  ## 后果
  ```
- **不可改写**：已 Accepted 的 ADR 不可修改；如需变更须新建 ADR 并标记原 ADR 为 `Superseded by ADR-NNN`。

### 20.5 文档评审

- 文档变更（TD/DEV_GUIDE/runbook/ADR）走 PR + review，与代码变更同等对待。
- `contract_check.py --mode doc_sync` 校验 TD §3/§4/§12 与代码/状态文件一致。
- PR 描述须填写 `TD影响章节`（禁止填"无"）。
