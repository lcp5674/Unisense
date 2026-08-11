# Runbook：维度管理服务（dimension）

> 模块状态：`implemented`（门禁 11/13 绿；runbook 已建；§6.3 双视角审查进行中；待 perf 压测 + §1.5 人工 ratify）
> 关联文档：TD §12.15、docs/CHANGELOG_MODULES.md

## 1. 职责边界

维度管理服务负责：

- 维度主数据 CRUD（`dimension`）与状态机 `DRAFT → PUBLISHED → DEPRECATED`
- 维度成员（`dimension_member`）、维度映射（`dimension_mapping`）
- 指标-维度绑定（`metric_dimension`，角色 PARTITION / SPLICE / FILTER）
- 口径对账（`reconciliation`，`PENDING → APPROVED/REJECTED`）

**依赖**：MySQL（dimension 系列五表）、audit 服务。
**一期明确不做**：SCD2 历史版本链落库、映射表达式自动校验引擎。

## 2. 关键端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/dimensions` | 创建维度（write） |
| GET | `/api/v1/dimensions` | 维度列表（guard） |
| GET | `/api/v1/dimensions/{id}` | 维度详情 |
| PUT | `/api/v1/dimensions/{id}` | 更新（write） |
| POST | `/api/v1/dimensions/{id}/deprecate` | 废止（gov） |
| POST | `/api/v1/dimensions/{id}/publish` | 发布（gov） |
| POST | `/api/v1/dimensions/mappings` | 创建映射（write） |
| GET | `/api/v1/dimensions/mappings` | 映射列表（guard） |
| POST | `/api/v1/dimensions/members` | 创建成员（write） |
| POST | `/api/v1/dimensions/{id}/metrics` | 绑定指标-维度（write） |
| GET | `/api/v1/dimensions/metrics/{metric_id}` | 指标维度列表（guard） |
| POST | `/api/v1/dimensions/reconciliations` | 提交对账（write） |
| GET | `/api/v1/dimensions/reconciliations` | 对账列表（guard） |
| POST | `/api/v1/dimensions/reconciliations/{id}/review` | 复核（gov，body `reviewer_id`） |

## 3. 状态机与留痕

`dimension.status`：`DRAFT → PUBLISHED`（publish）→ `DEPRECATED`（deprecate）。`reconciliation.status`：`PENDING → APPROVED/REJECTED`（review）。非法转移返回 `400`。

写操作经 `write_audit` 落审计。

> **已知**：`review_reconciliation` 的复核人取请求体 `reviewer_id` 而非认证 `user.id`（身份可伪造风险，归级见 §6.3）；`create_mapping` / `create_member` / `bind_metric_dimension` / `submit_reconciliation` 端点未写 audit（审计覆盖缺口，见 §6.3）。

## 4. 业务语义

- 维度映射 `mapping_type`：`EQUIVALENT`（等价）/ `PARTIAL`（部分），唯一约束 `(source_dim_code, target_dim_code, mapping_type)`。
- 指标-维度角色决定切片/分区/过滤语义；`default_member` 为空时由消费层默认处理。

## 5. 依赖与故障

| 依赖 | 故障表现 | 处置 |
|------|----------|------|
| MySQL（dimension 五表） | 5xx | 确认迁移 `upgrade head`（0009 + 0012）；连接池 |
| audit 服务 | 写操作无审计 | 检查 audit 健康；不影响落库 |
| RBAC（write / gov） | 403 | 确认调用方角色 |

## 6. 迁移与回滚

- 相关迁移：`0009_dimension`（dimension / dimension_member / dimension_mapping / metric_dimension / reconciliation）、`0012_audit_columns`（补 `deleted_at` / `reconciliation.updated_at`）。
- 回滚：`alembic downgrade -1`；`0009` 会 drop 五张表，`0012` 仅 drop 审计列，均结构回退、数据无损。

## 7. 可观测性

- 结构化日志：维度创建/发布/废止、对账提交/复核。
- 审计：`create/update/deprecate/publish/review` 写 audit（其余写端点审计缺口见 §6.3）。

## 8. 已知限制（一期）

- `review_reconciliation` 复核人取请求体（身份可伪造）—— §6.3 待修复项。
- `create_mapping` / `create_member` / `bind_metric_dimension` / `submit_reconciliation` 未写审计 —— §6.3 待补项。
- `create_dimension` 无 `user_id` 入参，需依赖审计 operator 留痕。
- 性能基线未单独压测。
