# Lineage Runbook

> 服务：lineage（指标血缘与影响分析，TD §12.2 / FR-04, FR-93, FR-94）
> 状态门槛：released（verified + runbook + migration 可逆，§1.5 人工 ratify 待补）
> 最近更新：2026-08-12（生产级重构：五通道解析 / Neo4j 图读路径 / 环检 / 历史快照 / what-if / PII 传导）

## 1. 服务概述
- **职责**：SQL 解析生成表级/字段级血缘（sqlglot 五通道：SQL AST 主通道 + MERGE/UNION/CTE 深度解析 + sqlparse 多语句/净化降级 + 方言透传）；影响分析**图优先**（Neo4j Cypher），图不可用时回退 MySQL BFS；血缘历史快照（WORM）；变更影响预览（what-if）；PII 沿血缘传导；按节点清理孤儿边。
- **依赖**：
  - MySQL（lineage_edge / lineage_edge_history 权威存储）
  - Neo4j（**可选但推荐**：图遍历影响分析；不可达时静默降级 MySQL，不影响主流程）
  - Redis（影响分析结果缓存 TTL 60s + 事件发布，可选）
  - sqlglot / sqlparse（纯解析，无注入面）
- **关键指标**：
  - 影响面计算深度≤5 跳 P95 < 500ms
  - `max_hops≤10` / `max_edges=5000` 双重上限（防无限响应）
  - 血缘核心模块覆盖率：repository/service/graph/events >90%（实测 97%/94%/100%/100%）

## 2. 部署步骤
- **前置条件**：MySQL 可达；Neo4j 建议配置（缺失则影响分析走 MySQL BFS）；`alembic upgrade head`（需到 0019）。
- **部署命令**：
  ```bash
  UNISENSE_DB_URL=... UNISENSE_JWT_SECRET=... alembic upgrade head
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
  ```
- **验证步骤**：
  - `POST /api/v1/lineage/parse` → 200 返回表/字段血缘（多语句 SQL 会拆分解析）
  - `GET /api/v1/lineage/impact?node=...&direction=...` → 200 分页 `{items,total,page,has_more}`
  - `POST /api/v1/lineage/impact-preview` → 200 what-if 变更影响预览
  - `DELETE /api/v1/lineage/edges` → 200 清理孤儿边（RBAC+注入守卫+LINEAGE_DELETE 审计）

## 3. 监控指标
- **Prometheus 指标**：解析耗时、影响分析跳数分布、Neo4j 写入失败计数、BFS 截断次数、图读降级次数。
- **告警规则**：
  - `lineage_impact_p95 > 500ms` → 解析/查询变慢
  - `lineage_bfs_truncated_total > 0` 突增 → 出现超大扇出节点（数据建模问题）
  - `lineage_graph_query_failed` 连续出现 → Neo4j 不可达，影响分析降级 MySQL（观察恢复）

## 4. 告警处置
| 告警 | 原因 | 处置步骤 |
|------|------|----------|
| 影响分析慢 | MySQL BFS N+1（受 max_hops≤10/max_edges≤5000 约束） | 确认 Neo4j 已配置启用图优先；超大规模走离线批处理 |
| Neo4j 写入/查询失败 | 图库宕机 | 无需干预，已静默降级 MySQL；恢复后事件补图/图优先自动回归 |
| 字段血缘断裂 | SQL 别名未解析 / 深 CTE 未展开 | 已修复 `_build_alias_map` + CTE 递归；确认方言参数正确（hive/mysql/doris） |
| 孤儿边 | 数据源删除未清血缘 | `DELETE /api/v1/lineage/edges` 清理 |
| 循环依赖 409 | 建 DERIVED_FROM 边成环 | 检查指标/表派生关系，调整口径消除环 |

## 5. 生产能力说明（2026-08-12 新增）

### 5.1 五通道血缘解析
- 通道① SQL AST（sqlglot，表级 L1 + 字段级 L2，深度≤8 递归 CTE/子查询/别名）
- 通道② 调度 DAG（规划中，对接 Airflow）
- 通道③ 指标口径表达式（指标级 L3 `metric:code` 节点 DERIVED_FROM 边）
- 通道④ 运行时日志（规划中）
- 通道⑤ LLM 补位（规划中，候选边需人确认）

### 5.2 影响分析图优先
- `LineageGraphClient.query_impact` 走 Neo4j Cypher 可变长关系（`[:LINEAGE*1..N]`）
- Neo4j 不可用/熔断打开 → 自动降级 MySQL BFS
- 结果经 Redis cache-aside（TTL 60s）

### 5.3 循环依赖检测
- 建 `DERIVED_FROM` 边前调用 `would_create_cycle`（BFS 反向可达检测）
- 命中返回 `409` + `ConflictError`，拒绝成环边

### 5.4 血缘历史快照（R10-04）
- `lineage_edge_history` 表（WORM，只追加）
- 边覆盖前自动记录旧值（change_reason：schema_drift/reparse/manual/rename）
- 保留期 ≥180 天，支持时间旅行溯源

### 5.5 变更影响预览（what-if）
- `POST /api/v1/lineage/impact-preview {metric_code, change_type}`
- 返回受影响指标/物理表/消费方明细 + risk_level（low/medium/high/critical）

### 5.6 PII 沿血缘传导
- `propagate_pii(node, depth=3)`：下游 DERIVED_FROM 边标记 `pii_inherited=True`
- 查询响应含 `pii_inherited` 字段

### 5.7 断链登记（R10-03）
- `edge_type=EXTERNAL_BREAK` 边标记未接入外部依赖（规划 API 接入）

## 6. 回滚步骤
- **migration down**：`alembic downgrade -1`（0019 → 0018 collector → 0017）
- **K8s 回滚**：`kubectl rollout undo deploy/unisense-api`
- **血缘数据回退**：edges 为幂等 upsert，重跑 `parse` 可重建；孤儿边用 `DELETE /edges` 清理；历史快照只追加不可改

## 7. 联系人
- **Owner / Backup**：见 `docs/module-status.yaml` lineage.owner（空缺，platform_admin 指派）
- **升级路径**：On-call → platform_admin → 架构委员会
- **相关文档**：TD §12.2、CHANGELOG_MODULES（lineage 双视角审查 0 High + 2026-08-12 生产级重构提交 a1abcdb）
