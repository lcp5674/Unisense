# Runbook：可观测性服务（observability）

> 模块状态：`implemented`（门禁 11/13 绿；runbook 已建；§6.3 双视角审查进行中；待 perf 压测 + §1.5 人工 ratify）
> 关联文档：TD §12.10、FR-16、docs/CHANGELOG_MODULES.md

## 1. 职责边界

可观测性服务负责：

- 用户反馈采集（`feedback`）
- 运营仪表盘聚合（`dashboard`）：质量 / API / 通知 / 血缘四类统计
- 质量指标时序（`quality_metrics`）

**依赖**：MySQL（`feedback` + 上游 metric / quality_event / api_log / notification / lineage_edge 等只读聚合）、audit 服务。
**一期明确不做**：指标持久化存储（实时聚合上游表）、告警阈值引擎、反馈自动归类。

## 2. 关键端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/observability/feedback` | 提交反馈（write，body `user_id`） |
| GET | `/api/v1/observability/feedback` | 反馈列表（guard） |
| GET | `/api/v1/observability/dashboard` | 运营仪表盘（guard） |
| GET | `/api/v1/observability/stats/quality` | 质量统计（guard） |
| GET | `/api/v1/observability/stats/api` | API 统计（guard） |
| GET | `/api/v1/observability/stats/notification` | 通知统计（guard） |
| GET | `/api/v1/observability/stats/lineage` | 血缘统计（guard） |
| GET | `/api/v1/observability/metrics/quality` | 质量指标时序（guard） |

## 3. 状态机与留痕

反馈为追加型记录，无状态机。`submit_feedback` 写审计。

> **已知**：`submit_feedback` 的反馈人取请求体 `FeedbackCreate.user_id` 而非认证 `user.id`，可被伪造为他人反馈（身份可伪造风险，归级见 §6.3）。

## 4. 业务语义

- 仪表盘四类统计实时聚合上游表（quality_event / api_log / notification / lineage_edge），无缓存。
- `quality_metrics` 返回质量事件按状态/级别的时序分布。

## 5. 依赖与故障

| 依赖 | 故障表现 | 处置 |
|------|----------|------|
| MySQL（上游表只读聚合） | 仪表盘 5xx | 确认上游（quality/notify/lineage）迁移 `upgrade head`；连接池 |
| 上游数据缺失 | 统计为空/偏低 | 确认上游服务正常写入；非本服务故障 |
| RBAC（write=viewer / read） | 403 | 确认调用方角色 |

## 6. 迁移与回滚

- 相关迁移：`0011_observability`（`feedback`）、`0012_audit_columns`（补 `deleted_at`）。
- 回滚：`alembic downgrade -1`；`0011` drop `feedback` 表，`0012` 仅 drop 审计列，均结构回退、数据无损。
- 本服务无自有写表（除 feedback），统计类无迁移依赖。

## 7. 可观测性

- 结构化日志：反馈提交。
- 审计：`submit_feedback` 写 audit（detail 含评分/对象）。
- 依赖 observability 自身聚合；若上游表无数据，统计静默为空（非故障，需结合上游健康判断）。

## 8. 已知限制（一期）

- `submit_feedback` 反馈人取请求体（身份可伪造）—— §6.3 待修复项。
- 仪表盘/统计实时聚合上游表、无缓存，大规模下查询压力（性能风险）。
- 无指标持久化，时序依赖实时聚合。
- 性能基线未单独压测。
