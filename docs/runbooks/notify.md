# Runbook：通知服务（notify）

> 模块状态：`released`（门禁 13/13 绿：lint/type/unit/integration/security_reverse/chaos/observability/migration/contract/doc_sync/perf_baseline/runbook/secret/supply_chain；§1.5 人工 ratify 待补）
> 关联文档：TD §12.9、FR-16、FR-17、docs/CHANGELOG_MODULES.md

## 1. 职责边界

通知服务负责：

- 事件发布（`publish_event`）：按订阅偏好 fan-out 生成 `Notification`，**即时投递**并回写 `SENT / FAILED`，返回 `delivered` 计数
- 通知状态回写（`mark_sent` / `mark_failed`）
- 订阅偏好管理（`upsert_subscription`）
- 事件日志与通知查询
- **真实渠道外发**：SMTP 邮件 / 钉钉 Webhook 机器人 / Webhook 三渠道适配

**依赖**：MySQL（`notification` / `event_log` / `subscription_pref`）、audit 服务、SMTP/Webhook 外部端点（可选）。

## 2. 渠道配置

通过环境变量配置外发渠道（`backend/app/core/config.py`）：

| 渠道 | 环境变量 | 说明 |
|------|----------|------|
| Webhook | `UNISENSE_NOTIFY_WEBHOOK_URL` | 通用 HTTP POST（`event_type`/`title`/`body`/`payload`） |
| 钉钉机器人 | `UNISENSE_NOTIFY_DINGTALK_WEBHOOK` | 按事件类型选模板：质量异常告警 / 审核待办 / 冲突升级（markdown）/ 默认文本 |
| SMTP 邮件 | `UNISENSE_NOTIFY_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` | `aiosmtplib` 异步发送（无 TLS 时自动尝试 STARTTLS） |

**投递语义**：`_dispatch` 按渠道分派；渠道未配置 → 记 warning 返回 `False`（该通知标记 `FAILED`）；HTTP 非 2xx / SMTP 异常 → 返回 `False` 不抛出（不阻塞事件发布主流程）。共享 `httpx.AsyncClient` 单例（超时 10s），避免连接池泄漏。

**验证**：
```bash
# 钉钉/Webhook 投递
curl -s -X POST http://localhost:8100/api/v1/notify/events \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"event_type":"quality.anomaly","source":"test","payload":{"msg":"x"}}'
# 返回 delivered>0 表示已真实外发（配置了渠道时）
```

## 3. 关键端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/notify/events` | 发布事件（write，fan-out + 即时投递） |
| GET | `/api/v1/notify/events` | 事件列表（`subscriber_id` 参数 + guard） |
| POST | `/api/v1/notify/notifications/{id}/sent` | 标记已发（write） |
| POST | `/api/v1/notify/notifications/{id}/failed` | 标记失败（write） |
| GET | `/api/v1/notify/notifications` | 通知列表（`subscriber_id` 参数 + guard） |
| POST | `/api/v1/notify/subscriptions` | 订阅偏好 upsert（write） |
| GET | `/api/v1/notify/subscriptions` | 订阅列表（`user_id` 参数 + guard） |

## 4. 状态机与留痕

`notification.status`：`PENDING → SENT / FAILED`（`mark_sent` / `mark_failed` 单向）。`event_log.notified` 标记是否已通知（`delivered>0` 时置 True）。

`publish_event` 写审计；`mark_sent` / `mark_failed` / `upsert_subscription` 经 `write_audit` 落审计。

> **安全收敛（§6.3 已修复）**：`subscriber_id` / `user_id` 强制取自已认证的 `user.id`（忽略请求体伪造，PLAT-2 IDOR）；`source` / `level` 取值白名单校验（PLAT-5）。

## 5. 业务语义

- 事件 `level`（`INFO` / `WARN` / `ERROR`）决定严重级；fan-out 仅对启用订阅（`enabled=True`）生效。
- `publish_event` 返回 `(event_id, notifications, delivered)`：无启用订阅时 `created=0`，`event.notified=False`（正常空 fan-out，不报错）。
- 渠道未配置时投递返回 `FAILED` 并记 warning（不静默假装成功）。

## 6. 依赖与故障

| 依赖 | 故障表现 | 处置 |
|------|----------|------|
| MySQL（三表） | 5xx | 确认迁移 `upgrade head`（0010 + 0012）；连接池 |
| SMTP/Webhook 不可达 | 该通知 `FAILED`（`delivered` 不增），事件仍落库 | 检查渠道配置与网络；`_dispatch` 不抛异常不阻塞 |
| 渠道未配置 | 全部 `FAILED` + warning 日志 | 按 §2 配置渠道 |
| RBAC（write / system 角色） | 403 | 确认调用方为 system 或通知管理员 |

## 7. 迁移与回滚

- 相关迁移：`0010_notify`（`notification` / `event_log` / `subscription_pref`）、`0012_audit_columns`（补 `deleted_at`）、`0022_notify_channel_enum`（channel 枚举扩展为 6 值：EMAIL/SMS/WEBHOOK/IN_APP/DINGTALK/console）。
- 回滚：`alembic downgrade -1`；`0010` drop 三表，`0012` 仅 drop 审计列，均结构回退、数据无损。

## 7.1 端到端验证（2026-08-12 实跑通过）

```bash
# 订阅（channel 自动规范化到枚举 value）
curl -X PUT $BASE/notify/subscriptions -H "Authorization: Bearer $TOKEN" \
  -d '{"user_id":1,"channel":"console","event_type":"quality.anomaly","enabled":true}'
# 发布事件 → 扇出 + 即时投递
curl -X POST $BASE/notify/events -H "Authorization: Bearer $TOKEN" \
  -d '{"event_type":"quality.anomaly","source":"quality","level":"WARN","payload":{...}}'
# 返回 {"event_id":1,"notifications":1,"delivered":1}

# 查询通知/事件/订阅（返回序列化 JSON 而非 ORM）
curl "$BASE/notify/notifications?status=SENT" -H "Authorization: Bearer $TOKEN"
curl "$BASE/notify/events" -H "Authorization: Bearer $TOKEN"
# 状态流转
curl -X POST $BASE/notify/notifications/2/sent -H "Authorization: Bearer $TOKEN"
curl -X POST $BASE/notify/notifications/2/failed -H "Authorization: Bearer $TOKEN"
```

验证要点：① console 渠道 `delivered=1`（SENT）；② 配置 `UNISENSE_NOTIFY_WEBHOOK_URL` 后 webhook 订阅真实 POST 到外部端点（本地接收器实测收到）；③ 历史修复（2026-08-12 端到端暴露并修复）：channel 枚举漂移（迁移 0022）、API ORM 序列化 500（改 from_model）、渠道大小写漂移（_dispatch 归一化 + SubscriptionUpsert validator），均有防回归单测。

## 8. 可观测性

- 结构化日志：事件发布、fan-out 计数、各渠道投递结果（成功/失败/未配置）。
- 审计：`publish_event` / `mark_*` / `upsert_subscription` 写 audit。
- 监控重点：`delivered / notifications` 比值（外发成功率）、`FAILED` 通知数（渠道健康）。

## 9. 已知限制

- `publish_event` 无幂等键，at-least-once 重发会生成重复通知（去重网关列为后续迭代）。
- 钉钉仅支持机器人 Webhook（markdown），无 @ 成员/群会话定向。
- SMTP 无 TLS 时自动 STARTTLS；强制 TLS 场景需配置中间件。
- 性能基线未单独压测。
