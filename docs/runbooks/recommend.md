# Runbook：推荐服务（recommend）

> 模块状态：`implemented`（门禁 11/13 绿；runbook 已建；§6.3 双视角审查进行中；待 perf 压测 + §1.5 人工 ratify）
> 关联文档：TD §12.12、docs/CHANGELOG_MODULES.md

## 1. 职责边界

推荐服务（**只读**）负责：

- 个性化指标推荐（`recommend_metrics`：基于用户近期行为事件）
- 关联指标推荐（`related_metrics`：基于血缘 `lineage_edge`）
- 术语推荐（`recommend_terms`：基于订阅领域）

**依赖**：MySQL（只读 `metric` / `lineage_edge` / `notification` / `subscription_pref`）、audit 服务（仅读）。
**一期明确不做**：协同过滤/向量召回模型、实时特征、推荐理由可解释性闭环。

## 2. 关键端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/recommend/metrics` | 个性化推荐（`user_id` 参数 + guard） |
| GET | `/api/v1/recommend/metrics/{metric_id}/related` | 关联指标（guard） |
| GET | `/api/v1/recommend/terms` | 术语推荐（guard） |

## 3. 状态机与留痕

只读服务，无状态机、无写审计。全部端点经 `guard_against_injection` + read 角色校验。

## 4. 业务语义

- `recommend_metrics` 以 `user_id` 拉取 `recent_user_events`（来自 notification 事件），聚合行为指标去重后推荐。
- `related_metrics` 以 `metric_id` 查 `related_edges`（双向血缘）返回关联指标。
- 冷启动：行为事件为空时返回空列表（正常，非错误）。

## 5. 依赖与故障

| 依赖 | 故障表现 | 处置 |
|------|----------|------|
| MySQL（上游表只读） | 5xx | 确认上游（metric/lineage/notify）迁移 `upgrade head`；连接池 |
| 上游行为数据缺失 | 推荐为空 | 确认 notification 事件正常写入；冷启动属正常 |
| RBAC（read 角色） | 403 | 确认调用方具备读权限 |

## 6. 迁移与回滚

- 本服务**无自有迁移**（仅聚合上游表），无 `downgrade` 影响。
- 回滚：上游 schema 变更异常时回退上游迁移；本服务代码用 K8s 版本回退。

## 7. 可观测性

- 只读服务，无写审计；依赖 API 层 RED 指标。
- 冷启动/空结果属正常业务态，不应告警。

## 8. 已知限制（一期）

> 以下 High / Medium 项经独立第三方 §6.3 双视角审查确认（详见 CHANGELOG_MODULES + module-status `review`），目前**退回 implemented 待修复**，修复后重跑 §6.3。

- **【High】RBAC 闸门缺失**：`_READ_ROLES` 已定义但全文件未 import / 未使用 `require_roles`，3 个端点均无角色闸门（对比 notify 正确用法为死代码造成的假象）；安全测试未写 403 反向用例，门禁假绿。
- **【High】IDOR 越权**：`recommend_metrics` 的 `user_id: int = Query(...)` 来自客户端且从不与 `user.id` 比对，service 直接透传 → 任意登录用户可读他人行为画像推荐。
- **【Medium】N+1 查询**：`recommend_metrics` 内 `related_edges` 按 seed 逐个查询（seeds 无上限），最坏数十次串行 DB 往返，与「P95 < 500ms」契约冲突；且 k6 baseline 阈值 800ms 宽于声明契约（为过门禁改阈值）。
- **【Medium】`recommend_terms` 直接返回 Term ORM 裸对象**，无 Pydantic schema，字段泄漏面不可控且违反其他服务范式。
- 冷启动无行为数据时推荐为空，无兜底热门指标；无模型召回，推荐质量依赖血缘/行为数据覆盖度。
- 性能基线未单独压测。
