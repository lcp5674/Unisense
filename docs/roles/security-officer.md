# 安全官操作手册（Security Officer）

## 1. 角色定位
对平台**数据安全与合规**负总责：PII 识别与分级、RBAC 授权策略、敏感数据访问审计、被遗忘权执行、合规审查闭环。对应 `governance`（FR-11）、`quality`（FR-10 外部基准对账/合规留痕）、审计日志与被遗忘权（`erasure`）。

## 2. 核心职责
- **数据分级**：维护指标/字段的 Sensitivity Level（PII / CONFIDENTIAL / INTERNAL / PUBLIC）。
- **权限治理**：评审与签发授权（Grant），遵循默认拒绝（PDP fail-closed）。
- **敏感访问审计**：监控标注 `pii_access` 的审计记录。
- **被遗忘权（R7-09③）**：按合规请求对主体审计行去标识化。
- **合规审查**：参与服务交付的双视角（§6.3）审查，清零 High。

## 3. 日常操作清单
| 操作 | 端点 / 命令 | 说明 |
|------|------------|------|
| 分级重扫 | `POST /api/v1/classification/rescan` | 对目标重新推断敏感性（COMPL-2）；不可达降级 UNKNOWN |
| PII 复核 | `GET /api/v1/pii/review`、`POST /api/v1/pii/review` | PII 指标发布前必须经合规复核放行 |
| 权限快照 | `GET /api/v1/me/permissions` | 查看当前用户全部有效授权 |
| 权限校验 | `POST /api/v1/permissions/check` | 预检某操作是否被授权（fail-closed） |
| 授权签发 | `POST /api/v1/grants`、`POST /api/v1/grants/batch` | 单条/批量；批量上限 200 条，超限 422 |
| 授权回收 | `DELETE /api/v1/grants/{id}`、`POST /api/v1/grants/batch-revoke` | 软回收（过期标记），到期由 `expire_due_grants` 自动清理 |
| 被遗忘权 | `POST /api/v1/erasure` | **仅 compliance_officer** 可发起；对 `actor_id==subject` 的审计行去标识化，生成台账 |

> 写操作均需对应 RBAC 角色（见 `docs/runbooks/governance.md` 的 `_WRITE_ROLES` / `_COMPLIANCE_ROLES`）。

## 4. 关键监控指标
- 审计日志中 `pii_access=True` 的访问频次与主体分布。
- 分级覆盖率（已分级指标 / 总指标），UNKNOWN 占比。
- 越权访问：所有 403 响应（含注入守卫 `INJECTION_DETECTED` 400）。
- 被遗忘权台账：`erasure_request` 表的 `status=COMPLETED` 与 `affected_rows`。

## 5. 应急响应
1. **发现越权/泄露**：立即 `DELETE /api/v1/grants/{id}` 撤销相关授权；必要时批量回收。
2. **PII 误发布**：触发 `POST /api/v1/pii/review` 复核并拦截下游发布。
3. **合规删除请求**：执行 `POST /api/v1/erasure`（主体 user_id + 原因），核对 `affected_rows` 与台账 `token_prefix`。
4. **留存证据**：以上动作均落审计（action 含 `grant.*` / `pii.review` / `benchmark.*` / `erasure`），不可物理删除（WORM）。

## 6. 已知边界（诚实提示）
- 授权回收仅按角色闸门，缺归属/域归属校验（Medium，不阻塞）；批量上限 200。
- 定义相似度为文本粗粒度匹配，PII 二次校验待补。
- §1.5 人工 ratify 仍待补（agent 不可代签）。
