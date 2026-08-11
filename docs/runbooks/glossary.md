# Runbook：术语库服务（glossary）

> 模块状态：`implemented`（门禁 11/13 绿；runbook 已建；§6.3 双视角审查进行中；待 perf 压测 + §1.5 人工 ratify）
> 关联文档：TD §12.14、FR-08、docs/CHANGELOG_MODULES.md

## 1. 职责边界

术语库服务负责：

- 术语注册 / 更新 / 废止 / 恢复 / 强制发布（状态机 `DRAFT → PUBLISHED → DEPRECATED`）
- 术语冲突检测（别名重叠 / 名称重叠 / 定义重叠）与裁决闭环
- 术语关系维护（同义 / 上位 / 下位 / 相关）

**依赖**：MySQL（`term` / `glossary_conflict` / `term_version` / `term_relation`）、audit 服务（写操作审计落盘）。
**一期明确不做**：术语自动抽取、LLM 关系建议落库闭环（仅声明来源，不自动确认）。

## 2. 关键端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/terms` | 创建术语（write） |
| GET | `/api/v1/terms` | 术语列表（guard 注入校验） |
| GET | `/api/v1/terms/{term_id}` | 术语详情 |
| PUT | `/api/v1/terms/{term_id}` | 更新术语（write） |
| POST | `/api/v1/terms/{id}/deprecate` | 废止（gov） |
| POST | `/api/v1/terms/{id}/restore` | 恢复至 PUBLISHED（gov） |
| POST | `/api/v1/terms/{id}/enforce` | 强制发布（gov） |
| GET | `/api/v1/terms/conflicts` | 冲突列表 |
| POST | `/api/v1/terms/conflicts/{id}/resolve` | 裁决（gov，body `resolver_id`） |
| GET | `/api/v1/terms/relations` | 关系列表 |
| POST | `/api/v1/terms/relations` | 创建关系（write） |

## 3. 状态机与留痕

`term.status` 转移：`DRAFT → PUBLISHED`（enforce）→ `DEPRECATED`（deprecate），`restore` 回 `PUBLISHED`，非法转移由服务校验（非法返回 `400`）。

每次写操作经 `write_audit` 落审计；`deprecate/restore/enforce` 额外落 `operator`（`user.id`）留痕列。

> **已知**：`resolve_conflict` 的裁决人取请求体 `resolver_id` 而非认证 `user.id`，审计留痕存在身份可伪造风险（归级见 §6.3 审查）。

## 4. 冲突检测语义

`_detect_conflicts` 在每次 `create_term` / `update_term` 时调用 `all_terms()` 全量扫描，单写 O(N)。大规模术语库下存在性能风险（见 §8）。冲突写入 `glossary_conflict`，状态 `OPEN → RESOLVED/IGNORED`。

## 5. 依赖与故障

| 依赖 | 故障表现 | 处置 |
|------|----------|------|
| MySQL（`term` / `glossary_conflict` / `term_version` / `term_relation`） | 创建/查询 5xx | 确认迁移 `upgrade head`（0001 + 0003 + 0008 + 0012）；检查连接池 |
| audit 服务 | 写操作无审计 | 检查 audit 健康；不影响主流程落库 |
| RBAC（write / gov 角色） | 403 | 确认调用方角色 |

## 6. 迁移与回滚

- 相关迁移：`0001_initial`（`term`）、`0003_conflict`、`0008_glossary`（`glossary_conflict` / `term_version` / `term_relation`）、`0012_audit_columns`（补 `updated_at` / `deleted_at`）。
- 回滚：`alembic downgrade -1` 逐版回退；`0008` 会 drop 三张表，`0012` 仅 drop 审计列，均为结构回退、数据无损。

## 7. 可观测性

- 结构化日志（structlog JSON）：冲突创建、关系创建。
- 审计：`create/update/deprecate/restore/enforce` 写 audit；`resolve_conflict`、`create_term_relation` 端点审计覆盖见 §6.3 审查。

## 8. 已知限制（一期）

- `resolve_conflict` 裁决人取请求体（身份可伪造）—— §6.3 待修复项。
- 冲突检测 O(N) 每写，大规模术语库下性能风险。
- `list_conflicts` / `get_term` 端点缺少显式角色装饰器（开放读，需复核最小权限原则）。
- 性能基线未单独压测；随语义/消费层统一在 `unisense_perf` 验证。
