# Lineage Runbook

> 服务：lineage（指标血缘与影响分析，TD §12.2 / FR-04, FR-93, FR-94）
> 状态门槛：released（verified + runbook + migration 可逆，§1.5 人工 ratify 待补）

## 1. 服务概述
- **职责**：SQL 解析生成表级/字段级血缘（sqlglot 投影级映射）；影响分析（MySQL 内 BFS，深度≤5）；Neo4j 图存储（best-effort 降级）；血缘事件发布（熔断）；按节点清理孤儿边。
- **依赖**：
  - MySQL（lineage_edge 权威存储）
  - Neo4j（可选，不可达时静默降级，不影响主流程）
  - sqlglot（纯解析，无注入面）
- **关键指标**：
  - 影响面计算深度≤5 跳 P95 < 500ms
  - `max_edges=5000` 扇出上限（防无限响应）

## 2. 部署步骤
- **前置条件**：MySQL 可达；Neo4j 可选（缺失则降级）；`alembic upgrade head`。
- **部署命令**：
  ```bash
  UNISENSE_DB_URL=... UNISENSE_JWT_SECRET=... alembic upgrade head
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
  ```
- **验证步骤**：
  - `POST /api/v1/lineage/parse` → 200 返回表/字段血缘
  - `POST /api/v1/lineage/impact` → 200（大扇出受 max_edges 截断）
  - `DELETE /api/v1/lineage/edges` → 200（清理孤儿边，RBAC+注入守卫+LINEAGE_DELETE 审计）

## 3. 监控指标
- **Prometheus 指标**：解析耗时、影响分析跳数分布、Neo4j 写入失败计数、BFS 截断次数。
- **告警规则**：
  - `lineage_impact_p95 > 500ms` → 解析/查询变慢
  - `lineage_bfs_truncated_total > 0` 突增 → 出现超大扇出节点（数据建模问题）

## 4. 告警处置
| 告警 | 原因 | 处置步骤 |
|------|------|----------|
| 影响分析慢 | N+1 DB 往返（受 max_hops≤10/max_edges≤5000 约束） | 确认目标规模下可控；超大规模走离线批处理 |
| Neo4j 写入失败 | 图库宕机 | 无需干预，已静默降级；恢复后由事件补图 |
| 字段血缘断裂 | SQL 别名未解析 / 仅顶层投影 | 已修复 `_build_alias_map`；深层 CTE/子查询待 LLM 通道补全（已知限制） |
| 孤儿边 | 数据源删除未清血缘 | `DELETE /api/v1/lineage/edges` 清理 |

## 5. 回滚步骤
- **migration down**：`alembic downgrade -1`
- **K8s 回滚**：`kubectl rollout undo deploy/unisense-api`
- **血缘数据回退**：edges 为幂等 upsert，重跑 `parse` 可重建；孤儿边用 `DELETE /edges` 清理

## 6. 联系人
- **Owner / Backup**：见 `docs/module-status.yaml` lineage.owner（空缺，platform_admin 指派）
- **升级路径**：On-call → platform_admin → 架构委员会
- **相关文档**：TD §12.2、CHANGELOG_MODULES（lineage 双视角审查，0 High，修复 3 Medium）
