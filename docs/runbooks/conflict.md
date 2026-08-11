# Conflict Runbook

> 服务：conflict（指标冲突检测与仲裁，TD §12.4 / FR-09）
> 状态门槛：released（verified + runbook + migration 可逆，§1.5 人工 ratify 待补）

## 1. 服务概述
- **职责**：四类冲突检测（同名异义/同义异名/口径单位/跨域同义 + 版本冲突/PII）；仲裁状态机（OPEN→NEGOTIATING→ESCALATED→RULED→CLOSED）；裁决记录沉淀为规则知识库；PII 冲突特殊路由转交 governance.pii_review（不入库）。
- **依赖**：
  - MySQL（conflict / ruling_record 权威存储）
  - 通知/治理服务（事件发布，best-effort 503 降级）
- **关键指标**：
  - 四类冲突检测 P95 < 1s
  - PII 冲突路由 403（security_reverse 验证）

## 2. 部署步骤
- **前置条件**：MySQL 可达；`alembic upgrade head`（0003 含 conflict+ruling_record，可逆）。
- **部署命令**：
  ```bash
  UNISENSE_DB_URL=... UNISENSE_JWT_SECRET=... alembic upgrade head
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
  ```
- **验证步骤**：
  - `POST /api/v1/conflicts/check` → 200 返回冲突列表
  - `POST /api/v1/conflicts/{id}/arbitrate` → 200 推进状态机
  - 普通用户 token 调管理员接口 → 403（RBAC 写闸门）

## 3. 监控指标
- **Prometheus 指标**：冲突检测耗时、仲裁状态跃迁计数、事件发布失败计数、PII 路由计数。
- **告警规则**：
  - `conflict_detect_p95 > 1s` → 文本相似度计算变慢
  - `conflict_event_fail_total > 0` 持续 → 通知/治理服务不可达

## 4. 告警处置
| 告警 | 原因 | 处置步骤 |
|------|------|----------|
| 检测慢 | 定义文本相似度（编辑距离/Jaccard）粗粒度 | 深度同义待 LLM 补位（TD 已规划）；非阻塞 |
| 事件发布失败 | 下游不可达 | 已 `_safe_publish` 降级；check/arbitrate 仍成功，返回 503 信号非 500 |
| PII 路由异常 | PII 冲突未转交治理 | 确认转交 governance.pii_review 不落常规表 |
| ORM 读取报错 | Enum 未配 values_callable | 已修复（`values_callable` 归一化）；复现查 `trace_id` |

## 5. 回滚步骤
- **migration down**：`alembic downgrade -1`（0002→upgrade 0003 已验证可逆）
- **K8s 回滚**：`kubectl rollout undo deploy/unisense-api`
- **裁决回退**：ruling_record 为追加式知识库，回退即关闭冲突状态，不删历史裁决

## 6. 联系人
- **Owner / Backup**：见 `docs/module-status.yaml` conflict.owner（空缺，platform_admin 指派）
- **升级路径**：On-call → platform_admin → 架构委员会
- **相关文档**：TD §12.4、CHANGELOG_MODULES（conflict 双视角审查，0 High，修复 4 处 in-session 缺陷）
