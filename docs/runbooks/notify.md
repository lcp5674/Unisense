# Runbook：通知服务（notify）

> 模块状态：`implemented`（门禁 11/13 绿；runbook 已建；§6.3 双视角审查进行中；待 perf 压测 + §1.5 人工 ratify）
> 关联文档：TD §12.9、FR-16、FR-17、docs/CHANGELOG_MODULES.md

## 1. 职责边界

通知服务负责：

- 事件发布（`publish_event`）：按订阅偏好 fan-out 生成 `Notification`
- 通知状态回写（`mark_sent` / `mark_failed`）
- 订阅偏好管理（`upsert_subscription`）
- 事件日志与通知查询

**依赖**：MySQL（`notification` / `event_log` / `subscription_pref`）、audit 服务。
**一期明确不做**：真实渠道投递（邮件/SMS/Webhook 实际发送）、重试队列、去重网关。

## 2. 关键端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/notify/events` | 发布事件（write，fan-out 通知） |
| GET | `/api/v1/notify/events` | 事件列表（`subscriber_id` 参数 + guard） |
| POST | `/api/v1/notify/notifications/{id}/sent` | 标记已发（write） |
| POST | `/api/v1/notify/notifications/{id}/failed` | 标记失败（write） |
| GET | `/api/v1/notify/notifications` | 通知列表（`subscriber_id` 参数 + guard） |
| POST | `/api/v1/notify/subscriptions` | 订阅偏好 upsert（write） |
| GET | `/api/v1/notify/subscriptions` | 订阅列表（`user_id` 参数 + guard） |

## 3. 状态机与留痕

`notification.status`：`PENDING → SENT / FAILED`（`mark_sent` / `mark_failed` 单向）。`event_log.notified` 标记是否已通知。

`publish_event` 写审计；`mark_sent` / `mark_failed` / `upsert_subscription` 经 `write_audit` 落审计。

> **已知**：`publish_event` 无幂等键，同事件 at-least-once 重发会生成重复通知（见 §8 / §6.3）；`list_notifications` / `list_subscriptions` 按 `subscriber_id` / `user_id` 入参查询但未校验调用方归属，存在 IDOR 越权读取风险（见 §6.3）。

## 4. 业务语义

- 事件 `level`（`INFO` / `WARN` / `ERROR`）取自请求体，决定严重级；fan-out 仅对启用订阅（`enabled=True`）生效。
- `publish_event` 返回 `(created, notified)`：无启用订阅时 `created=0`，`event.notified=False`（不报错，属正常空 fan-out）。

## 5. 依赖与故障

| 依赖 | 故障表现 | 处置 |
|------|----------|------|
| MySQL（三表） | 5xx | 确认迁移 `upgrade head`（0010 + 0012）；连接池 |
| audit 服务 | 发布无审计 | 检查 audit 健康 |
| RBAC（write / system 角色） | 403 | 确认调用方为 system 或通知管理员 |

## 6. 迁移与回滚

- 相关迁移：`0010_notify`（`notification` / `event_log` / `subscription_pref`）、`0012_audit_columns`（补 `deleted_at`）。
- 回滚：`alembic downgrade -1`；`0010` drop 三表，`0012` 仅 drop 审计列，均结构回退、数据无损。

## 7. 可观测性

- 结构化日志：事件发布、fan-out 计数。
- 审计：`publish_event` / `mark_*` / `upsert_subscription` 写 audit。

## 8. 已知限制（一期）

- `publish_event` 无幂等键，重试会造成重复通知 —— §6.3 待修复项。
- `list_notifications` / `list_subscriptions` 缺归属校验（IDOR）—— §6.3 待修复项。
- 事件 `level` 取自请求体，可被调用方伪造严重级。
- 无真实渠道投递能力（一期仅落库，不实际发送）。
- 性能基线未单独压测。
