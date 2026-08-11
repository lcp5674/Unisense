# Runbook：数据质量服务（quality）

> 模块状态：`released`（13/13 门禁绿 + 双视角审查 0 High；人工 ratify 待 §1.5）
> 关联文档：TD §12.8、FR-10、docs/CHANGELOG_MODULES.md

## 1. 职责边界

数据质量服务负责：

- 质量规则 CRUD（随指标 `PUBLISHED` 注册，按 tier/dw_layer 差异化；v1 仅支持 `STATIC` 静态阈值模式）
- 异常事件闭环 `OPEN → ACK → RESOLVED → CLOSED`
- 检测引擎一期：静态阈值评估（obs vs threshold），命中落 `QualityEvent` 并 best-effort 触发告警

**一期明确不做**（避免范围蔓延）：外部基准对账、血缘传导、动态基线（dynamic_baseline / yoy_woy / cross_source）求值、修复建议自动生成。

## 2. 关键端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/quality/rules` | 创建规则（body `QualityRuleCreate`，鉴权 owner/admin） |
| GET | `/api/v1/quality/rules` | 规则列表（metric_id/type/severity/enabled 过滤 + 分页） |
| GET | `/api/v1/quality/rules/{id}` | 规则详情 |
| PUT | `/api/v1/quality/rules/{id}` | 更新规则（rule_mode 仅允许 STATIC） |
| DELETE | `/api/v1/quality/rules/{id}` | 逻辑删除（deleted_at 软删） |
| POST | `/api/v1/quality/detect` | 同步检测（body `QualityDetectRequest`，命中返回事件） |
| GET | `/api/v1/quality/events` | 异常事件列表（status/level 过滤 + 分页） |
| POST | `/api/v1/quality/events/{id}/ack` | ACK（body `QualityEventAck.note` 持久化到 ack_note，记录 ack_by/ack_at） |
| POST | `/api/v1/quality/events/{id}/resolve` | RESOLVE（记录 resolved_by/resolved_at） |
| POST | `/api/v1/quality/events/{id}/close` | CLOSE（记录 closed_by/closed_at） |

## 3. 状态机与留痕

`quality_event.status` 转移严格单向，非法转移返回 `400 QUALITY_EVENT_STATE_INVALID`：

```
OPEN ──ack──> ACK ──resolve──> RESOLVED ──close──> CLOSED
```

每次转移**必须携带 operator（`user_id`）**，由仓储 `transition_event` 落操作人留痕列：

| 转移 | 留痕列 |
|------|--------|
| ACK | `ack_by`, `ack_at`, `ack_note`（处理说明） |
| RESOLVE | `resolved_by`, `resolved_at` |
| CLOSE | `closed_by`, `closed_at` |

> 留痕列用于治理闭环审计回溯；`user_id` 为必填，不可缺省或传空（否则责任链断裂，属 §6.3 已知缺陷修复项）。

## 4. 检测引擎语义

- 阈值 `op` 描述「正常值应满足的条件」，越界即异常；`min/max` 双边阈值越界时，事件 `threshold` 记录实际越界的边界值（回溯异常方向）。
- 同指标同类型多条规则命中时，取**最高严重级**（P0 优先），避免被随机丢弃。
- 未实现模式（`DYNAMIC_BASELINE`/`YOY_WOY`/`CROSS_SOURCE`）在检测时**跳过并告警**，不落事件（杜绝静默失效）；创建规则时配置未实现模式在 `create`/`update` 阶段 fail-fast 拒绝（`QUALITY_RULE_MODE_UNSUPPORTED`）。

## 5. 依赖与故障

| 依赖 | 故障表现 | 处置 |
|------|----------|------|
| MySQL（quality_rule / quality_event） | 检测/查询 5xx | 检查 `unisense_perf` 库迁移是否 `upgrade head`；确认连接池 |
| notify（告警 best-effort） | 检测成功但无告警 | 不影响事件落库；查 `quality.anomaly` 发布日志与 notify 服务健康 |
| 鉴权（RBAC owner/admin） | 403 | 确认调用方为指标 owner 或质量管理员 |

## 6. 迁移与回滚

- 最新迁移：`0007_quality_trace`（在 `0006_consume` 之上），新增操作人留痕列 + `ack_note`，`downgrade()` 按序 drop 七列，可逆。
- 回滚：`alembic downgrade -1`（会丢弃留痕数据，仅结构回退，需评估审计影响）。

## 7. 可观测性

- 结构化日志（structlog JSON）：`quality.detect skip unsupported mode`、`quality.anomaly` 发布。
- 指标：检测调用量 / 命中率 / 事件状态分布可经 observability 服务聚合。

## 8. 已知限制（一期）

- 动态基线 / 同环比 / 跨源校验未实现（规则创建拦截）。
- 修复建议、ack note 之外的处理 SLA、事件批量导出未实现。
- 性能基线未单独压测；随语义/消费层统一在 `unisense_perf` 验证。
