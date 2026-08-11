# Governance Runbook

> 服务：governance（权限与合规治理，TD §12.5 / FR-11）
> 状态门槛：released（verified + runbook + migration 可逆，§1.5 人工 ratify 待补）

## 1. 服务概述
- **职责**：RBAC 六角色；域授权 + 跨域只读引用（grants TTL 软回收）；PII 复核门禁（COMPL-1）；分级重扫（COMPL-2 降级 UNKNOWN）；权限快照；PDP 纯函数决策（fail-closed）。
- **依赖**：
  - MySQL（role / grants / classification 主存储，迁移 0004）
  - 事件总线（发布经 `CircuitBreaker` 降级）
- **关键指标**：
  - 合规门禁判定 P95 < 300ms
  - 裁决闭环率 ≥ 90%

## 2. 部署步骤
- **前置条件**：MySQL 可达；`alembic upgrade head`（0004 含 role/grants/classification，可逆）；JWT 密钥经 Secret Manager 注入。
- **部署命令**：
  ```bash
  UNISENSE_DB_URL=... UNISENSE_JWT_SECRET=... alembic upgrade head
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
  ```
- **验证步骤**：
  - `GET /api/v1/me/permissions` → 200 返回当前用户权限
  - `POST /api/v1/permissions/check` → 200 决策（fail-closed 默认拒绝）
  - `POST /api/v1/pii/review` 非合规官 → 403（PII 门禁）
  - `POST /api/v1/classification/rescan` → 200（分级重扫，UNKNOWN 降级）

## 3. 监控指标
- **Prometheus 指标**：门禁判定耗时、授权判定计数、PII 复核拒绝计数、grants 软回收计数、事件发布失败计数。
- **告警规则**：
  - `governance_gate_p95 > 300ms` → PDP 决策变慢
  - `governance_pii_review_denied > 0` 异常突增 → 可能是越权尝试
  - `governance_grant_expired_total` 持续 → 授权到期回收正常，关注关键授权续期

## 4. 告警处置
| 告警 | 原因 | 处置步骤 |
|------|------|----------|
| PII 复核被拒 | 非合规官/自审 | 确认 require_roles 归一化（已修复 User.role 枚举值）；自审置 pii_access 审计 |
| 授权回收异常 | 仅按角色闸门缺归属/域校验 | 已知 Medium：建议补 owner/domain_admin 范围（不阻塞）；临时手工 revoke |
| 门禁判定慢 | PDP 纯函数+DB 查询 | 确认索引命中；grants JSON 列幂等由服务层保证（无唯一索引） |
| 角色权限失效 | User.role 枚举不匹配 | 已修复 `_role_to_str`/`require_roles` 归一化；复现查 `trace_id` |

## 5. 回滚步骤
- **migration down**：`alembic downgrade -1`（0004→0003 已验证可逆；注意 role 表随迁移回退）
- **K8s 回滚**：`kubectl rollout undo deploy/unisense-api`
- **授权回退**：grants 支持 expire（TTL 软回收），回退即置 expired；历史审计保留

## 6. 联系人
- **Owner / Backup**：见 `docs/module-status.yaml` governance.owner（空缺，platform_admin 指派）
- **升级路径**：On-call → platform_admin → 架构委员会（合规事件升级合规官）
- **相关文档**：TD §12.5、CHANGELOG_MODULES（governance 双视角审查 0 High，修复 1 High；perf_baseline k6 P95=26ms 已验证）
